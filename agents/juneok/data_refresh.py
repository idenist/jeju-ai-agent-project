"""Stage, validate and activate market snapshots without editing shared datasets."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .config import PROJECT_ROOT


def kst_today():
    return datetime.now(timezone(timedelta(hours=9))).date()


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


def market_status(root: Path = PROJECT_ROOT, today=None) -> dict:
    today = today or kst_today()
    selected = resolve_market_root(root)
    if selected != Path(root):
        manifest = json.loads((selected / "manifest.json").read_text(encoding="utf-8"))
        dates = manifest["observed_dates"]
        name = manifest["snapshot_id"]
        collection = manifest.get("collection", {})
    else:
        report = json.loads((Path(root) / "data/reports/auction_quality_report.json").read_text(encoding="utf-8"))
        dates = report["observed_dates"]
        name = "shared-baseline"
        collection = {}
    from datetime import date
    last = max(dates) if dates else None
    return {"snapshot_id": name, "observed_first_date": min(dates) if dates else None,
            "observed_last_date": last, "checked_on_kst": today.isoformat(),
            "calendar_days_since_last_observation": (today - date.fromisoformat(last)).days if last else None,
            "is_live_price": False, "source_update_checked": bool(collection),
            "queried_through": max(collection.get("queried_dates", []), default=None)}


def stage_snapshot(inputs: list[Path], name: str, root: Path = PROJECT_ROOT, today=None, collection=None) -> dict:
    from scripts.preprocess_auction import run
    from .evidence import _market_rows
    today = today or kst_today()
    staged = snapshot_dir(root, name)
    if staged.exists():
        raise ValueError("이미 존재하는 자료 버전입니다. 새 이름을 사용하세요.")
    if not inputs or any(not Path(p).is_file() or Path(p).suffix.lower() != ".csv" for p in inputs):
        raise ValueError("원본 CSV 파일을 지정하세요.")
    staged.mkdir(parents=True)
    raw = staged / "data/raw"
    raw.mkdir(parents=True)
    copied = []
    for i, source in enumerate(inputs):
        target = raw / f"auction_{i}.csv"
        shutil.copyfile(source, target)
        copied.append(target)
    report = run(copied, staged / "data/processed", staged / "data/reports")
    rows = _market_rows(staged)
    dates = sorted({row["date"] for row in rows})
    if not dates or max(dates) > today.isoformat():
        raise ValueError("유효한 관측이 없거나 미래 날짜가 포함됐습니다. 활성화하지 않았습니다.")
    files = ["data/processed/daily_prices.csv", "data/processed/daily_variety_prices.csv", "data/reports/auction_quality_report.json"]
    manifest = {"status": "VALIDATED", "snapshot_id": name, "checked_on_kst": today.isoformat(),
        "source_url": "https://www.data.go.kr/data/15141810/openapi.do", "observed_dates": dates,
        "raw_files": {p.relative_to(staged).as_posix(): digest(p) for p in copied},
        "files": {p: digest(staged / p) for p in files},
        "accepted_rows": report["accepted_rows"], "rejected_rows": report["rejected_rows"],
        "license_status": "원출처 이용조건 확인 필요. 새로 수집했다는 사실만으로 이용허락을 확정하지 않음.",
        "is_live_price": False}
    if collection:
        manifest["collection"] = collection
    (staged / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def activate_snapshot(name: str, root: Path = PROJECT_ROOT, today=None) -> dict:
    today = today or kst_today()
    staged = snapshot_dir(root, name)
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "VALIDATED" or manifest.get("snapshot_id") != name:
        raise ValueError("검증된 버전만 선택할 수 있습니다.")
    files = {"data/processed/daily_prices.csv", "data/processed/daily_variety_prices.csv", "data/reports/auction_quality_report.json"}
    if set(manifest["files"]) != files or any(digest(staged / p) != v for p, v in manifest["files"].items()):
        raise ValueError("검증 후 파일이 변경됐습니다.")
    # Re-read actual rows; do not trust manifest date claims at activation time.
    from .evidence import _market_rows
    dates = sorted({r["date"] for r in _market_rows(staged)})
    if dates != manifest["observed_dates"] or not dates or max(dates) > today.isoformat():
        raise ValueError("관측 날짜가 명세와 다르거나 미래 날짜입니다.")
    previous = market_status(root, today)
    if previous["observed_last_date"] and max(dates) <= previous["observed_last_date"]:
        raise ValueError("기존보다 최신 관측이 없습니다. 재현용 버전으로 보존하고 교체하지 않습니다.")
    pointer = Path(root) / "artifacts/market-active.json"
    if pointer.exists():
        (pointer.parent / "market-active-previous.json").write_bytes(pointer.read_bytes())
    payload = {"snapshot_id": name, "manifest_sha256": digest(manifest_path)}
    temporary = pointer.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(pointer)
    return market_status(root, today)
