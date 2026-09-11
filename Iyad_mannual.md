# Iyad's Manual to Utopia

**A bitemporal, ontology-governed knowledge graph, and how to bend it toward medicine and the patient digital twin.**

Written 2026-09-11 against the `dev` branch (latest migration `0049`, latest ADR `0038`). Everything in Part A and Part B is what the code does today. Part C and Part D are proposals. Where a proposal needs a change to the data model I say so, and I name the ADR number it would take.

---

## How to read this

| If you want to | Read |
|---|---|
| Run it on your laptop in 10 minutes | Part A, section 1 |
| Understand what it stores and why it never overwrites | Part A, sections 2 to 4 |
| Understand the document → graph pipeline | Part A, section 5 |
| See every API surface (REST, MCP, RDF export, SQL mounts) | Part A, sections 7 to 9 |
| Know the rules the project enforces on contributors | Part B |
| Make it medical: SNOMED, ICD-10, ICD-11, LOINC, RxNorm | Part C |
| Build a patient digital twin on top of it | Part D |
| Know what to do first | Part E |

---

# Part A. What the system is and how it works

## 1. Shape of the deployment

One Rust binary plus one Postgres with `pgvector`. Nothing else runs.

| Concern | Where it lives |
|---|---|
| Relational data, graph, ledger, job queue | Postgres |
| Vectors (chunks, entity profiles, ontology labels) | `pgvector` columns, HNSW index per dimension built by a job |
| Full-text search | Tantivy, embedded in the binary, rebuilt from the DB if the index directory is empty |
| Raw uploaded files | `UTOPIA_DATA_DIR/files/`, looked up by content hash |
| Web UI | Vite/React SPA in `web/`, served by the binary in production |
| LLM | Any OpenAI-compatible chat + embedding endpoint, configured per workspace under Administration → Models |

### 1.1 Local run

```bash
docker compose up -d db                 # Postgres+pgvector on 127.0.0.1:1517
cargo run -p utopia-server              # :1516, runs migrations on startup
cd web && pnpm install && pnpm dev      # :5173, proxies /api to :1516
```

Or the prebuilt image:

```bash
docker compose --profile app up -d      # open http://localhost:1516 and register
```

The first registered account becomes the system administrator. Configure a chat model and an embedding model before uploading anything you want extracted; without them a document is still searchable by full text, but no graph grows.

### 1.2 Configuration

All settings are `UTOPIA_*` environment variables (copy `.env.example` to `.env`). The ones that matter:

| Variable | Meaning |
|---|---|
| `UTOPIA_DATABASE_URL` | Application connection |
| `UTOPIA_MIGRATION_URL` | Optional owner connection used only for migrations, so the app can run as the restricted `utopia_app` role |
| `UTOPIA_SECRET_KEY` | 32-byte AES-GCM key sealing LLM keys, connection strings and source tokens at rest. Generated into `data/secret.key` on first start if absent. Not in the DB. Back it up with the data directory. |
| `UTOPIA_JWT_SECRET` | Session signing; auto-generated if empty |
| `UTOPIA_OPEN_REGISTRATION` | `false` means only the first user can self-register |
| `UTOPIA_DATA_DIR` | Files and Tantivy index |

### 1.3 Crate map

| Crate | Role |
|---|---|
| `utopia-core` | `AppConfig`, domain models, `AppError`, AES-GCM sealing |
| `utopia-store` | **All SQL.** One module per domain: `graph`, `ontology`, `reasoning`, `jobs`, `review`, `resolution`, `governance`, `audit`, `world_axis`, `record_axis`, `business_rules`, `execution_gate`, `export`, … Nothing above this crate writes SQL. |
| `utopia-ingest` | Parsers (PDF, DOCX, PPTX, XLSX, XLS, ODS, CSV, TSV, Markdown, HTML, text), chunker (1200 chars, overlap 150), OWL/RDFS projection (`ontology_rdf.rs`) |
| `utopia-extract` | Prompt building and response parsing for entity and fact extraction; `governor.rs` is the agentic adjudicator |
| `utopia-reason` | Pure in-memory reasoning: axiom checks, forward-chaining derivation, attribute rules |
| `utopia-search` | Tantivy index with jieba tokenisation |
| `utopia-llm` | OpenAI-compatible chat and embedding client |
| `utopia-server` | axum routes, job dispatch, connectors (GitHub, Jira, Notion, RSS, WebDAV, S3), `query_engine/` (Postgres, MySQL, Trino, Databricks, Snowflake), MCP endpoint, RDF export, five embedded ontology packs |

---

## 2. The three things it stores

### 2.1 Entities

A row in `entities`. The important columns:

| Column | Meaning |
|---|---|
| `type_id` | Nullable FK to `entity_types`. **NULL means "no type decided", and that is a legitimate state**, not a fallback class (ADR 0009). |
| `canonical_name`, `aliases[]` | Names are not identity. Two "Ahmad Khalil" rows may coexist. Merges move the losing name into `aliases`. |
| `type_source` | `extracted`, `inferred`, or `human`. A human decision is never re-judged by the engine, including "a human decided it has no type". |
| `specific_type` | Free-text description the model gave ("vector database software"). Never enters the ontology; feeds type resolution. |
| `proposed_type` | The out-of-vocabulary type the model wanted. Drives ontology growth. |
| `profile_embedding` | Incremental centroid of the evidence chunks. Used for similarity in entity resolution. |
| `merged_into` | Points at the survivor after a merge. Merges are recorded in `entity_merges` and reversible. |

### 2.2 Facts

A row in `facts`, subject-predicate-object with **two clocks** (section 3). Attributes and relations share the table:

- A **relation** fact has `object_id` (another entity).
- An **attribute** fact has `object_value` (JSONB literal, normalised to the predicate's declared datatype: `text`, `number`, `date`, `bool`, plus `unit`).
- `predicate_id` is **nullable**. "The extractor saw an edge but the ontology has no relation for it" is not a relation called `related_to` (ADR 0010). The original wording is kept in `fact_evidence.proposed_predicate`.
- `supersedes` links a correcting row to the one it replaces. **Nothing is ever UPDATEd into a different truth**; a correction inserts a new row and closes the old one.
- `confidence` is the model's self-report. The project's own measurement says it is bimodal and tone-like, so tiers are decided structurally, not by this number.
- `derived_by_rule` is legacy; derived facts now live in a separate table (section 4).

Every fact has an evidence chain (`fact_evidence` → chunk → document). Since migration `0049`, a relation may carry **qualifiers** (`relation_type_qualifiers`, `fact_qualifiers`): typed attributes on the edge itself, e.g. `dose` on a `prescribed` edge (ADR 0037, in progress).

### 2.3 The ontology

Per knowledge base. Four tables carry the contract:

| Table | Holds |
|---|---|
| `entity_types` | Classes: `key` (short token the model reads), `label`, `description` (goes verbatim into the prompt), `iri` (global identity, used for re-import matching), `color`, `shape`, embeddings of label and description |
| `entity_type_parents` | `rdfs:subClassOf`, multiple inheritance allowed |
| `entity_type_disjoint` | `owl:disjointWith`, symmetric |
| `relation_types` | Relations and attributes in one table (`kind`). Carries `temporal` (`state`, `event`, `eternal`), `functional`, `inverse_functional`, `is_transitive`, `is_symmetric`, `is_asymmetric`, `is_irreflexive`, `inverse_of`, `sub_property_of`, `datatype`, `unit` |
| `relation_type_domains`, `relation_type_ranges` | Signature, many-to-many, with a flag for union vs intersection semantics |

The ontology is **a contract, not a suggestion** (ADR 0012). Extraction can only write types and predicates that exist. Anything else becomes a counted proposal in `ontology_proposals`, adopted when it is seen in at least 2 documents and 3 signals (`MIN_DOCS`, `MIN_SIGNALS`) or when a person adopts it.

**Cold start.** A new base seeds nothing. You can pick from five embedded packs (schema.org, W3C Org, PROV-O, FOAF, IOF Core), upload your own OWL/RDFS in Turtle or RDF/XML, or start empty. The importer stores the original file verbatim as a blob and *projects* what it understands; unrecognised predicates are reported as "not yet projected", never as errors (ADR 0001). This matters for Part C: a SNOMED or ICD OWL file loads without the importer needing to understand every axiom.

---

## 3. The two clocks

This is the feature that makes Utopia different from a knowledge graph or a vector store, and it is the feature a patient twin needs most.

| Axis | Columns | Question it answers |
|---|---|---|
| **World** (valid time) | `valid_from`, `valid_to`, `valid_from_precision`, `valid_to_precision`, `attested_at` | When was this true in the world? |
| **Record** (transaction time) | `recorded_at`, `invalidated_at`, `supersedes` | When did the system come to believe it, and when did it stop? |

Rules the store enforces:

1. **Precision travels with the date.** `valid_from_precision` runs year → month → day → hour → minute → second (ADR 0024) and must be NULL when the date is NULL. No default; an unmeasured date is not silently "day".
2. **An ended thing with an unknown end date** is `valid_to IS NULL` with `valid_to_precision = 'unknown'`. "Former CEO" is no longer read as "still CEO". A missing start is anchored at the document that attests it (`attested_at`, ADR 0022).
3. **Events hold at the moment they name** (`temporal = 'event'`), states hold over an interval, eternal facts have no dates (ADR 0031). A diagnosis date is an event; "has diabetes" is a state; "blood group O+" is eternal.
4. **Functional predicates auto-close.** A `functional` predicate (one value at a time) seeing a new value closes the open row. That is how "current medication" stays single-valued without anyone deleting.
5. **Every read takes `held_at` and `as_of`** (ADR 0019). `held_at` rewinds the world; `as_of` rewinds what the system knew. Entities rewind too, by unwinding `entity_merges`. Derived facts rewind with their premises.

Deletions and purges are events. The audit ledger (`audit.rs`, `agent_decisions`) is append-only and protected by a restricted DB role (migration `0010`); the application connection can insert and read but not update or drop it.

---

## 4. Reasoning

All of it is in `utopia-reason`, pure and in memory. The store feeds it and persists results.

### 4.1 Consistency check (R0), writes nothing

Runs after every ontology import and on demand from Review. First the ontology checks itself (eight defect kinds: a relation both symmetric and asymmetric, a subclass cycle, an inverse that does not point back, …) into `ontology_defects`. Then facts are checked against axioms: self-loop, asymmetry, transitive cycle, cardinality, signature. Violations land in `axiom_violations` with the full path. A person then retracts the fact, relaxes the axiom, or accepts both.

### 4.2 Materialised inference (R1), off by default

Per-base switch. Rules compile **only from ontology axioms** (transitive, symmetric, inverseOf, subPropertyOf). Derived facts go to `derived_facts`, never to `facts`. Asserted facts strictly override derived ones. Depth limit, cycle detection, a cap of 20,000 per predicate, and truncation is reported. A derivation that contradicts an assertion is not written; it leaves an `axiom_violations` row of kind `derived_contradiction` (ADR 0017), on the grounds that a contradiction points at an error upstream, not at the rule.

### 4.3 Attribute rules, written by people

`attribute_rules` + `attribute_rule_conditions` (ADRs 0021, 0029, 0030, 0032). A rule reads an entity's attribute facts and concludes either a **typing** (this `patient` is a `high_risk_patient`) or an **attribute** (`risk_tier = 'high'`). Conditions support `gt`, `gte`, `lt`, `lte`, `between`, `in`, `not_in`, `present`, grouped with AND inside a group and OR across groups, one level only. A rule may read what another rule concluded (fixed point, in memory, one run). The conclusion may be a computed expression over attributes, including across one relation hop, but **never an aggregate**: a `sum` asserts "these are all the readings", a completeness claim the base cannot make.

The proof of every derived fact is a tree of premises (`fact_derivations`), shown in the entity panel and over MCP.

### 4.4 Governance, the agent that adjudicates

Per-base `governance` switch. A `govern` job works the duplicate-entity queue, pulling **precedents from the ledger** into the prompt (ADR 0025), recording every look in `agent_decisions` with the model's own rationale (ADR 0026). An automatic merge is gated by what it would break (ADR 0027): a contradiction the checker would open, a derivation resting on either side, or a chat answer that cited either side sends the pair to a person regardless of confidence. Two reverts of the agent's merges in a week trip a fuse: the switch turns off and an alert fires.

---

## 5. Document → graph pipeline

Read `docs/pipeline.md` for the diagrams. The short form:

```
Upload / sync
  → parse → chunk (1200/150) → embed        → document READY (searchable, askable)
  → extract per chunk (one LLM call, ontology in prompt)
      → entities (type from list, specific_type free)
      → attribute facts (value normalised)
      → relation facts (signature checked; may swap direction or blank the predicate)
      → out-of-vocabulary → proposal, wording kept in evidence
  → entity resolution (exact/alias → embedding tiers → containment recall → batched LLM adjudication → merge or keep apart)
  → type resolution (manual today: preview → apply)
  → ontology growth (proposals counted, adopted)
  → consistency check (writes nothing)
  → materialised inference (opt-in)
```

Three things worth remembering:

- **Two-phase.** A document is `ready` the moment embedding finishes. Extraction queues behind it.
- **Prompt budget.** If the ontology fits 24,000 characters it goes in whole; otherwise per-chunk vector retrieval picks about 40 classes, 30 relations, 30 attributes plus ancestors (ADR 0006). A medical ontology will always take the retrieval path.
- **Every drop is counted.** Eleven reason codes in `extraction_drops` (`truncated_reply`, `malformed_item`, `subject_not_declared`, `attr_domain_mismatch`, `domain_mismatch`, `direction_corrected`, …). `attr_domain_mismatch` is the expensive one: the fact is never written and retyping the entity later does not recover it.

Everything asynchronous is a row in the `jobs` table, claimed with `FOR UPDATE SKIP LOCKED`. Kinds today: `process_document`, `extract_document`, `resolve_types`, `adjudicate_entities`, `govern`, `materialize_inferences`, `sync_source`, `build_vector_index`, `explore_mappings`, `bootstrap_ontology`, `embed_ontology`, `memory_ingest`, `hydrate_rss_entry`. Adding a background task is one arm in `main.rs::dispatch` plus an `enqueue` call.

---

## 6. Search and chat

Full-text (Tantivy) and vector (pgvector) fused with reciprocal rank fusion. Chat is an agent with tools over the base: `search_chunks`, `search_docs`, `get_document`, `find_entities`, `entity_facts`, `neighbors`, `paths_between`, `timeline`, `changes` (what the base learned or unlearned in a period), `list_rules`, `rule_matches`. Answers cite chunks and facts. A conversation streams over SSE. Chat can also `remember`: a sentence you tell it waits in `pending_facts` for a nod before becoming a fact (ADR 0015).

---

## 7. REST API

Base path `/api/v1`. Full list in `crates/utopia-server/src/api/mod.rs`. The groups:

| Group | Endpoints |
|---|---|
| Auth | `/auth/register`, `/auth/login`, `/auth/logout`, `/auth/me`, `/auth/password`, `/me/tokens` |
| Workspaces and bases | `/workspaces`, `/workspaces/{id}/kbs`, `/workspaces/{id}/settings` (models), `/kbs/{id}`, `/kbs/{id}/members`, `/kbs/{id}/readiness` |
| Ingest | `/kbs/{id}/documents` (upload), `/kbs/{id}/ingest` (API push), `/kbs/{id}/sources` (connectors), `/documents/{id}/reprocess`, `/extract`, `/restore`, `/purge` |
| Ontology | `/kbs/{id}/ontology`, `/ontology/imports/preview`, `/ontology/imports`, `/entity-types`, `/relation-types`, `/proposals`, `/adopt-predicate`, `/type-resolution/preview`, `/type-resolution/approve`, `/auto-extension`, `/uniqueness`, `/suggest` |
| Graph | `/kbs/{id}/graph/overview`, `/graph/neighborhood`, `/entities`, `/entities/{id}`, `/entities/{id}/history`, `/entities/merge`, `/merges/{id}/revert`, `/facts/{id}` (+ `/confirm`, `/reject`, `/close`, `/evidence`) |
| Reasoning | `/kbs/{id}/consistency/check`, `/inference/run`, `/rules`, `/rules/run`, `/rules/{id}/matches`, `/derived/{id}/proof`, `/violations/{id}/proof` |
| Review | `/kbs/{id}/review`, `/review/summary`, `/review/history`, `/review/batch`, `/review/pending`, `/review/{id}`, `/review/violations/{id}`, `/review/defects/{id}`, `/review/agent/{id}` |
| Data sources | `/admin/data-sources` (register, test, grant), `/kbs/{id}/data-sources` (mount), `/explore`, `/mappings`, `/mappings/preview` |
| Search and chat | `/kbs/{id}/search`, `/kbs/{id}/chat`, `/conversations`, `/conversations/{id}/stream` |
| Audit and export | `/kbs/{id}/audit`, `/kbs/{id}/events` (SSE), `/kbs/{id}/export?format=turtle\|jsonld`, `/kbs/{id}/extraction-drops` |
| Ops | `/health`, `/jobs/requeue`, `/kbs/{id}/jobs/failed`, `/alerts`, `/admin/users`, `/admin/deployment` |

Every graph read accepts `held_at` and `as_of` query parameters.

## 8. MCP

`POST /api/v1/kbs/{id}/mcp`, Streamable HTTP, authenticated by a personal token (`/account/tokens` in the UI). Identity comes from the person, scope from the token (ADR 0014). Tools are the same set chat uses, read-only today (`can_write` is hard-coded false). Every fact and entity carries a stable ledger identity, so an external agent (Claude, an EHR bot) can cite a Utopia fact by id and the citation survives corrections.

## 9. Mounted databases and Ontology2SQL

An administrator registers a database (Postgres, MySQL, Trino for Iceberg/Delta/Hive, Databricks, Snowflake) and grants it to a workspace. A base mounts it. An `explore_mappings` job proposes how each table aligns to the ontology (which class it is a table of, which columns are attributes, which are relations) through `ontology_proposals`, and a person adopts the alignment (ADR 0036, partially built). Chat then queries the database alongside documents. **A mapping is configuration, not a fact** (ADR 0011): nothing from the warehouse is copied into `facts`.

For KHCC this is the bridge to `AIDI-DB` and to the Databricks federated catalog. See Part D, section 3.

## 10. RDF export

`GET /kbs/{id}/export?format=turtle|jsonld` streams the whole base as RDF with the two clocks reified, so an auditor can read it without Utopia (ADR 0020). SPARQL is deliberately not offered.

---

# Part B. Contributor rules the project enforces

- Branch off `dev`, PR to `dev`. `main` is release-only. Commit `-s` (DCO). One English sentence as the message.
- Migrations: `migrations/NNNN_short_english_sentence.sql`, forward-only, never edited after merge. Check the latest number first (`0049` today).
- **Code comments are in Chinese** and explain the trap, not the code. UI strings, README, ADRs are English.
- UI strings go through i18n (`web/src/i18n/en.ts` and `zh.ts`). `web/DESIGN.md` is enforced by a style guard in `pnpm build`.
- Any change to the data model, ontology contract or public API gets an ADR in `docs/decisions/` first. Test file names are sentences that match the ADR they guard.
- DB-backed tests silently skip without `UTOPIA_DATABASE_URL`; set `UTOPIA_TEST_REQUIRE_DB=1` to make a skip a failure.
- Never add an `UPDATE` that rewrites history. Add a superseding row.

---

# Part C. Making it medical

## 1. What already fits medicine, and what does not

**Fits, out of the box:**

- Bitemporality is exactly the clinical record problem: a diagnosis made in March, revised in June, and a lab value that was true on a date. `held_at` gives "what was true on admission day"; `as_of` gives "what the team knew at the tumour board".
- Ontology as a contract is what a coding system is. Extraction cannot invent a diagnosis the vocabulary lacks; it can only propose.
- `type_id` nullable: an "unspecified" diagnosis is honestly untyped, not forced into a residual class.
- Attribute rules with typing conclusions map directly onto clinical criteria: eGFR < 60 for 3 months → `CKD`; ANC < 500 and temperature ≥ 38.3 → `febrile_neutropenia`. Proof trees give the premises. This is the KDIGO / CTCAE / staging pattern.
- Functional predicates auto-closing is how "current line of therapy" stays single-valued.
- The append-only ledger with restricted DB role is a compliance audit trail you do not have to build.
- Ontology2SQL is how VISTA/SILVER/GOLD tables get queried in the same conversation as the notes.

**Does not fit yet:**

| Gap | Why it matters medically |
|---|---|
| No notion of a **coded concept** distinct from an entity type | ICD codes are instances (a specific code) *and* a hierarchy (chapters, blocks). Today a class is a class; there is no place for `E11.9` as a *value* with a parent. |
| Entity resolution is name-driven | Patients must resolve by identifier (encoded MRN), never by name similarity. A 0.55 cosine merge between two patients is a safety event. |
| Ontology packs are general-purpose | schema.org has `MedicalCondition` but no codes; none of the five packs is a clinical terminology. |
| Chunk-level extraction has no section awareness | "History of" versus "Assessment" versus "Family history" changes whether a mention is the patient's own diagnosis. |
| `datatype` is `text`/`number`/`date`/`bool` | No `quantity` with UCUM units, no `coded` datatype pointing at a code system. `unit` exists as free text on the predicate. |
| No negation / uncertainty modelling on a fact | "No evidence of metastasis" extracted as `has_metastasis` is the classic clinical NLP failure. `confidence` is not the right slot for it. |
| Prompt budget (24,000 chars) forces retrieval for any real terminology | SNOMED CT is 350k concepts; ICD-10-CM is 70k codes. Retrieval by chunk vector will pick classes, but recall on rare codes will be poor without a proper terminology index. |
| Names and identifiers are stored in cleartext in `entities.canonical_name` | PHI. At KHCC every MRN must be Optimus-encoded and names Fernet-encrypted at rest. |

## 2. Recommended medical ontology stack

Do not load one giant terminology as the base's ontology. Layer it:

```
Layer 0  Utopia core classes (person, organisation, document, event)       [schema.org subset]
Layer 1  Clinical information model  (Patient, Encounter, Condition,       [FHIR-shaped, ~40 classes,
         Observation, MedicationRequest, Procedure, Specimen, ...)          written by hand as OWL]
Layer 2  Terminology bindings         (Condition.code → ICD-10 / ICD-11 /  [code systems, NOT loaded as
         SNOMED; Observation.code → LOINC; Medication.code → RxNorm/ATC)    entity_types; see section 3]
Layer 3  Domain rules                 (staging, CTCAE grades, KDIGO, ...)   [attribute_rules]
```

**Why FHIR-shaped for Layer 1.** FHIR resources are already the agreed clinical information model, they map cleanly to entity types (resource → class) and relation types (reference → relation, element → attribute), and they give you a free, well-documented export target. FHIR's own RDF representation (`fhir.ttl`) exists but is too large and too reified to load as-is; write a ~40-class projection by hand. This is one ADR and one Turtle file. Suggested classes and the key relations:

| Class | Key attributes | Key relations |
|---|---|---|
| `patient` | `mrn_encoded` (text, functional), `birth_date` (date), `sex` (text), `deceased_date` (date) | |
| `encounter` | `class` (inpatient/outpatient/ER), `admit_date`, `discharge_date` | `subject → patient`, `location → organization_unit` |
| `condition` | `clinical_status` (active/resolved), `onset_date`, `abatement_date`, `code` (coded) | `subject → patient`, `recorded_in → encounter`, `evidenced_by → observation` |
| `observation` | `code` (coded), `value` (number, unit), `effective_date` | `subject → patient`, `interprets → specimen` |
| `medication_statement` | `code` (coded), `dose` (number, unit), `route`, `start`, `end` | `subject → patient`, `reason → condition` |
| `procedure` | `code` (coded), `performed_date` | `subject → patient`, `reason → condition` |
| `specimen`, `imaging_study`, `pathology_report`, `care_plan`, `care_team`, `practitioner` | | |

Mark `condition`, `procedure`, `medication_statement` as `temporal = 'state'` where they are ongoing and `observation` as `event`. Declare `condition disjointWith procedure`, `patient disjointWith practitioner`, and so on. The consistency checker starts earning its keep on day one.

## 3. Integrating ICD-10 and ICD-11

### 3.1 The design decision: codes are values, not classes

Loading ICD-10 as `entity_types` would create 70,000 classes in one base. The prompt retrieval path would cope, but:

- Every entity would need a `type_id` pointing at a code, and a patient's *condition* is an entity whose *code* is a value, not its class.
- The type-resolution machinery would try to re-judge codes as if they were "what this entity is".
- Ontology growth would propose new codes from free text, which is the one thing a coding system forbids.

Instead, introduce a **coded datatype**. This is the single most important change in this manual. Concretely (new ADR, migration `0050`):

1. Add `'coded'` to `relation_types.datatype`'s CHECK, plus a `code_system` column (an IRI: `http://hl7.org/fhir/sid/icd-10`, `http://id.who.int/icd/release/11/mms`, `http://snomed.info/sct`, `http://loinc.org`, `http://www.nlm.nih.gov/research/umls/rxnorm`).
2. Store a coded value in `facts.object_value` as `{"system": "...", "code": "C50.911", "display": "Malignant neoplasm of unspecified site of right female breast", "version": "2026"}`. That is exactly a FHIR `Coding`, so export is free.
3. Add a `code_systems` table per deployment, and a `codes` table (`system`, `code`, `display`, `parent_code`, `chapter`, `valid_from`, `valid_to`, `replaced_by`) loaded from the official release files. ICD-10 and ICD-11 both publish parent links; ICD-11 publishes them through its API, ICD-10 through the tabular XML. The `codes` table is itself **bitemporal on the world axis** because codes are retired and replaced between releases; store the release date as `valid_from`.
4. A `codes.embedding` column plus its own HNSW index, so extraction can retrieve candidate codes per chunk exactly as it retrieves classes today (section A.5, prompt budget). This is a second retrieval, run only for predicates whose datatype is `coded`.

What extraction then does: for a `condition` entity it emits `code` as a coded attribute picked from the retrieved candidates, or leaves it empty and puts the original wording in `proposed_predicate`. No new code is ever created by the pipeline. Unresolved wording queues for a coder, which is the ICD workflow hospitals already run.

### 3.2 ICD-10 versus ICD-11: which and how

| | ICD-10 (WHO 2019, or ICD-10-CM / ICD-10-AM) | ICD-11 MMS (2026 release) |
|---|---|---|
| What KHCC bills and reports with today | Yes | Not yet |
| Structure | Chapters → blocks → 3-char → 4-char codes. Strict tree. | Foundation (a polyhierarchical ontology, ~80k entities) plus the MMS linearisation (the codes). Post-coordination via extension codes. |
| Distribution | Tabular list XML or ClaML from WHO; CMS publishes ICD-10-CM XML | Official REST API (`id.who.int/icd/`), OAuth2; also a downloadable Docker container of the same API for offline use |
| Ontology fit | Tree only; `parent_code` suffices | The Foundation is already an ontology with `is-a` and multiple parents; it projects into `entity_type_parents`-style structures naturally |
| Mapping | WHO publishes ICD-10 ↔ ICD-11 crosswalk tables | Same tables, plus SNOMED ↔ ICD-11 maps in progress |

**Recommendation.** Load **both**, as two `code_systems`, and store the WHO crosswalk as a `code_mappings` table (`from_system`, `from_code`, `to_system`, `to_code`, `kind` = equivalent / broader / narrower, `valid_from`). Extract to ICD-10 first because that is what your registry, your tumour board notes and `SILVER_*` tables speak, and derive the ICD-11 code through the mapping table as a **derived fact** with the mapping row as its premise. That way the ICD-11 code carries a proof ("because ICD-10 C50.9 maps to 2C60.Z, equivalent, WHO table 2026-02") and is retired automatically if the mapping changes. This reuses the derivation machinery in section A.4 rather than inventing a new one.

For ICD-11 specifically, run the WHO Docker container inside KHCC's network; the README's offline promise should hold for terminology too. Import the MMS linearisation into `codes` and, optionally, the Foundation's parents into a `code_parents` table so a rule can say "any code under `2C6` (breast neoplasms)".

### 3.3 SNOMED CT, LOINC, RxNorm

Same mechanism, three more `code_systems`. Notes:

- **SNOMED CT** is distributed as RF2 tables, and Jordan is not a SNOMED International member as of this writing. Check licensing before loading; the affiliate licence is required for production use. The RF2 `Relationship` file gives `is-a` and attribute relationships (finding site, causative agent). Load only the `is-a` parents into `code_parents`; the rest is out of scope until a rule needs it.
- **LOINC** for `observation.code`. Free with registration. The `LOINC.csv` is 100k rows; load only lab and vital-sign classes first.
- **RxNorm** or **ATC** for medications. KHCC chemotherapy data in `silver_chemotherapy_journey` carries drug names; ATC is the smaller and more stable of the two for a first pass.

## 4. Patient safety changes to entity resolution

Entity resolution today: exact or alias match, embedding similarity tiers (attach ≥ 0.55, review 0.35 to 0.55), containment recall, LLM adjudication. For `patient` entities this must be **replaced, not tuned**:

1. Declare `patient.mrn_encoded` as `functional` and `inverse_functional` (one MRN per patient, one patient per MRN). `inverse_functional` already exists on `relation_types` for exactly this purpose.
2. Add an **identity rule** to the resolution stage: if an entity type has an inverse-functional attribute, resolve **only** on that attribute; never enqueue a name-based pair for that type. ADR 0025 already introduced "two rules the model cannot argue with" (name shapes, type families); this is a third.
3. Governance's automatic merge must be **off** for `patient`. Use the execution gate (ADR 0027) with a new hard condition: type has an inverse-functional attribute → `escalate_impact` always.
4. The MRN stored in `object_value` is the **Optimus-encoded** value. Raw MRN never enters the base. The display edge (entity panel, CSV export) decodes once. This is the KHCC rule and Utopia's cleartext columns make it your responsibility, not the system's.

## 5. Negation, uncertainty and section context

Three small additions, each an ADR:

- **Assertion status on a fact.** A `fact_assertion` column or a qualifier: `present`, `absent`, `possible`, `hypothetical`, `family_history`, `historical`. This is the NegEx / ConText classification and it is what stops "no evidence of metastasis" becoming a metastasis fact. Qualifiers (migration `0049`) already give you the storage; what is missing is the prompt instruction and the write-path rule that an `absent` fact never contributes to a rule premise unless the rule asks for absence.
- **Section-aware chunking.** The chunker is 1200 chars with overlap and knows nothing about document structure. For clinical notes add a section detector (Chief Complaint, HPI, PMH, FH, Meds, Assessment, Plan) and pass the section name into the extraction prompt. It costs one column on `chunks` and a line in the prompt.
- **Temporal anchoring to the encounter.** A note dated 2026-03-14 that says "diagnosed two years ago" should produce `onset_date = 2024`, precision `year`, `attested_at = 2026-03-14`. The precision ladder and `attested_at` already exist (ADR 0022, 0024); the prompt needs to be told the document date is the anchor.

## 6. Domain rules that write themselves

Once conditions carry ICD codes and observations carry LOINC codes with numeric values, the attribute-rule engine expresses most published criteria without code:

| Criterion | Rule shape |
|---|---|
| CTCAE neutropenia grade | `observation.code = LOINC 751-8` AND `value between 500 and 1000` → typing `neutropenia_grade_3` |
| KDIGO AKI stage 1 | creatinine rise ≥ 0.3 mg/dL within 48 h or ≥ 1.5× baseline. The "within 48 h" needs a computed operand across two readings, which ADR 0032 permits across a relation but not as an aggregate; baseline-versus-current is two premises, allowed. |
| Febrile neutropenia | ANC < 500 AND temperature ≥ 38.3 → typing `febrile_neutropenia` (this is the alert your AIDI pipelines already compute in SQL) |
| Eligible for a protocol | code under `2C6` AND stage in (`II`, `III`) AND age ≥ 18 → typing `eligible_protocol_X` |

Every conclusion carries its premises and is retired when a premise is superseded. That retirement is what a hand-written SQL pipeline never gives you.

---

# Part D. The patient digital twin

## 1. What a twin is in Utopia terms

A patient twin is **one `patient` entity plus everything reachable from it, read at a chosen `held_at` and `as_of`**. Nothing else needs building for the twin to *exist*; the work is in making it complete, safe and queryable.

```
                 held_at = 2026-06-01, as_of = now
 patient ──subject── condition (C50.9, active since 2025-11, stage IIB)
         ──subject── observation (LOINC 751-8, ANC 420, 2026-05-30)
         ──subject── medication_statement (docetaxel 75 mg/m², cycle 4, state)
         ──subject── procedure (mastectomy, 2026-01-12, event)
         ──subject── encounter (inpatient, 2026-05-29 → open)
         ──derived── febrile_neutropenia (rule FN-1; premises: ANC 420, temp 38.6)
```

Move `held_at` to 2026-01-01 and the mastectomy is a future event, the ANC is a different reading, the derived typing disappears. Move `as_of` to the day of the tumour board and you see what the team could have known. That is the twin's core capability and it is already built.

## 2. Architecture for KHCC

```
Sources                             Utopia (per-KB)                    Consumers
──────────────────────────────      ────────────────────────────       ──────────────────────
VISTA_/SILVER_/GOLD_ (AIDI-DB) ──►  mounted data source (Ontology2SQL) ─┐
Pathology / radiology text     ──►  documents → extraction               ├─► chat + MCP (clinicians, agents)
Clinical notes (ISI documents) ──►  documents → extraction               ├─► FHIR / RDF export
Registry (ICD-10 coded)        ──►  API ingest as pre-coded facts        ├─► rules → alerts (FN, AKI, ADR)
Chemo journey table            ──►  API ingest as medication_statements ─┘   → Databricks pipelines read the twin
```

Three ingestion modes, and which to use for what:

| Mode | Use for | Why |
|---|---|---|
| **API ingest of pre-coded facts** (`POST /kbs/{id}/ingest` with subject, predicate, coded object, valid interval, evidence pointer) | Registry diagnoses, lab results, chemo administrations, admissions | Structured data should never pass through an LLM. It is already coded. The pipeline writes facts directly with `confidence = 1.0` and a source-row evidence pointer. |
| **Document extraction** | Pathology reports, radiology narratives, progress notes, tumour board minutes | This is where the LLM adds value: staging language, response assessment, plans, reasons. |
| **Mounted data source** | Ad-hoc questions over the whole warehouse that were never ingested | No copy; the query runs on `AIDI-DB` or Databricks at question time. |

The API-ingest route exists (`sources_routes::ingest`) but takes documents today. Extending it to accept **facts** with a coded object and an explicit interval is the second ADR after the coded datatype. It is essentially what the `remember` path already does minus the nod.

## 3. The Databricks bridge

Utopia's `query_engine/databricks.rs` speaks to a Databricks SQL warehouse over the same trait as Trino and Snowflake (ADR 0018), verified against Trino only so far. Two consequences for KHCC:

- Mount `aidi_catalog` read-only. Only `LIKE`, `=`, `IN` and range predicates push down to a federated catalog; Utopia's generated SQL is simple enough that this holds, but check the query log on the first few conversations.
- The reverse direction is more valuable: a Databricks pipeline calls Utopia's MCP `entity_facts` and `rule_matches` to read the twin (e.g. every patient currently typed `febrile_neutropenia` with the premise readings) and sends the alert through `/acs-email`. The rule engine becomes the shared definition and the SQL pipeline stops re-encoding the criterion.

## 4. PHI and security posture

Utopia was not written for PHI. Before any patient data:

1. **Encode at the boundary.** Every ingest path (API, document upload, mounted source) must call `ensure_encoded_mrn()` before the value reaches Utopia. Names go in Fernet-encrypted or not at all; the twin does not need a name to work.
2. **Restricted DB role on.** Set `UTOPIA_APP_DB_PASSWORD`, `UTOPIA_DATABASE_URL` as `utopia_app`, and `UTOPIA_MIGRATION_URL` as owner. This is the ledger immutability guarantee.
3. **`UTOPIA_SECRET_KEY` from Key Vault**, not from a file in the data directory, when it moves off a laptop.
4. **Per-base membership** is the access model (owner/admin/editor/viewer, restricted bases by invitation). Map it to the AIDI roles; do not create an open base for patient data.
5. **The LLM endpoint** is Azure OpenAI inside the KHCC tenant. Chunks of clinical notes go to it; that is the same exposure the existing AIDI extraction pipelines already accept.
6. Run `/aidi-unified-security` before the base holds a single real patient. Utopia's own `SECURITY.md` and `docs/utopia-pilot-security.md` list what it does and does not protect.

## 5. What the twin gives you that the warehouse does not

- **Point-in-time truth with provenance.** "What did we know about this patient's staging on the day we chose the regimen, and which report said so."
- **Contradiction surfacing.** Pathology says ER-negative, the oncology note says ER-positive: two facts on one functional predicate, the second closes the first, and the consistency check or a `fact_conflicts` row shows the seam. Today that disagreement is invisible in `SILVER_*` tables.
- **Criteria as rules with proofs**, retired when the evidence changes.
- **One conversational surface** over notes, registry and warehouse, with citations an auditor can follow.
- **Decision replay** (roadmap item): recording the tumour-board decision as an entity with its premises and replaying it later against what was known.

---

# Part E. Order of work

Estimated in engineering weeks for one person who knows the codebase.

| # | Cut | What it delivers | Effort |
|---|---|---|---|
| 1 | **ADR: coded datatype + `code_systems` / `codes` / `code_mappings` tables**, migration `0050`, loader for ICD-10 tabular XML and the ICD-11 MMS API | A `condition.code` fact stores a real ICD code; the pipeline cannot invent one | 2 weeks |
| 2 | **Clinical information model pack** (`fhir-lite.ttl`, ~40 classes) as a sixth embedded pack with disjointness and temporal declarations | A base can be created "medical" with one click; the consistency checker has axioms to work with | 1 week |
| 3 | **Identity rule for inverse-functional attributes** in entity resolution + hard gate in governance | Patients never merge by name | 1 week |
| 4 | **Fact-level API ingest** with coded objects and explicit intervals | Registry, labs and chemo rows enter as facts without an LLM | 1 week |
| 5 | **Assertion status qualifier** (negation, family history, hypothetical) in prompt and write path | Clinical NLP stops asserting what a note denied | 1 week |
| 6 | **Code retrieval in extraction** (embeddings on `codes`, second retrieval for coded predicates) | Free-text diagnoses in pathology reports land on ICD-10 codes with a coder queue for the rest | 2 weeks |
| 7 | **ICD-10 → ICD-11 as derived facts** over `code_mappings` | Dual coding for free, with proofs | 3 days |
| 8 | **Pilot base**: 50 breast-cancer patients, registry + pathology + chemo journey, three rules (FN, AKI stage 1, protocol eligibility), Databricks reads it over MCP | The first twin, measured against the existing AIDI alert pipelines | 2 weeks |

Cuts 1 to 3 are the foundation and should be one PR series on `dev` with three ADRs (`0039`, `0040`, `0041` in the decisions directory). Cuts 4 to 7 can proceed in parallel after cut 1. Cut 8 is the validation: compare Utopia's rule matches against the febrile-neutropenia and AKI worklists the current SQL pipelines produce, patient by patient, and log the disagreements as either a rule error or a data-entry finding. That comparison is the number that justifies the rest.

---

## Appendix: quick reference of the doctrines, one line each

- **No type is a type** (0009). **No relation is no relation** (0010). Empty is honest; a fallback class is a lie.
- **The ontology is a contract** (0012). Extraction obeys it; it does not suggest.
- **A mapping is not a fact** (0011). Warehouse alignment is configuration.
- **An unknown date is not an open one** (0022). `valid_to_precision = 'unknown'` exists for a reason.
- **The world axis reaches the second** (0024) and **an event holds at the moment it names** (0031).
- **The second clock can be rewound** (0019). Every read takes `held_at` and `as_of`.
- **A contradiction points upstream** (0017). A derivation that disagrees with the ledger is a signal, not a write.
- **Governance reads the ledger before it decides** (0025), **a decision records why** (0026), **an automatic merge is gated by what it can undo** (0027).
- **A rule reads attributes and concludes a type** (0021), **may say "or" once** (0029), **may read what a rule concluded** (0030), **computes what it concludes** (0032), and never aggregates.
- **An auditor reads it without us** (0020). Turtle and JSON-LD export, no SPARQL.
