"""Validated market-snapshot pointer and file integrity; no evidence or UI imports."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from core.config import PROJECT_ROOT


BASELINE_ID = "shared-baseline"
MARKET_POINTER_FILE = Path("artifacts/market-active.json")
DAILY_FILES = {
    "data/processed/daily_prices.csv", "data/processed/daily_variety_prices.csv",
    "data/reports/auction_quality_report.json",
}
COMPARISON_FILES = {
    "data/processed/price_comparisons.csv", "data/processed/price_comparison_availability.csv",
    "data/reports/price_comparison_report.json",
}
SNAPSHOT_FILES = DAILY_FILES | COMPARISON_FILES


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot_dir(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("자료 버전 이름은 영문·숫자·밑줄·하이픈 1~64자입니다.")
    return Path(root) / "artifacts/data-refresh" / name


def read_market_pointer(root: Path) -> dict:
    try:
        pointer = json.loads((Path(root) / MARKET_POINTER_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"snapshot_id": BASELINE_ID, "manifest_sha256": None}
    if not isinstance(pointer, dict):
        raise ValueError("선택한 가격자료의 버전 명세를 읽을 수 없습니다.")
    return pointer


def load_snapshot(staged: Path, name: str, manifest_sha256=None) -> dict:
    manifest_path = staged / "manifest.json"
    raw = manifest_path.read_bytes()
    if manifest_sha256 is not None and hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("선택한 가격자료의 명세 해시가 다릅니다.")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get("status") != "VALIDATED"
            or manifest.get("snapshot_id") != name):
        raise ValueError("검증되지 않은 가격자료입니다.")
    files = manifest["files"]
    if (not isinstance(files, dict) or set(files) not in (DAILY_FILES, SNAPSHOT_FILES)
            or any(digest(staged / p) != sha for p, sha in files.items())):
        raise ValueError("선택한 가격자료 파일이 변경되거나 누락됐습니다.")
    return manifest


def resolve_snapshot(root: Path, reference: dict) -> Path:
    name = reference["snapshot_id"]
    sha = reference["manifest_sha256"]
    if name == BASELINE_ID and sha is None:
        return root
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("선택한 가격자료의 명세 해시가 올바르지 않습니다.")
    staged = snapshot_dir(root, name)
    load_snapshot(staged, name, sha)
    return staged


def resolve_market_root(root: Path = PROJECT_ROOT) -> Path:
    root = Path(root)
    return resolve_snapshot(root, read_market_pointer(root))
