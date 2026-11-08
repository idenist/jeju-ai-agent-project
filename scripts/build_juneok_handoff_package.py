"""Build and rehearse a local team handoff; never send it externally."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from dotenv import dotenv_values
from agents.juneok.config import PROJECT_ROOT
from scripts import build_juneok_center_package, prepare_juneok_handoff_demo


def main():
    root = PROJECT_ROOT
    if prepare_juneok_handoff_demo.main() != 0:
        raise RuntimeError("시연 자료 재현 실패")
    build_juneok_center_package.main()
    product = json.loads((root / "artifacts/center-package/latest-package.json").read_text(encoding="utf-8"))
    product_zip = root / "artifacts/center-package" / product["zip"]
    demo = json.loads((root / "artifacts/handoff-demo/latest.json").read_text(encoding="utf-8"))
    with ZipFile(product_zip) as source:
        contents = {name: source.read(name) for name in source.namelist()}
    for path in Path(demo["output_dir"]).iterdir():
        if path.is_file() and path.suffix in {".pdf", ".md", ".json"}:
            contents["시연자료/" + path.name] = path.read_bytes()
    contents["팀인수_먼저읽기.md"] = (
        "# 감귤 Agent 팀 인수 후보\n\n"
        "먼저 [팀 인수 안내](docs/임준억/인수/팀인수안내.md)를 읽으세요.\n\n"
        "제품은 이 폴더에 바로 들어 있습니다. setup-center.cmd로 설치합니다. "
        "API 키는 포함하지 않았으며 .env.example을 참고합니다.\n\n"
        "시연자료/는 인터넷·실제 모델 호출 없이 재현한 가상 조건 PDF·MD·JSON과 검증 결과입니다. "
        "결과 JSON의 원래 PC 경로는 생성 당시 기록이며, 동봉 파일은 시연자료/에서 찾습니다. "
        "재생성은 python -m scripts.prepare_juneok_handoff_demo 명령으로 합니다.\n\n"
        "팀 채택·통합·사람 리허설·농가 검증은 완료되지 않았습니다. "
        "자동 검증 기록은 ZIP 바깥 latest.json의 verification 항목과 별도 로그에 있습니다.\n"
    ).encode("utf-8")
    keys = [v.encode() for k, v in dotenv_values(root / ".env", interpolate=False).items()
            if k in {"OPENAI_API_KEY", "DATA_GO_KR_KEY"} and v]
    for name, content in contents.items():
        if Path(name).name == ".env" or any(key in content for key in keys):
            raise ValueError("비밀정보 검사 실패: 패키지를 작성하지 않습니다.")
    manifest = {"scope": "LOCAL_TEAM_HANDOFF_CANDIDATE_NOT_TEAM_ACCEPTANCE",
                "source_product_zip": product_zip.name,
                "source_product_sha256": hashlib.sha256(product_zip.read_bytes()).hexdigest(),
                "demo_scope": demo["scope"], "demo_checks_passed": demo["passed"],
                "configured_secrets_included": False,
                "files_sha256": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}}
    contents["HANDOFF_MANIFEST.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    folder = root / "artifacts/handoff-package"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / datetime.now(timezone(timedelta(hours=9))).strftime("감귤Agent_팀인수_%Y%m%dT%H%M%S.zip")
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        for name, content in contents.items():
            output.writestr(name, content)
    verification = verify(archive)
    result = {"zip": str(archive), "files": len(contents), "configured_secrets_included": False,
              "verification": verification, "passed": verification["passed"]}
    (folder / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def verify(archive):
    folder = PROJECT_ROOT / "artifacts/handoff-package-smoke" / uuid4().hex[:12]
    extracted = folder / "product"
    extracted.mkdir(parents=True)
    with ZipFile(archive) as bundle:
        names = bundle.namelist()
        for name in names:
            if not (extracted / name).resolve().is_relative_to(extracted.resolve()):
                raise ValueError("패키지 경로가 대상 폴더를 벗어납니다.")
        bundle.extractall(extracted)
    manifest = json.loads((extracted / "HANDOFF_MANIFEST.json").read_text(encoding="utf-8"))
    checks = {"all_file_hashes_match": all(hashlib.sha256((extracted / name).read_bytes()).hexdigest() == value
                                           for name, value in manifest["files_sha256"].items()),
              "credentials_and_environments_excluded": not any(Path(n).name == ".env" or ".venv" in Path(n).parts for n in names),
              "four_sample_pdfs_included": len(list((extracted / "시연자료").glob("*.pdf"))) == 4}
    tmp = extracted / "artifacts/tmp"
    tmp.mkdir(parents=True)
    env = dict(os.environ, OPENAI_API_KEY="", DATA_GO_KR_KEY="", TEMP=str(tmp), TMP=str(tmp), PYTHONUTF8="1")
    steps = [("doctor", ["-m", "scripts.doctor_juneok"]),
             ("offline_demo", ["-m", "scripts.prepare_juneok_handoff_demo"]),
             ("regression", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_juneok_*.py", "-v"]),
             ("published_spec", ["-m", "scripts.verify_juneok_center_spec"])]
    count = None
    for name, args in steps:
        run = subprocess.run([sys.executable, "-X", "utf8", *args], cwd=extracted, env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=180)
        (folder / (name + ".stdout.log")).write_text(run.stdout, encoding="utf-8")
        (folder / (name + ".stderr.log")).write_text(run.stderr, encoding="utf-8")
        checks[name] = run.returncode == 0
        if name == "regression":
            matched = re.search(r"Ran (\d+) tests", run.stderr)
            count = int(matched[1]) if matched else None
    result = {"scope": "LOCAL_EXTRACTED_HANDOFF_KEYLESS_AUTOMATED_NOT_HUMAN_REHEARSAL",
              "extracted": str(extracted), "python": sys.executable, "checks": checks,
              "tests_run": count, "live_model_called": False, "passed": all(checks.values())}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
