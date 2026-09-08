"""Verify the determinism gate by running the CLI twice and diffing the gated fields.

The assessment gates on this: for a fixed model digest, artifact, generation settings,
database, documents and input file, two fresh runs must agree on `id`, `status`,
`final_answer`, `sql`, `assumptions`, `repairs`, `citations`, and the presence of a review
packet -- compared after parsing and canonical JSON serialization. `explanation` wording
and trace timings are exempt.

Reasoning about it is not evidence, so this runs it. Both runs use a cold LM cache, so a
pass cannot be an artefact of the second run replaying the first's cached responses --
which would be the easy way to fake this.

    python scripts/check_determinism.py [--batch FILE]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GATED = ("id", "status", "final_answer", "sql", "assumptions", "repairs", "citations")
EXEMPT = ("explanation", "confidence")   # confidence is derived, but keep the diff readable


def canonical(rec: dict) -> str:
    """Canonical JSON of the gated fields, plus review-packet presence (not wording)."""
    subset = {k: rec.get(k) for k in GATED}
    subset["has_review_packet"] = rec.get("review_packet") is not None
    return json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def run(batch: Path, out: Path, cache_dir: Path) -> list[dict]:
    if cache_dir.exists():
        shutil.rmtree(cache_dir)            # cold cache: no replay of the other run
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "run_agent_hybrid.py",
         "--batch", str(batch), "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-2000:]); print(proc.stderr[-2000:])
        raise SystemExit(f"CLI failed with code {proc.returncode}")
    return [json.loads(l) for l in out.read_text().splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="sample_questions_hybrid_eval.jsonl")
    args = ap.parse_args()

    batch = ROOT / args.batch
    cache = ROOT / ".dspy_cache"
    a = run(batch, ROOT / "artifacts" / "determinism_run_a.jsonl", cache)
    b = run(batch, ROOT / "artifacts" / "determinism_run_b.jsonl", cache)

    if len(a) != len(b):
        raise SystemExit(f"different record counts: {len(a)} vs {len(b)}")

    mismatches = []
    for ra, rb in zip(a, b):
        ca, cb = canonical(ra), canonical(rb)
        if ca != cb:
            diffs = [f for f in GATED if ra.get(f) != rb.get(f)]
            if (ra.get("review_packet") is not None) != (rb.get("review_packet") is not None):
                diffs.append("review_packet presence")
            mismatches.append({"id": ra.get("id"), "fields": diffs,
                               "run_a": {f: ra.get(f) for f in diffs},
                               "run_b": {f: rb.get(f) for f in diffs}})

    report = {
        "batch": args.batch,
        "records": len(a),
        "gated_fields": list(GATED) + ["review_packet presence"],
        "exempt_fields": list(EXEMPT),
        "deterministic": not mismatches,
        "mismatches": mismatches,
        "explanation_differed_on": [ra["id"] for ra, rb in zip(a, b)
                                    if ra.get("explanation") != rb.get("explanation")],
    }
    (ROOT / "artifacts" / "determinism.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"records compared : {len(a)}")
    print(f"gated fields     : {', '.join(GATED)}, review_packet presence")
    if mismatches:
        print(f"RESULT           : NOT DETERMINISTIC ({len(mismatches)} record(s) differ)")
        for m in mismatches:
            print(f"  {m['id']}: {m['fields']}")
            for f in m["fields"]:
                print(f"      a={m['run_a'].get(f)!r}")
                print(f"      b={m['run_b'].get(f)!r}")
        raise SystemExit(1)
    print("RESULT           : DETERMINISTIC (both runs used a cold LM cache)")
    if report["explanation_differed_on"]:
        print(f"  note: `explanation` differed on {report['explanation_differed_on']}, "
              f"which the contract exempts")
    print("wrote artifacts/determinism.json")


if __name__ == "__main__":
    main()
