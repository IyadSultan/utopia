# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Utopia: a bitemporal knowledge graph with an ontology layer, shipped as one Rust binary plus one Postgres (pgvector). Full-text search (Tantivy) lives in the binary, vectors in pgvector, the job queue is a Postgres table. Frontend is a Vite/React SPA in `web/`, served by the binary in production.

## Commands

```bash
# Local dev (three terminals)
docker compose up -d db                 # Postgres+pgvector on 127.0.0.1:1517 (not 5432)
cargo run -p utopia-server              # :1516, runs migrations on startup
cd web && pnpm install && pnpm dev      # :5173, proxies /api to :1516

# Exactly what CI runs
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cd web && pnpm install --frozen-lockfile && pnpm test && pnpm build   # build = style-guard + tsc + vite

# Single tests
cargo test -p utopia-store --test a_fact_awaits_a_nod        # one integration test file
cargo test -p utopia-server api::mcp::tests                  # one inline test module
cargo test -p utopia-reason some_fn_name                      # by name substring
cd web && pnpm vitest run src/theme.test.ts                   # one vitest file
cd web && pnpm guard                                          # style guard only

# End-to-end
./scripts/smoke.sh [BASE_URL]           # register → workspace → kb → jobs against a running server
./scripts/e2e_type_drift.sh             # isolated DB + port, type-drift scenario
```

Config is env vars with the `UTOPIA_` prefix; copy `.env.example` to `.env`. Never re-run `sqlx migrate` by hand in dev; the server does it.

### Database-backed tests silently skip

Most `crates/utopia-store/tests/*.rs` and some server tests start with `let Some(url) = utopia_store::test_db::url() else { return Ok(()) };`. Without `UTOPIA_DATABASE_URL` they pass green without running. When you touch SQL or anything under `utopia-store`:

```bash
export UTOPIA_DATABASE_URL=postgres://utopia:utopia@localhost:1517/utopia
export UTOPIA_TEST_REQUIRE_DB=1        # turns a skip into a failure, as CI's migrations job does
cargo test -p utopia-store
```

New DB tests must use `test_db::url()`, never read the env var directly. Tests run against the shared dev DB and clean up their own fixture rows (see any test's `cleanup`/`teardown`).

## Architecture

### Crate chain

| crate | role |
|---|---|
| `utopia-core` | `AppConfig` (figment, `UTOPIA_*`), `models.rs` domain types, `error.rs` (`AppError::invalid` etc.), `secrets.rs` AES-GCM sealing of credentials at rest |
| `utopia-store` | All SQL. One module per domain (`graph`, `ontology`, `reasoning`, `jobs`, `review`, `resolution`, `governance`, …). `db::migrate` runs `sqlx::migrate!("../../migrations")`. Nothing above this crate writes SQL. |
| `utopia-ingest` | Parsers (PDF/DOCX/XLSX/HTML/…), chunker (1200 chars, overlap 150), RDF/OWL ontology projection (`ontology_rdf.rs`) |
| `utopia-extract` | LLM prompt building and response parsing for entity/fact extraction; `governor.rs` is the agentic adjudicator loop |
| `utopia-reason` | Pure, in-memory: axiom checking (`ontology.rs`, `lib.rs`), forward-chaining derivation (`derive.rs`), attribute rules (`rules.rs`). Store's `reasoning::materialize` feeds it and persists results. |
| `utopia-search` | Tantivy index (with jieba) over documents; rebuilt from DB if empty at startup |
| `utopia-llm` | OpenAI-compatible chat/embedding client |
| `utopia-server` | axum binary: `api/` routes (`api/mod.rs::router`), pipeline orchestration, connectors (GitHub/Jira/Notion/RSS/WebDAV/S3), `query_engine/` (Postgres/MySQL/Trino/Databricks/Snowflake behind one trait), MCP endpoint (`api/mcp.rs`), RDF export (`rdf.rs`), five embedded ontology packs in `packs/` |

### Everything async goes through the job table

`utopia_store::jobs` is the queue: `enqueue*` inserts a row and `NOTIFY`s; `run_worker` claims with `FOR UPDATE SKIP LOCKED`, backs off on failure, gives up after `max_attempts` (terminal errors do not spend the budget). `main.rs::dispatch` is the single switch on `job.kind`: `process_document`, `extract_document`, `resolve_types`, `adjudicate_entities`, `govern`, `materialize_inferences`, `sync_source`, `build_vector_index`, `explore_mappings`, … Adding a background task means a new arm there plus an `enqueue` call, nothing else.

### Document pipeline (read `docs/pipeline.md` first)

Upload → parse → chunk → embed (document is `ready`, searchable) → extract per chunk (one LLM call, ontology in the prompt, signature check may swap direction or blank the predicate) → entity resolution (exact/alias → embedding → batched LLM adjudication) → type resolution → ontology growth proposals → consistency check (writes nothing) → optional materialized inference (derived facts stored separately, asserted facts win). `docs/pipeline.md` lists every point where something is dropped and which table records it (`extraction_drops`).

### Two clocks, and nothing is overwritten

Every fact carries a world interval (`valid_from`/`valid_to`, precision year→second, `world_axis.rs`) and a record interval (`record_axis.rs`). Corrections close the old row and insert a new one linked to it; merges are recorded in `entity_merges` and can be unwound; deletions and purges are events. Every graph read accepts `held_at` / `as_of`. Human and agent decisions go to an append-only ledger (`audit.rs`, `agent_decisions`) that migration `0010` protects with a restricted DB role. Do not add an `UPDATE` that rewrites history; add a superseding row.

### Ontology is a contract

Extraction only writes types/predicates that exist in the KB's ontology; out-of-vocabulary terms become counted proposals (`ontology_proposals`), adopted by counting (`MIN_DOCS`, `MIN_SIGNALS`) or by a person. `type_id` and `predicate_id` are nullable on purpose: "no type" and "no relation" are the honest states, not a fallback class (ADRs 0009, 0010).

### Frontend

`web/src/api.ts` is the only HTTP client; `pages/` are route components (`router.tsx`), `ui/` holds the only allowed controls (`Button`, `Input`, `Dropdown`, `SearchSelect`, `Dialog`, …). Live updates come over SSE via `useKbEvents`. Locale is resolved once in `i18n/index.ts` and consumed as `S.…` everywhere. Entity colours in `palette.ts` must stay byte-identical to `crates/utopia-store/src/palette.rs`.

## Conventions review enforces

- **Branch off `dev`, PR base `dev`.** `main` is release-only. Commit with `-s` (DCO). Message: one English sentence stating the motivation, no body; skim `git log` for the register.
- **Migrations**: `migrations/NNNN_short_english_sentence.sql`, roll forward only, never edited after merge. Check the latest number on `dev`/`main` before adding one; CI fails on duplicate numbers and runs the whole set twice on a fresh DB.
- **Comments explain why, in Chinese.** Code comments are Chinese and dense, recording the trap that motivated the code. UI strings, README, ADRs are English. A comment that restates the code will be sent back.
- **UI strings go through i18n**: add to both `web/src/i18n/en.ts` and `zh.ts`.
- **`web/DESIGN.md` is enforced by `web/scripts/style-guard.mjs`** (runs in `pnpm build`): named type sizes (`text-body`, not `text-sm`), six spacing steps, four role-named radii, colour tokens only (no hex/rgba in `.ts`/`.tsx`), no raw `<button>`/`<input>`/`<select>`, no `hover:`/`transition` in pages. Read it before touching a page.
- **Design changes get an ADR** in `docs/decisions/NNNN-title.md` before code, for anything touching the data model, ontology contract or public API. The implementing PR updates the record's status line. `docs/` root is git-ignored scratch; only `docs/decisions/` and `docs/pipeline.md` are tracked.
- **Every GitHub workflow declares `permissions:`** (repo default is read-write).
- **Test file names are sentences** (`a_purge_is_final.rs`, `the_second_clock_can_be_rewound.rs`), matching the ADR they guard. Follow that.
- **Benchmarks** (`scripts/bench/`): one fresh KB per run, never reuse a DB between rounds; see its README.

## Code navigation

The repo is indexed by graft (`.cursor/rules/graft.mdc`, hooks in `.claude/settings.json`). Prefer `graft ask "<question>" --source`, `graft skeleton <file>`, `graft callers <symbol>` over grepping; run `graft build` after large changes.
