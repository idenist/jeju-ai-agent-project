"""Validated market-snapshot pointer and file integrity; no evidence or UI imports."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from core.config import PROJECT_ROOT


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot_dir(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("자료 버전 이름은 영문·숫자·밑줄·하이픈 1~64자입니다.")
    return Path(root) / "artifacts/data-refresh" / name


def resolve_market_root(root: Path = PROJECT_ROOT) -> Path:
    root = Path(root)
    pointer = root / "artifacts/market-active.json"
    if not pointer.exists():
        return root
    active = json.loads(pointer.read_text(encoding="utf-8"))
    staged = snapshot_dir(root, active["snapshot_id"])
    manifest_path = staged / "manifest.json"
    if digest(manifest_path) != active["manifest_sha256"]:
        raise ValueError("선택한 가격자료의 명세 해시가 다릅니다.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "VALIDATED":
        raise ValueError("검증되지 않은 가격자료입니다.")
    expected_files = {"data/processed/daily_prices.csv", "data/processed/daily_variety_prices.csv", "data/reports/auction_quality_report.json"}
    if set(manifest["files"]) != expected_files or any(digest(staged / p) != value for p, value in manifest["files"].items()):
        raise ValueError("선택한 가격자료 파일이 변경되거나 누락됐습니다.")
    return staged
