"""Create a reviewable CSV-runtime package, excluding credentials and environments."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zipfile import ZipFile, ZIP_DEFLATED
from dotenv import dotenv_values
from agents.juneok.config import PROJECT_ROOT
from agents.juneok.data_refresh import resolve_market_root, market_status


def main():
    root = PROJECT_ROOT
    files = {root / p for p in ["app.py", "README.md", "requirements.txt", "requirements-juneok.txt",
        "requirements-juneok.lock", ".env.example", "run-juneok.cmd", "setup-center.cmd"]}
    for folder in ["agents", "scripts", "tests", "evals", "docs", "data/raw", "data/processed", "data/reports", "data/metadata"]:
        files.update(p for p in (root / folder).rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".csv", ".json"}
                     and "__pycache__" not in p.parts)
    selected = resolve_market_root()
    files.update(p for p in (root / "assets/fonts").rglob("*") if p.is_file() and p.suffix.lower() in {".ttf", ".txt", ".json"})
    from scripts import build_juneok_notices
    build_juneok_notices.main()
    # Include full supplied license texts, including extensionless LICENSE/NOTICE.
    files.update(p for p in (root / "assets/licenses").rglob("*") if p.is_file())
    files.add(root / "docs/임준억/제출/외부라이선스목록.md")
    spec_path = root / "artifacts/center-spec/result.json"
    if spec_path.exists():
        files.add(spec_path)
    if selected != root:
        manifest = json.loads((selected / "manifest.json").read_text(encoding="utf-8"))
        if any(hashlib.sha256((selected / p).read_bytes()).hexdigest() != value for p, value in manifest["raw_files"].items()):
            raise ValueError("Snapshot raw input changed; package was not written")
        files.update(selected / p for p in [*manifest["raw_files"], *manifest["files"], "manifest.json"])
        files.add(root / "artifacts/market-active.json")
    secrets = dotenv_values(root / ".env", interpolate=False)
    keys = [v.encode() for k, v in secrets.items() if k in {"OPENAI_API_KEY", "DATA_GO_KR_KEY"} and v]
    contents = {}
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        if path.name.startswith(".env") and path.name != ".env.example":
            raise ValueError("Credential file is not packageable")
        content = path.read_bytes()
        if any(key in content for key in keys):
            raise ValueError("Configured secret detected; package was not written")
        contents[relative] = content
    metadata = {"package_type": "CSV_RUNTIME", "market_snapshot": market_status(),
                "data_api_required_at_runtime": False, "llm_api_required_for_natural_language": True,
                "configured_secrets_included": False, "center_pc_verified": False,
                "files_sha256": {p: hashlib.sha256(b).hexdigest() for p, b in contents.items()}}
    if spec_path.exists():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        metadata["published_spec_assessment"] = spec["assessment"]
        metadata["published_spec_scope"] = spec["scope"]
    contents["CENTER_PACKAGE_MANIFEST.json"] = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
    folder = root / "artifacts/center-package"
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone(timedelta(hours=9))).strftime("jeju-agent-csv-%Y%m%dT%H%M%S.zip")
    with ZipFile(folder / name, "w", ZIP_DEFLATED) as archive:
        for p, b in contents.items():
            archive.writestr(p, b)
    (folder / "latest-package.json").write_text(json.dumps({"zip": name, **metadata}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"zip": str(folder / name), "files": len(contents), "configured_secrets_included": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
