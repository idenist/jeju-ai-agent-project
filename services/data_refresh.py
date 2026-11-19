"""Stage, validate and activate market snapshots without editing shared datasets."""
from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from core.config import PROJECT_ROOT
from services.market_snapshot import (BASELINE_ID, DAILY_FILES, MARKET_POINTER_FILE, SNAPSHOT_FILES,
                                      digest, load_snapshot, read_market_pointer, resolve_market_root,
                                      resolve_snapshot, snapshot_dir)

PAIR_INTERVALS = (3, 5, 7, 10, 14)


def kst_today():
    return datetime.now(timezone(timedelta(hours=9))).date()


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
        name = BASELINE_ID
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
    from services.evidence import _market_rows
    today = today or kst_today()
    if name == BASELINE_ID:
        raise ValueError("기본 자료 이름은 새 버전에 사용할 수 없습니다.")
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
    manifest = {"status": "VALIDATED", "snapshot_id": name, "checked_on_kst": today.isoformat(),
        "source_url": "https://www.data.go.kr/data/15141810/openapi.do", "observed_dates": dates,
        "raw_files": {p.relative_to(staged).as_posix(): digest(p) for p in copied},
        "accepted_rows": report["accepted_rows"], "rejected_rows": report["rejected_rows"],
        "license_status": "원출처 이용조건 확인 필요. 새로 수집했다는 사실만으로 이용허락을 확정하지 않음.",
        "is_live_price": False}
    if collection:
        manifest["collection"] = collection
    return _finish_snapshot(staged, manifest)


def _finish_snapshot(staged, manifest):
    from scripts.build_price_comparisons import run

    processed = staged / "data/processed"
    pair_report = run(processed / "daily_prices.csv", processed, staged / "data/reports", PAIR_INTERVALS)
    manifest.update(files={p: digest(staged / p) for p in sorted(SNAPSHOT_FILES)},
                    comparison_intervals=pair_report["interval_days"])
    (staged / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def stage_baseline_extension(name: str, root: Path = PROJECT_ROOT, today=None) -> dict:
    """Build an interval extension from the verified shared price source."""
    from services.data_contract import verify_integrated_price_sources

    root = Path(root)
    today = today or kst_today()
    if name == BASELINE_ID:
        raise ValueError("기본 자료 이름은 새 버전에 사용할 수 없습니다.")
    baseline = verify_integrated_price_sources(root, market_root=root)
    if baseline["status"] != "OK":
        raise ValueError("기본 가격자료를 검증하지 못했습니다.")
    staged = snapshot_dir(root, name)
    staged.mkdir(parents=True)
    raw = "data/raw/auction_trades.csv"
    for relative in (*DAILY_FILES, raw):
        target = staged / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    report = json.loads((staged / "data/reports/auction_quality_report.json").read_text(encoding="utf-8"))
    return _finish_snapshot(staged, {"status": "VALIDATED", "snapshot_id": name,
        "build_mode": "BASELINE_INTERVAL_EXTENSION", "checked_on_kst": today.isoformat(),
        "source_url": "https://www.data.go.kr/data/15141810/openapi.do",
        "observed_dates": report["observed_dates"], "raw_files": {raw: digest(staged / raw)},
        "accepted_rows": report["accepted_rows"], "rejected_rows": report["rejected_rows"],
        "is_live_price": False})


@contextmanager
def _pointer_lock(root):
    path = root / MARKET_POINTER_FILE.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _replace_market_pointer(root, target, previous):
    pointer = root / MARKET_POINTER_FILE
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=pointer.parent,
                                prefix=pointer.stem + "-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({**target, "previous": previous}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(pointer)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _reference(pointer):
    return {key: pointer[key] for key in ("snapshot_id", "manifest_sha256")}


def activate_snapshot(name: str, root: Path = PROJECT_ROOT, today=None, *, expected_selection=None) -> dict:
    from services.data_contract import verify_cached_price_sources
    from services.evidence import _market_rows

    root = Path(root)
    today = today or kst_today()
    staged = snapshot_dir(root, name)
    target = {"snapshot_id": name, "manifest_sha256": digest(staged / "manifest.json")}
    manifest = load_snapshot(staged, name, target["manifest_sha256"])
    if set(manifest["files"]) != SNAPSHOT_FILES:
        raise ValueError("일별 가격과 가격쌍을 함께 준비한 버전만 활성화할 수 있습니다.")
    validation = verify_cached_price_sources(root, market_root=staged)
    if validation["status"] != "OK":
        raise ValueError("일별 가격으로 가격쌍과 검증 명세를 재현하지 못했습니다.")
    dates = sorted({r["date"] for r in _market_rows(staged)})
    if dates != manifest["observed_dates"] or not dates or max(dates) > today.isoformat():
        raise ValueError("관측 날짜가 명세와 다르거나 미래 날짜입니다.")
    with _pointer_lock(root):
        current = read_market_pointer(root)
        if expected_selection is not None:
            path = root / MARKET_POINTER_FILE
            token = digest(path) if path.exists() else BASELINE_ID
            if token != expected_selection:
                raise ValueError("미리보기 이후 사용 자료가 바뀌었습니다. 다시 검사하세요.")
        selected = resolve_snapshot(root, current)
        status = market_status(root, today)
        same_source_extension = (manifest.get("build_mode") == "BASELINE_INTERVAL_EXTENSION"
                                 and selected == root
                                 and all(digest(root / path) == manifest["files"][path] for path in DAILY_FILES))
        if not same_source_extension and status["observed_last_date"] and max(dates) <= status["observed_last_date"]:
            raise ValueError("기존보다 최신 관측이 없습니다. 재현용 버전으로 보존하고 교체하지 않습니다.")
        resolve_snapshot(root, target)
        _replace_market_pointer(root, target, _reference(current))
        return market_status(root, today)


def rollback_snapshot(root: Path = PROJECT_ROOT, today=None) -> dict:
    """Restore the previous verified selection in the same atomic transaction."""
    root = Path(root)
    with _pointer_lock(root):
        current = read_market_pointer(root)
        previous = current.get("previous")
        if previous is None:
            legacy = root / MARKET_POINTER_FILE.with_name("market-active-previous.json")
            if current["snapshot_id"] == BASELINE_ID or not legacy.exists():
                raise ValueError("복구할 이전 자료 버전이 없습니다.")
            previous = _reference(json.loads(legacy.read_text(encoding="utf-8")))
        selected = resolve_snapshot(root, previous)
        from services.data_contract import verify_cached_price_sources

        if verify_cached_price_sources(root, market_root=selected)["status"] != "OK":
            raise ValueError("이전 자료의 일별 가격과 가격쌍을 검증하지 못했습니다.")
        _replace_market_pointer(root, _reference(previous), _reference(current))
        return market_status(root, today)
