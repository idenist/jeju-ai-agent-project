"""Explicit preview/apply of supported auction CSV; no network or model calls."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from .config import PROJECT_ROOT
from .data_refresh import (activate_snapshot, digest, kst_today, market_status,
                           resolve_market_root, snapshot_dir, stage_snapshot)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _selection_token(root):
    pointer = root / "artifacts/market-active.json"
    return digest(pointer) if pointer.exists() else "shared-baseline"


def _current_raw_files(root):
    selected = resolve_market_root(root)
    if selected == root:
        return [root / "data/raw/auction_trades.csv"]
    manifest = json.loads((selected / "manifest.json").read_text(encoding="utf-8"))
    paths = []
    for relative, expected in manifest["raw_files"].items():
        path = (selected / relative).resolve()
        if not path.is_relative_to(selected.resolve()) or digest(path) != expected:
            raise ValueError("현재 자료의 경매 원본이 변경됐습니다. 자료 관리자가 확인해야 합니다.")
        paths.append(path)
    if not paths:
        raise ValueError("현재 자료의 경매 원본이 없습니다.")
    return paths


def _decode(payload):
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return payload.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("UTF-8 또는 CP949 CSV로 저장한 뒤 다시 선택하세요.")


def preview_auction_csv(payload: bytes, root: Path = PROJECT_ROOT, today=None) -> dict:
    """Keep a validated candidate, but leave the active data untouched."""
    root = Path(root)
    today = today or kst_today()
    if not payload or len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError("빈 파일은 사용할 수 없습니다. CSV는 최대 20MiB까지 지원합니다.")
    text, encoding = _decode(payload)
    if "\x00" in text:
        raise ValueError("CSV 텍스트가 아닙니다. 지원 형식으로 다시 저장하세요.")
    raw_files = _current_raw_files(root)
    with (root / "data/raw/auction_trades.csv").open(encoding="utf-8-sig", newline="") as f:
        headers = next(csv.reader(f))
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    supplied = reader.fieldnames or []
    if len(supplied) != len(set(supplied)):
        raise ValueError("중복 컬럼이 있습니다. 컬럼명을 확인하세요.")
    if set(supplied) != set(headers):
        missing = sorted(set(headers) - set(supplied))
        extra = sorted(set(supplied) - set(headers))
        raise ValueError("지원 경매 원본 명세와 다릅니다. 누락: " + ", ".join(missing)
                         + " / 지원하지 않는 컬럼: " + ", ".join(extra))
    current = market_status(root, today)
    token = _selection_token(root)
    tmp = root / "artifacts/tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp) as folder:
        folder = Path(folder)
        source = folder / "incoming.csv"
        with source.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            try:
                for row in reader:
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError("CSV의 행별 컬럼 수가 다릅니다. 원본을 확인하세요.")
                    writer.writerow(row)
            except csv.Error as exc:
                raise ValueError("CSV 따옴표 또는 행 구분이 잘못됐습니다.") from exc
        # Validate incoming rows alone: existing valid data must not mask an invalid upload.
        from scripts.preprocess_auction import run
        report = run([source], folder / "data/processed", folder / "data/reports")
        dates = report["observed_dates"]
        if not dates or max(dates) > today.isoformat():
            raise ValueError("유효한 제주 감귤 경매 관측이 없거나 미래 날짜입니다.")
        if current["observed_last_date"] and max(dates) <= current["observed_last_date"]:
            raise ValueError("기존보다 최신인 유효 관측이 없습니다. 현재 자료는 유지합니다.")
        name = "csv-" + uuid4().hex
        manifest = stage_snapshot([*raw_files, source], name, root, today)
    staged = snapshot_dir(root, name)
    # This evidence is not a claim that the provider was queried for updates.
    manifest["import"] = {"mode": "USER_CSV_APPEND", "payload_sha256": hashlib.sha256(payload).hexdigest(),
                          "encoding": encoding, "previous_snapshot": current["snapshot_id"]}
    path = staged / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"snapshot_id": name, "manifest_sha256": digest(path), "previous_selection": token,
            "payload_sha256": hashlib.sha256(payload).hexdigest(), "encoding": encoding,
            "current": current, "observed_first_date": min(manifest["observed_dates"]),
            "observed_last_date": max(manifest["observed_dates"]),
            "input_rows": report["input_rows"], "accepted_rows": report["accepted_rows"],
            "rejected_rows": report["rejected_rows"],
            "rejected_by_primary_reason": report["rejected_by_primary_reason"],
            "varieties": sorted(report["accepted_by_variety"]),
            "combined_accepted_rows": manifest["accepted_rows"]}


def apply_auction_preview(preview: dict, root: Path = PROJECT_ROOT, today=None) -> dict:
    root = Path(root)
    if preview["previous_selection"] != _selection_token(root):
        raise ValueError("미리보기 이후 사용 자료가 바뀌었습니다. 다시 검사하세요.")
    staged = snapshot_dir(root, preview["snapshot_id"])
    if digest(staged / "manifest.json") != preview["manifest_sha256"]:
        raise ValueError("미리보기 이후 자료 명세가 변경됐습니다. 다시 검사하세요.")
    manifest = json.loads((staged / "manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["raw_files"].items():
        path = (staged / relative).resolve()
        if not path.is_relative_to(staged.resolve()) or digest(path) != expected:
            raise ValueError("검사 후 경매 원본이 변경됐습니다. 다시 검사하세요.")
    return activate_snapshot(preview["snapshot_id"], root, today)
