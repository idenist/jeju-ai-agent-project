"""Verify an extracted package without Git, credentials, or a physical center PC."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile

from agents.juneok.config import PROJECT_ROOT


def main():
    metadata = json.loads((PROJECT_ROOT / "artifacts/center-package/latest-package.json").read_text(encoding="utf-8"))
    folder = PROJECT_ROOT / "artifacts/package-shared-smoke" / uuid4().hex[:12]
    extracted = folder / "product"
    extracted.mkdir(parents=True)
    archive = PROJECT_ROOT / "artifacts/center-package" / metadata["zip"]
    checks = {}
    with ZipFile(archive) as zipfile:
        names = zipfile.namelist()
        for name in names:
            if not (extracted / name).resolve().is_relative_to(extracted.resolve()):
                raise ValueError("패키지 경로가 대상 폴더를 벗어납니다.")
        checks["credentials_and_environments_excluded"] = not any(
            Path(name).name == ".env" or ".venv" in Path(name).parts for name in names)
        checks["fonts_license_and_evals_included"] = all(name in names for name in [
            "assets/fonts/NanumGothic-Regular.ttf", "assets/fonts/NanumGothic-Bold.ttf", "assets/fonts/OFL.txt"])
        checks["fonts_license_and_evals_included"] &= any(n.startswith("evals/") for n in names)
        zipfile.extractall(extracted)
    manifest = json.loads((extracted / "CENTER_PACKAGE_MANIFEST.json").read_text(encoding="utf-8"))
    checks["all_packaged_files_match_hashes"] = all(
        hashlib.sha256((extracted / path).read_bytes()).hexdigest() == expected
        for path, expected in manifest["files_sha256"].items())
    tmp = extracted / "artifacts/tmp"
    tmp.mkdir(parents=True)
    env = dict(os.environ, OPENAI_API_KEY="", DATA_GO_KR_KEY="", TEMP=str(tmp), TMP=str(tmp), PYTHONUTF8="1")
    steps = [
        ("doctor", ["-m", "scripts.doctor_juneok"]),
        ("tests", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_juneok_*.py", "-v"]),
        ("published_spec", ["-m", "scripts.verify_juneok_center_spec"]),
    ]
    test_count = None
    for name, args in steps:
        result = subprocess.run([sys.executable, "-X", "utf8", *args], cwd=extracted, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=180)
        (folder / (name + ".stdout.log")).write_text(result.stdout, encoding="utf-8")
        (folder / (name + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
        checks[name] = result.returncode == 0
        if name == "tests":
            matched = re.search(r"Ran (\d+) tests", result.stderr)
            test_count = int(matched[1]) if matched else None
    report = {"scope": "LOCAL_EXTRACTED_PACKAGE_USING_CLEAN_ENV_NOT_PHYSICAL_CENTER", "zip": str(archive),
              "extracted": str(extracted), "python": sys.executable, "checks": checks,
              "tests_run": test_count, "live_model_called": False, "passed": all(checks.values())}
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROJECT_ROOT / "artifacts/package-shared-latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
