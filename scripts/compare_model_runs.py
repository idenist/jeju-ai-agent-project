"""Compare two FULL paid model E2E runs, refusing mismatched cases or source data.

Offline operation. Does not call a model or estimate API charges.
Usage: python -m scripts.compare_model_runs BEFORE_FOLDER AFTER_FOLDER
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def load_run(folder):
    folder = Path(folder)
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    return metadata, summary


def compare_runs(before, after):
    old_meta, old = before
    new_meta, new = after
    for key in ("model", "cases", "repeats", "expected_turns_per_repeat"):
        if old_meta.get(key) != new_meta.get(key):
            raise ValueError(f"Incomparable model evaluation metadata: {key}")
    if not old_meta.get("cases"):
        raise ValueError("Missing evaluation cases")
    for key in ("planned_runs",):
        if old.get(key) != new.get(key):
            raise ValueError(f"Incomparable evaluation totals: {key}")
    if old.get("complete") is not True or new.get("complete") is not True:
        raise ValueError("Both evaluations must be complete")
    if old.get("planned_runs") != len(old_meta["cases"]) * old_meta["repeats"]:
        raise ValueError("Inconsistent evaluation count")
    old_hashes, new_hashes = old_meta.get("source_sha256"), new_meta.get("source_sha256")
    if not isinstance(old_hashes, dict) or not isinstance(new_hashes, dict):
        raise ValueError("Missing source hashes")
    evidence_paths = {path for path in old_hashes if path.startswith("data/") or path.startswith("evals/")}
    if not evidence_paths or any(old_hashes[path] != new_hashes.get(path) for path in evidence_paths):
        raise ValueError("Different evaluation dataset or data files")
    a,b = old.get("pass_rate"), new.get("pass_rate")
    if not isinstance(a, (float,int)) or not isinstance(b, (float,int)):
        raise ValueError("Missing pass rates")
    return {"model": old_meta["model"], "cases": len(old_meta["cases"]),
            "before_pass_rate": a, "after_pass_rate": b,
            "pass_rate_change_percentage_points": round((b-a)*100,2),
            "before_usage": old.get("usage"), "after_usage": new.get("usage"),
            "before_request_footprint": old.get("request_footprint"),
            "after_request_footprint": new.get("request_footprint"),
            "note": "Observational comparison, not proof of causality; same model and source data required."}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before",type=Path)
    parser.add_argument("after",type=Path)
    args=parser.parse_args(argv)
    try:
        result=compare_runs(load_run(args.before),load_run(args.after))
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
