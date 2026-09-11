#!/usr/bin/env python3
"""Build one synthetic patient's case file from khcc-aidi-synth-100-v1 and upload it to the local Utopia pilot.

Usage: scripts/pilot-patient.py <SYNTH_ID> [--no-upload | --per-note]
  --per-note pushes each note as its own document with doc_time = note Entry_Date (POST /kbs/{kb}/ingest),
  so extracted facts get the note date instead of the upload date.
Needs: server on 127.0.0.1:1516, a bearer token in data/pilot/.cookie (utopia_token=...), pandas.
"""
import json, pathlib, re, subprocess, sys
import pandas as pd

DATA = pathlib.Path.home() / "Library/CloudStorage/OneDrive-KingHusseinCancerCenter/AI/research/synth_cohort_v1/khcc-aidi-synth-100-v1/tables"
KB = "01a09191-1b2d-75c1-9f5b-82298c122577"   # KB "General" in the local pilot
BASE = "http://127.0.0.1:1516/api/v1"

GENERIC = re.compile(r"\b(?:the|this|our) patient\b", re.IGNORECASE)

def name_patient(text: str, sid: int) -> str:
    """Regenerated notes say "the patient"; the extractor then mints a generic node per note instead of
    linking to the patient entity. Naming the patient in the text makes exact-match resolution work."""
    return GENERIC.sub(f"Synthetic patient {sid:03d}", text)

def build(sid: int) -> pathlib.Path:
    pt = pd.read_csv(DATA / "VISTA_PATIENTS.csv"); pt = pt[pt.SYNTH_ID == sid].iloc[0]
    n = pd.read_csv(DATA / "VISTA_NOTES.csv"); n = n[n.SYNTH_ID == sid].copy()
    n["Entry_Date"] = pd.to_datetime(n.Entry_Date); n = n.sort_values("Entry_Date")
    al = pd.read_csv(DATA / "VISTA_PATIENT_ALLERGIES.csv"); al = al[al.SYNTH_ID == sid]
    dod = pt.DATE_OF_DEATH if pd.notna(pt.DATE_OF_DEATH) else "not recorded"
    out = [f"# Synthetic patient {sid:03d} — case file\n",
           f"Synthetic patient identifier: SYNTH-{sid:03d}. Sex: {pt.SEX}. Age: {pt.age_years} years. "
           f"Nationality: {pt.NATIONALITY}. Marital status: {pt.MARITAL_STATUS}. Date of death: {dod}.\n",
           "All dates are date-shifted; relative timing is preserved. All identifiers are synthetic.\n"]
    if len(al):
        out.append("## Allergies\n")
        out += ["- " + "; ".join(f"{k}: {v}" for k, v in r.drop("SYNTH_ID").dropna().items()) for _, r in al.iterrows()]
        out.append("")
    out.append(f"## Clinical notes ({len(n)} notes, chronological)\n")
    for _, r in n.iterrows():
        if pd.isna(r.Note) or not str(r.Note).strip():
            continue
        svc = r.AUTHOR_SERVICE if pd.notna(r.AUTHOR_SERVICE) else r.SERVICE
        out.append(f"### {r.Entry_Date:%Y-%m-%d %H:%M} — {r.DOCUMENT_TYPE} ({svc})\n")
        out.append(name_patient(str(r.Note).strip(), sid) + "\n")
    p = pathlib.Path("data/pilot") / f"synth_patient_{sid:03d}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out))
    print(f"built {p} ({p.stat().st_size} bytes, {len(n)} notes)")
    return p

def upload(p: pathlib.Path) -> None:
    tok = pathlib.Path("data/pilot/.cookie").read_text().strip().split("=", 1)[1]
    r = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {tok}", "-F", f"files=@{p}",
                        f"{BASE}/kbs/{KB}/documents", "-w", " [%{http_code}]"], capture_output=True, text=True)
    print(r.stdout[:300])

def push_per_note(sid: int) -> None:
    tok = pathlib.Path("data/pilot/.cookie").read_text().strip().split("=", 1)[1]
    pt = pd.read_csv(DATA / "VISTA_PATIENTS.csv"); pt = pt[pt.SYNTH_ID == sid].iloc[0]
    n = pd.read_csv(DATA / "VISTA_NOTES.csv"); n = n[n.SYNTH_ID == sid].copy()
    n["Entry_Date"] = pd.to_datetime(n.Entry_Date); n = n.sort_values("Entry_Date")
    head = (f"Synthetic patient {sid:03d}, {pt.SEX.lower()}, {pt.age_years} years. "
            "Dates are date-shifted; identifiers are synthetic.")
    created = 0
    for _, r in n.iterrows():
        if pd.isna(r.Note) or not str(r.Note).strip():
            continue
        svc = r.AUTHOR_SERVICE if pd.notna(r.AUTHOR_SERVICE) else r.SERVICE
        body = {"filename": f"SYNTH-{sid:03d}/{r.Entry_Date:%Y-%m-%d_%H%M}_{r.Document_Number}.txt",
                "doc_time": r.Entry_Date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "content": f"{head}\nNote type: {r.DOCUMENT_TYPE}. Service: {svc}. Note date: {r.Entry_Date:%Y-%m-%d}.\n\n{name_patient(str(r.Note).strip(), sid)}\n"}
        out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {tok}", "-H", "Content-Type: application/json",
                              "--data-binary", "@-", f"{BASE}/kbs/{KB}/ingest"], input=json.dumps(body), capture_output=True, text=True).stdout
        created += '"created"' in out
    print(f"pushed {created} notes for SYNTH-{sid:03d}")

if __name__ == "__main__":
    sid = int(sys.argv[1])
    if "--per-note" in sys.argv:
        push_per_note(sid)
    else:
        path = build(sid)
        if "--no-upload" not in sys.argv:
            upload(path)
