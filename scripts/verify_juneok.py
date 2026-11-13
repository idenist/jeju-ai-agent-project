"""Repeatable offline checks, with all logs and temporary files in artifacts/."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    artifacts = ROOT / "artifacts"
    (artifacts / "tmp").mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(artifacts / "tmp")
    manifest = json.loads((ROOT / "data/metadata/juneok_manifest.json").read_text(encoding="utf-8"))
    mismatches = [row["file"] for row in manifest["files"] if hashlib.sha256((ROOT / row["file"]).read_bytes()).hexdigest() != row["sha256"]]
    with (artifacts / "unittest.log").open("w", encoding="utf-8") as stream:
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_juneok_*.py", top_level_dir=str(ROOT / "tests"))
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    ignore = subprocess.run(["git", "check-ignore", ".env", ".venv", "artifacts/tmp"], cwd=ROOT, capture_output=True, text=True)
    source_check = subprocess.run(["git", "diff", "--quiet", "--", "data/raw", "data/processed", "data/reports", "scripts/preprocess_auction.py", "scripts/preprocess_shipments.py"], cwd=ROOT)
    expected_ignored = {".env", ".venv", "artifacts/tmp"}
    summary = {"python": sys.version, "python_executable": sys.executable, "environment_prefix": sys.prefix,
               "packages": {x: importlib.metadata.version(x) for x in ["openai", "python-dotenv", "streamlit"]},
               "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "data_manifest_mismatches": mismatches, "common_data_and_preprocessing_unchanged": source_check.returncode == 0,
               "env_venv_artifacts_ignored": expected_ignored.issubset(set(ignore.stdout.splitlines())),
               "live_model_called": False, "evidence_log": "artifacts/unittest.log"}
    (artifacts / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() and not mismatches and source_check.returncode == 0 and summary["env_venv_artifacts_ignored"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
