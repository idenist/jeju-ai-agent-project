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
from scripts import build_juneok_center_package, prepare_juneok_handoff_demo, prepare_juneok_review_demo, prepare_juneok_product_demo


def main():
    root = PROJECT_ROOT
    if prepare_juneok_handoff_demo.main() != 0:
        raise RuntimeError("시연 자료 재현 실패")
    prepare_juneok_review_demo.main()
    if prepare_juneok_product_demo.main() != 0: raise RuntimeError("기능 확장 시연 실패")
    build_juneok_center_package.main()
    product = json.loads((root / "artifacts/center-package/latest-package.json").read_text(encoding="utf-8"))
    product_zip = root / "artifacts/center-package" / product["zip"]
    demo = json.loads((root / "artifacts/handoff-demo/latest.json").read_text(encoding="utf-8"))
    with ZipFile(product_zip) as source:
        contents = {name: source.read(name) for name in source.namelist()}
    for path in Path(demo["output_dir"]).iterdir():
        if path.is_file() and path.suffix in {".pdf", ".md", ".json"}:
            contents["시연자료/" + path.name] = path.read_bytes()
    review_demo = json.loads((root / "artifacts/review-demo/latest.json").read_text(encoding="utf-8"))
    for path in Path(review_demo["output_dir"]).iterdir():
        if path.is_file() and path.suffix in {".pdf", ".md", ".json"}:
            contents["확장시연자료/" + path.name] = path.read_bytes()
    document_manifest = root / "artifacts/submission-documents/latest.json"
    product_demo = json.loads((root / "artifacts/product-demo/latest.json").read_text(encoding="utf-8"))
    for path in Path(product_demo["output_dir"]).iterdir():
        if path.is_file() and path.suffix in {".pdf", ".md", ".json"}:
            contents["기능확장시연자료/" + path.name] = path.read_bytes()
    if document_manifest.exists():
        documents = json.loads(document_manifest.read_text(encoding="utf-8"))
        for item in documents["documents"]:
            if hashlib.sha256((root / item["source"]).read_bytes()).hexdigest() != item["source_sha256"]:
                raise ValueError("제출 문서 PDF를 현재 원문으로 다시 생성하세요.")
            path = root / item["pdf"]
            contents["제출초안/" + path.name] = path.read_bytes()
    contents["팀인수_먼저읽기.md"] = (
        "# 감귤 Agent 팀 인수 후보\n\n"
        "먼저 [팀 인수 안내](docs/임준억/인수/팀인수안내.md)를 읽으세요.\n\n"
        "제품은 이 폴더에 바로 들어 있습니다. setup-center.cmd로 설치합니다. "
        "API 키는 포함하지 않았으며 .env.example을 참고합니다.\n\n"
        "시연자료/는 인터넷·실제 모델 호출 없이 재현한 가상 조건 PDF·MD·JSON과 검증 결과입니다. "
        "결과 JSON의 원래 PC 경로는 생성 당시 기록이며, 동봉 파일은 시연자료/에서 찾습니다. "
        "재생성은 python -m scripts.prepare_juneok_handoff_demo 명령으로 합니다.\n\n"
        "확장시연자료/에는 브리핑·판단 조건·복원 예시가 있습니다. 제출초안/이 있으면 개인 후보 기획서·결과 설명자료이며 팀 최종 합의본은 아닙니다.\n\n"
        "기능확장시연자료/에는 실제 거래규격 탐색·허용 감모/보관비 역산·저장 비교 변경 검토의 가상 예시가 있습니다.\n\n"
        "팀 채택·통합·사람 리허설·농가 검증은 완료되지 않았습니다. "
        "자동 검증 기록은 ZIP 바깥 latest.json의 verification 항목과 별도 로그에 있습니다.\n"
    ).encode("utf-8")
    keys = [v.encode() for k, v in dotenv_values(root / ".env", interpolate=False).items()
            if k in {"OPENAI_API_KEY", "DATA_GO_KR_KEY"} and v]
    for name, content in contents.items():
        if Path(name).suffix.lower() == '.json':
            from agents.juneok.settlement import reject_private_record
            reject_private_record(content)
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
    checks["four_extension_pdfs_and_case_included"] = len(list((extracted / "확장시연자료").glob("*.pdf"))) == 4 and (extracted / "확장시연자료/비교복원예시.json").exists()
    checks["product_expansion_pdfs_and_reference_included"] = len(list((extracted / "기능확장시연자료").glob("*.pdf"))) == 4 and (extracted / "기능확장시연자료/비교기준예시.json").exists()
    licenses = json.loads((extracted / "assets/licenses/python-manifest.json").read_text(encoding="utf-8"))
    checks["locked_dependency_notices_included"] = (
        hashlib.sha256((extracted / "requirements-juneok.lock").read_bytes()).hexdigest() == licenses["requirements_sha256"]
        and all(item["notices"] and all((extracted / notice["file"]).is_file()
                and hashlib.sha256((extracted / notice["file"]).read_bytes()).hexdigest() == notice["sha256"]
                for notice in item["notices"]) for item in licenses["packages"]))
    tmp = extracted / "artifacts/tmp"
    tmp.mkdir(parents=True)
    env = dict(os.environ, OPENAI_API_KEY="", DATA_GO_KR_KEY="", TEMP=str(tmp), TMP=str(tmp), PYTHONUTF8="1")
    steps = [("doctor", ["-m", "scripts.doctor_juneok"]),
             ("offline_demo", ["-m", "scripts.prepare_juneok_handoff_demo"]),
             ("review_demo", ["-m", "scripts.prepare_juneok_review_demo"]),
             ("product_demo", ["-m", "scripts.prepare_juneok_product_demo"]),
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
