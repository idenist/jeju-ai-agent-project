"""Run the saved multi-season research model for an exact auction specification.

Example historical replay (not a current shipment recommendation):
    python -m experiments.predict_multiseason_forecast --as-of 2026-11-13
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from experiments.evaluate_multiseason_inputs import BASE_NUMERIC, ROOT, SPEC_FIELDS
from experiments.forecast_bridge import (
    ForecastUnavailable,
    _features,
    _normalize_spec,
    _read_source_prices,
    _spec_key,
)

MODEL_ROOT = ROOT / "artifacts" / "seunghyuk" / "price_forecast_multiseason"
DEFAULT_PRICE_FILE = ROOT / "data" / "seasons" / "2026-2027" / "daily_prices.csv"
SUPPORTED_HORIZONS = (3, 7, 10, 14)
DEFAULT_SPEC = {
    "market_code": "110001", "variety_code": "13", "grade_code": "11",
    "size_code": "184", "package": "상자", "package_kg": "5", "trade_type": "경매",
}


def _resolve_bundle(path: Path | None) -> Path:
    if path is not None:
        candidate = path if path.is_absolute() else ROOT / path
        if (candidate / "metrics.json").is_file():
            return candidate
        raise ForecastUnavailable("MODEL_NOT_FOUND", f"모델 묶음에 metrics.json이 없습니다: {candidate}")
    candidates = sorted(
        (p.parent for p in MODEL_ROOT.glob("*/metrics.json")),
        key=lambda p: p.name,
        reverse=True,
    )
    if not candidates:
        raise ForecastUnavailable("MODEL_NOT_FOUND", "학습된 다중 시즌 모델을 찾을 수 없습니다.")
    return candidates[0]


def predict_prices(*, as_of: date, spec: dict, horizons: tuple[int, ...] = SUPPORTED_HORIZONS,
                   model_dir: Path | None = None, price_file: Path = DEFAULT_PRICE_FILE) -> dict:
    import joblib
    import numpy as np
    import sklearn

    if not horizons or any(type(days) is not int or days not in SUPPORTED_HORIZONS for days in horizons):
        raise ForecastUnavailable("UNSUPPORTED_HORIZON", "지원 간격은 3·7·10·14일입니다.")
    if len(set(horizons)) != len(horizons):
        raise ForecastUnavailable("UNSUPPORTED_HORIZON", "예측 간격이 중복됐습니다.")
    norm_spec = _normalize_spec(spec)
    bundle = _resolve_bundle(model_dir)
    metadata = json.loads((bundle / "metrics.json").read_text(encoding="utf-8"))
    if metadata.get("status") != "FITTED_RESEARCH_ONLY_NOT_DEPLOYED":
        raise ForecastUnavailable("MODEL_CONTRACT_ERROR", "연구용 모델 묶음 상태가 예상과 다릅니다.")
    if metadata.get("versions", {}).get("scikit_learn") != sklearn.__version__:
        raise ForecastUnavailable("MODEL_VERSION_MISMATCH", "학습과 현재 scikit-learn 버전이 다릅니다.")
    feature_contract = metadata.get("features", {})
    if (feature_contract.get("numeric") != list(BASE_NUMERIC)
            or feature_contract.get("categorical") != list(SPEC_FIELDS)):
        raise ForecastUnavailable("MODEL_CONTRACT_ERROR", "모델과 입력 피처 순서가 다릅니다.")
    allowed = {
        tuple(entry[field] for field in SPEC_FIELDS)
        for entry in metadata.get("eligible_spec_list", [])
    }
    if _spec_key(norm_spec) not in allowed:
        raise ForecastUnavailable("UNSEEN_SPEC", "이 규격은 정상 관측이 충분해 학습한 규격 목록에 없습니다.")

    source_path = price_file if price_file.is_absolute() else ROOT / price_file
    rows = _read_source_prices(source_path)
    latest = max((row["date"] for row in rows), default=None)
    if latest is None or as_of > latest:
        raise ForecastUnavailable("NO_BASE_OBSERVATION", "기준일 가격 자료가 없습니다.")
    features, provenance = _features(rows, spec=norm_spec, as_of=as_of)
    matrix = np.asarray([features], dtype=object)
    current = Decimal(provenance["source_price_krw_per_kg"])
    forecasts = {}
    for days in horizons:
        model_file = bundle / f"candidate_{days}d.joblib"
        if not model_file.is_file():
            raise ForecastUnavailable("MODEL_NOT_FOUND", f"{days}일 모델 파일이 없습니다.")
        model = joblib.load(model_file)
        delta = float(model.predict(matrix)[0])
        estimate = float(current) + delta
        if not math.isfinite(estimate) or estimate <= 0:
            raise ForecastUnavailable("INVALID_PREDICTION", f"{days}일 추정 가격이 비정상입니다.")
        price = Decimal(str(estimate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        forecasts[str(days)] = {
            "target_date": (as_of + timedelta(days=days)).isoformat(),
            "price_krw_per_kg": format(price, "f"),
            "predicted_change_krw_per_kg": round(delta, 2),
        }
    age_days = (date.today() - as_of).days
    return {
        "status": "EXPERIMENTAL_ONLY",
        "as_of": as_of.isoformat(),
        "latest_source_date": latest.isoformat(),
        "age_days": age_days,
        "price_file": str(source_path),
        "model_bundle": str(bundle),
        "model_family": metadata["model_family"],
        "spec": norm_spec,
        "current_price_krw_per_kg": format(current, "f"),
        "features": provenance,
        "forecasts": forecasts,
        "warnings": [
            "연구용 가격 전망이며 실제 출하 추천이나 농가 실수취가 예측이 아닙니다.",
            "예측 구간은 별도로 보정되지 않았습니다.",
            *( ["가격 자료가 최신 관측일 기준 3일보다 오래되어 현재 판단에 사용하면 안 됩니다."
                ] if age_days > 3 else [] ),
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat,
                        help="가격 관측 기준일 (YYYY-MM-DD)")
    parser.add_argument("--model-dir", type=Path, help="timestamped model bundle directory")
    parser.add_argument("--price-file", type=Path, default=DEFAULT_PRICE_FILE,
                        help="matching seasonal daily_prices.csv")
    parser.add_argument("--horizons", type=int, nargs="+", default=list(SUPPORTED_HORIZONS),
                        choices=SUPPORTED_HORIZONS)
    for field, default in DEFAULT_SPEC.items():
        parser.add_argument(f"--{field.replace('_', '-')}", default=default)
    args = parser.parse_args(argv)
    spec = {field: getattr(args, field) for field in DEFAULT_SPEC}
    try:
        result = predict_prices(as_of=args.as_of, spec=spec, horizons=tuple(args.horizons),
                                model_dir=args.model_dir, price_file=args.price_file)
    except (ForecastUnavailable, OSError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
        print(json.dumps({"status": "UNAVAILABLE", "message": str(exc)}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
