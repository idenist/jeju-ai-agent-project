"""연구용 가격 예측 → 기존 A/B 손익 엔진 연결 (실험용 CLI에서만 사용).

실행: python -m agents.seunghyuk.forecast_bridge --demo
* 모델은 scripts.train_price_forecast.py가 로컬에서 만든 신뢰 가능한 joblib만 사용한다.
* 경매 정산일 기준의 역사적 리플레이이며, 실제 출하일·농가 수취가 예측이 아니다.
* 3/7/14일 이외 간격은 보간하지 않고 거부한다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRICE_FILE = PROJECT_ROOT / "data/processed/daily_prices.csv"
CONTRACT_FILE = PROJECT_ROOT / "data/seasons/prediction_v2/데이터계약_v2.json"
MODEL_ROOT = PROJECT_ROOT / "artifacts/seunghyuk/price_forecast_v1"
# [실험 데이터 선택] 명시적 이름으로 검증된 독립 묶음만 사용한다.
# 일반 Agent·공용 활성 스냅샷 포인터를 바꾸지 않으며, 미설정 시 기존 공용 자료를 사용한다.
EXPERIMENT_DATASET_ENV = "JEJU_EXPERIMENTAL_DATASET"
DATASET_FOLDER = Path("artifacts/seunghyuk/datasets")
# [예측기간 계약] 기준일로부터 3·7·14일 후의 가격만 별도 모델이 학습했다.
# 10일 같은 임의 간격은 두 모델 사이를 보간해 만들지 않는다.
SUPPORTED_DAYS = frozenset((3, 7, 14))
KEY_FIELDS = ("market_code", "variety_code", "grade_code", "size_code", "package", "package_kg", "trade_type")
NUMERIC_FIELDS = (
    "feature_price_kg", "feature_total_kg", "feature_transaction_count",
    "feature_weekday", "feature_month", "feature_season_day_index",
    "feature_lag3_price_kg", "feature_lag7_price_kg",
    "feature_trailing7_mean_price_kg", "feature_trailing7_total_kg", "feature_trailing7_ok_days",
)
EXAMPLE_SPEC = {
    "market_code": "110001", "variety_code": "01", "grade_code": "11",
    "size_code": "184", "package": "상자", "package_kg": "5", "trade_type": "경매",
}
EXAMPLE_A = {"delay_days": 3, "loss_percent": 1, "daily_storage_cost": 10000,
             "other_cost_krw": 5000, "fee_percent": 5}
EXAMPLE_B = {"delay_days": 14, "loss_percent": 4, "daily_storage_cost": 10000,
             "other_cost_krw": 5000, "fee_percent": 7}


class ForecastUnavailable(ValueError):
    """데이터 부족·미지원 조건으로 안전하게 예측을 거절."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _money(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ForecastUnavailable("INVALID_NUMBER", "입력 가격·물량의 숫자 형식을 확인하세요.") from exc
    if not result.is_finite():
        raise ForecastUnavailable("INVALID_NUMBER", "가격·물량에는 유한한 숫자만 사용할 수 있습니다.")
    return result


# 원본 API 코드의 앞자리 0과 규격 식별 관계를 유지한다.
# 숫자처럼 보여도 품종·등급·크기 코드는 문자열로 비교한다.
def _normalize_spec(spec: dict) -> dict[str, str]:
    if not isinstance(spec, dict) or set(spec) != set(KEY_FIELDS):
        raise ForecastUnavailable("SPEC_REQUIRED", "시장·품종·등급·크기·포장·중량·거래방식을 모두 지정해야 합니다.")
    normalized = {}
    for field in KEY_FIELDS:
        value = spec[field]
        if not isinstance(value, str) or not value.strip():
            raise ForecastUnavailable("SPEC_REQUIRED", "거래규격은 빈 값 없이 지정해야 합니다.")
        normalized[field] = value.strip()
    weight = _money(normalized["package_kg"])
    if weight <= 0:
        raise ForecastUnavailable("SPEC_REQUIRED", "포장 중량은 양수여야 합니다.")
    normalized["package_kg"] = format(weight.normalize(), "f")
    return normalized


def _spec_key(spec: dict) -> tuple[str, ...]:
    return tuple(_normalize_spec(spec)[field] for field in KEY_FIELDS)


# [품질 게이트] price CSV에서 quality_flag=OK인 날짜·규격만 모델 입력으로 사용.
# 미상 크기·낮은 표본수 등을 정상 가격으로 암묵적으로 대체하지 않는다.
def _read_source_prices(path: Path) -> list[dict]:
    needed = set(KEY_FIELDS) | {"date", "avg_price_kg", "total_kg", "transaction_count", "quality_flag"}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        names = reader.fieldnames or []
        if len(set(names)) != len(names) or not needed.issubset(names):
            raise ForecastUnavailable("PRICE_SCHEMA_ERROR", "가격 CSV 컬럼 계약이 맞지 않습니다.")
        results = []
        seen = set()
        for r in reader:
            if None in r or any(v is None for v in r.values()):
                raise ForecastUnavailable("PRICE_SCHEMA_ERROR", "가격 CSV의 행 구조가 잘못됐습니다.")
            # 경고 행은 삭제가 아니라 이번 학습/예측 입력에서만 제외한다.
            # 누락 기간의 가격을 0원 또는 전날 가격으로 대체하지 않는다.
            if r["quality_flag"] != "OK":
                continue
            try:
                d = date.fromisoformat(r["date"])
                key = _spec_key({field: r[field] for field in KEY_FIELDS})
                price, kg, count = (_money(r[k]) for k in ("avg_price_kg", "total_kg", "transaction_count"))
            except (ValueError, TypeError) as exc:
                raise ForecastUnavailable("PRICE_SCHEMA_ERROR", "품질 OK 거래에 잘못된 날짜·숫자가 있습니다.") from exc
            if min(price, kg, count) <= 0 or count != count.to_integral_value():
                raise ForecastUnavailable("PRICE_SCHEMA_ERROR", "품질 OK 거래에 비정상 가격·중량·건수가 있습니다.")
            identity = (d, key)
            if identity in seen:
                raise ForecastUnavailable("PRICE_SCHEMA_ERROR", "가격 CSV에 동일 날짜·규격이 중복됐습니다.")
            seen.add(identity)
            results.append({"date": d, "key": key, "price": price, "kg": kg, "count": count})
    return results


# [데이터 누수 방지] 정답 날짜(미래) 관측을 피처에 쓰지 않는다.
# 모든 입력은 as_of 당일까지 관측된 같은 거래규격 데이터에서만 산출한다.
def _features(rows: list[dict], *, spec: dict, as_of: date) -> tuple[list[object], dict]:
    """기준일 이전 정상 거래만으로 prediction_v2의 특징을 동일하게 생성."""
    norm = _normalize_spec(spec)
    group = {r["date"]: r for r in rows if r["key"] == _spec_key(norm) and r["date"] <= as_of}
    current = group.get(as_of)
    if current is None:
        raise ForecastUnavailable("NO_BASE_OBSERVATION", "기준일에 해당 규격의 품질 OK 경매가격이 없습니다.")
    trailing = [r for d, r in group.items() if as_of - timedelta(days=6) <= d <= as_of]
    if not trailing:
        raise ForecastUnavailable("NO_BASE_OBSERVATION", "기준일 이전의 정상 가격이 없습니다.")
    # [피처 재현] 기존 prediction_v2와 동일하게 최근 7달력일 정상 관측의
    # '일별 가격 산술평균'을 사용한다. 거래중량 가중평균으로 바꾸면 학습과 입력이 달라진다.
    trailing_mean = (sum((r["price"] for r in trailing), Decimal(0)) / len(trailing))
    trailing_mean = trailing_mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    trailing_kg = sum((r["kg"] for r in trailing), Decimal(0))
    # 10월~다음 해 2월을 한 출하 시즌으로 취급한다.
    # 1월에는 10월 1일부터의 경과 일수가 끊기지 않아야 한다.
    season_year = as_of.year if as_of.month >= 10 else as_of.year - 1
    numeric: list[object] = [
        float(current["price"]), float(current["kg"]), int(current["count"]),
        as_of.weekday(), as_of.month, (as_of - date(season_year, 10, 1)).days,
        float(group[as_of - timedelta(days=3)]["price"]) if as_of - timedelta(days=3) in group else float("nan"),
        float(group[as_of - timedelta(days=7)]["price"]) if as_of - timedelta(days=7) in group else float("nan"),
        float(trailing_mean), float(trailing_kg), len(trailing),
    ]
    return [*numeric, *(norm[f] for f in KEY_FIELDS)], {
        "as_of": as_of.isoformat(), "source_price_krw_per_kg": str(current["price"]),
        "source_observed_days_in_last_7": len(trailing), "spec": norm,
    }


# [현재 한계] 모델 디렉터리를 지정하지 않으면 최신 로컬 학습 폴더를 선택한다.
# '가장 최근 학습'이 '승인된 모델'을 뜻하지 않으므로 운영 사용은 금지한다.
def _validated_models_folder(base: Path) -> Path:
    if (base / "metrics.json").is_file():
        return base
    candidates = sorted((p for p in base.glob("*/metrics.json") if p.is_file()), reverse=True)
    if not candidates:
        raise ForecastUnavailable("MODEL_NOT_FOUND", "학습된 모델을 찾지 못했습니다. train_price_forecast.py의 결과 폴더를 확인하세요.")
    return candidates[0].parent


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# [실제 장애 예방] 공용 refresh 스냅샷은 가격표와 품질 보고서만을 계약으로
# 허용한다. 그 경로로 개인 데이터 묶음을 활성화하면 과거 가격쌍이 다른 버전으로
# 남거나 자료 버전 검증에서 실패한다. 여기서는 운영 파일을 바꾸지 않고 연구용
# 예측 입력만 명시적으로 선택하고, 가격표/가격쌍/보고서가 한 묶음인지 검사한다.
def selected_experiment_source(project_root: Path = PROJECT_ROOT) -> dict:
    """연구용 가격 원천을 결정한다. 임의 경로나 해시 불일치는 기존 자료로 우회하지 않는다."""
    name = os.environ.get(EXPERIMENT_DATASET_ENV, "").strip()
    root = Path(project_root)
    if not name:
        return {"price_file": root / "data/processed/daily_prices.csv",
                "dataset_id": "shared-baseline", "kind": "shared_baseline", "last_date": None}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ForecastUnavailable("INVALID_DATASET_NAME", "연구용 데이터 묶음은 경로가 아닌 검증된 이름으로 지정하세요.")
    folder = (root / DATASET_FOLDER / name).resolve()
    manifest_file = folder / "manifest.json"
    required = {
        "data/processed/daily_prices.csv",
        "data/processed/daily_variety_prices.csv",
        "data/processed/price_comparisons.csv",
        "data/processed/price_comparison_availability.csv",
        "data/reports/auction_quality_report.json",
        "data/reports/price_comparison_report.json",
    }
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if (manifest.get("status") != "OFFLINE_VALIDATED" or
                manifest.get("agent_activated") is not False or
                manifest.get("snapshot_name") != name):
            raise ValueError("검증 완료되지 않은 연구용 묶음")
        hashes = manifest["sha256"]
        if not isinstance(hashes, dict) or not required.issubset(hashes):
            raise ValueError("가격표·가격쌍·품질 보고서 중 누락된 파일")
        # [버전 방어] 묶음의 모든 원본·집계·가격쌍·보고서 해시를 검사한다.
        # 파일을 바꾸거나 손상시킨 뒤 오래된 검증 명세만 남기면 예측을 거부한다.
        for relative, expected_hash in hashes.items():
            rel = Path(relative)
            if (not isinstance(relative, str) or rel.is_absolute() or
                    "\\" in relative or ".." in rel.parts or len(rel.parts) < 2):
                raise ValueError("검증 명세에 허용되지 않는 파일 경로")
            absolute = (folder / rel).resolve()
            if not absolute.is_relative_to(folder) or not absolute.is_file():
                raise ValueError("검증 명세의 파일 누락 또는 묶음 외부 경로")
            if not isinstance(expected_hash, str) or _sha(absolute) != expected_hash:
                raise ValueError("데이터 파일 해시 불일치")
        price = folder / "data/processed/daily_prices.csv"
        pair_report = json.loads((folder / "data/reports/price_comparison_report.json").read_text(encoding="utf-8"))
        quality_report = json.loads((folder / "data/reports/auction_quality_report.json").read_text(encoding="utf-8"))
        counts = manifest["csv_rows"]
        # [관측일·원천 계약] 단순히 파일이 존재하는지만 확인하지 않고 가격쌍
        # 생성 보고서에 기록된 가격 CSV 해시와 가격표 행 수를 대조한다.
        dates = quality_report["observed_dates"]
        if (not dates or min(dates) != manifest["observed_date_min"] or
                max(dates) != manifest["observed_date_max"] or
                quality_report["daily_prices_rows"] != counts["daily_prices.csv"] or
                pair_report["input_sha256"] != hashes["data/processed/daily_prices.csv"] or
                pair_report["input_rows"] != counts["daily_prices.csv"] or
                pair_report["rows"] != counts["price_comparisons.csv"] or
                pair_report["availability_rows"] != counts["price_comparison_availability.csv"]):
            raise ValueError("관측 기간·원천 가격표·가격쌍 보고서의 버전 불일치")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        # [실패 시 차단] 사용자가 새 묶음을 선택했는데 이를 읽지 못했다고
        # 공용 11/03 데이터를 조용히 사용하면 '최신 자료 예측'으로 오인한다.
        raise ForecastUnavailable("EXPERIMENT_DATASET_INVALID",
                                  "선택한 연구용 데이터 묶음의 파일·해시·가격쌍 계약을 확인하세요.") from exc
    return {"price_file": price, "dataset_id": name,
            "kind": "offline_validated", "last_date": manifest["observed_date_max"]}


# [버전 계약] 학습 CSV의 해시·피처 순서·scikit-learn 버전을 확인해
# 모델과 입력 자료가 서로 다른 버전으로 섞이지 않도록 한다.
def _check_model_contract(folder: Path, spec: dict, horizons: tuple[int, ...], root: Path) -> dict:
    import sklearn
    metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    contract = json.loads((root / "data/seasons/prediction_v2/데이터계약_v2.json").read_text(encoding="utf-8"))
    if metrics.get("status") != "EXPERIMENTAL_NOT_DEPLOYED" or contract.get("status") != "LABELED_DATA_NOT_TRAINED_MODEL":
        raise ForecastUnavailable("MODEL_CONTRACT_ERROR", "연구용 모델·데이터 계약이 예상과 다릅니다.")
    if metrics.get("model_inputs") != {"numeric": list(NUMERIC_FIELDS), "categorical": list(KEY_FIELDS)}:
        raise ForecastUnavailable("MODEL_CONTRACT_ERROR", "학습 모델의 입력 순서가 다릅니다.")
    if metrics.get("versions", {}).get("scikit_learn") != sklearn.__version__:
        raise ForecastUnavailable("MODEL_VERSION_MISMATCH", "학습 당시와 scikit-learn 버전이 다릅니다. 동일 환경에서 실행하세요.")
    for name, filename in (("train", "2024-2025_train.csv"), ("validation", "2025-2026_validation.csv")):
        p = root / "data/seasons/prediction_v2" / filename
        if _sha(p) != metrics.get("input_sha256", {}).get(name):
            raise ForecastUnavailable("MODEL_DATA_MISMATCH", "모델 학습·검증 데이터의 버전이 다릅니다.")
    # 학습 시 선별한 28개 규격 외 상품에는 모델 출력을 억지로 적용하지 않는다.
    # 매칭 기준은 사용자 표시명보다 원천 코드 조합이다.
    allowed = {_spec_key({k: entry[k] for k in KEY_FIELDS}) for entry in contract["training_spec_list"]}
    if _spec_key(spec) not in allowed:
        raise ForecastUnavailable("UNSEEN_SPEC", "이 거래규격은 학습용으로 사전 선정한 28개 규격에 없습니다.")
    for days in horizons:
        item = metrics.get("selected_candidates", {}).get(str(days))
        if not item or item.get("approved_for_agent") is not False or not (folder / f"candidate_{days}d.joblib").is_file():
            raise ForecastUnavailable("MODEL_CONTRACT_ERROR", "요청한 기간의 연구용 모델 파일 또는 계약을 찾지 못했습니다.")
    return metrics


def predict_prices(*, as_of: date, spec: dict, horizons: tuple[int, ...],
                   model_root: Path = MODEL_ROOT, project_root: Path = PROJECT_ROOT,
                   verify_baseline: bool = True) -> dict:
    """선택한 검증 가격표를 기준일에 잘라 미래 정보 없이 예측합니다. 운영 사용 금지."""
    if (not horizons or any(type(d) is not int or d not in SUPPORTED_DAYS for d in horizons)
            or len(set(horizons)) != len(horizons)):
        raise ForecastUnavailable("UNSUPPORTED_HORIZON", "현재 모델은 3·7·14일 후 가격만 추정할 수 있습니다.")
    if not isinstance(as_of, date) or as_of > date.today():
        raise ForecastUnavailable("INVALID_DATE", "관측 완료된 기준일만 사용할 수 있습니다.")
    if as_of.month not in (10, 11, 12, 1, 2):
        raise ForecastUnavailable("OUTSIDE_SEASON", "모델 학습 범위인 10월~다음 해 2월의 가격만 실험합니다.")
    if any((as_of + timedelta(days=d)).month not in (10, 11, 12, 1, 2) for d in horizons):
        raise ForecastUnavailable("OUTSIDE_SEASON", "예측일이 경매 시즌 범위를 벗어납니다.")
    norm = _normalize_spec(spec)
    # [원천 선택] 연구용 묶음을 명시했다면 동일 묶음의 가격표·가격쌍·보고서
    # 해시를 전부 확인한다. 학습 모델과 학습 시즌 CSV 계약은 바꾸지 않는다.
    source = selected_experiment_source(project_root)
    if verify_baseline and source["kind"] == "shared_baseline":
        from agents.common.data_contract import verify_integrated_price_sources
        checked = verify_integrated_price_sources(project_root)
        if checked.get("status") != "OK" or checked.get("full_content_verified") is not True:
            raise ForecastUnavailable("SOURCE_VERIFICATION_FAILED", "기존 가격 자료의 해시·가격쌍 검증에 실패했습니다.")
    folder = _validated_models_folder(Path(model_root))
    info = _check_model_contract(folder, norm, horizons, project_root)
    rows = _read_source_prices(source["price_file"])
    latest = max((r["date"] for r in rows), default=None)
    if latest is None or as_of > latest:
        raise ForecastUnavailable("NO_BASE_OBSERVATION", "요청한 기준일의 관측 가격이 아직 없습니다.")
    x, provenance = _features(rows, spec=norm, as_of=as_of)

    # [보안 주의] 모델 파일은 로컬에서 직접 학습해 생성한 신뢰 가능한 파일만 로드.
    # 서명·승인 레지스트리는 구현되지 않았으며 연구용 모델 자동 배포를 허용하지 않는다.
    # joblib는 임의 파일에서 코드를 실행할 수 있으므로, 신뢰하는 로컬 학습 폴더만 사용해야 한다.
    import joblib
    import numpy as np
    matrix = np.asarray([x], dtype=object)
    current = float(provenance["source_price_krw_per_kg"])
    forecasts = {}
    for days in horizons:
        model = joblib.load(folder / f"candidate_{days}d.joblib")
        delta = float(model.predict(matrix)[0])
        guess = current + delta
        if not math.isfinite(guess) or guess <= 0:
            raise ForecastUnavailable("INVALID_PREDICTION", "모델의 추정 가격이 비정상적이어서 비교를 중단했습니다.")
        price = Decimal(str(guess)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if price <= 0:
            raise ForecastUnavailable("INVALID_PREDICTION", "모델의 추정 가격이 비정상적이어서 비교를 중단했습니다.")
        selected = info["selected_candidates"][str(days)]
        forecasts[str(days)] = {
            "target_date": (as_of + timedelta(days=days)).isoformat(),
            "price_krw_per_kg": format(price, "f"),
            "model": selected["best_candidate"],
            "validation_mae_krw_per_kg": selected["candidate_mae"],
            "beats_baseline_on_validation": selected["better_than_persistence"],
        }
    # 가격이 오래된 경우에도 연구용 과거 재현은 허용하지만 경고를 반환한다.
    # 운영에선 최신성 자체를 별도 차단 조건으로 만들어야 한다.
    return {
        "status": "EXPERIMENTAL_ONLY", "as_of": as_of.isoformat(), "latest_market_date": latest.isoformat(),
        "age_days": (date.today() - as_of).days, "model_folder": str(folder),
        "data_sha256": _sha(source["price_file"]),
        "price_dataset_id": source["dataset_id"], "price_dataset_kind": source["kind"],
        "is_live_price": False,
        "features": provenance, "forecasts": forecasts,
        "warnings": [
            "이 결과는 연구용 가격 추정이며 실제 출하 권고·미래가격 보장이 아닙니다.",
            "경매 정산일을 기준으로 예측했습니다. 농가 실제 출하일·실수취가와 다릅니다.",
            *(["시장 자료가 최근 3일 이내의 관측이 아니므로 현재 시점의 출하 판단에 사용할 수 없습니다."]
              if (date.today() - as_of).days > 3 else []),
            *(["선정한 예측기간의 모델 중 검증 시즌에서 현재가격 유지보다 나빴던 모델이 있습니다."]
              if any(not forecasts[str(d)]["beats_baseline_on_validation"] for d in horizons) else []),
        ],
    }


def compare_experimental(*, as_of: date, spec: dict, quantity: float, price_basis: str,
                         option_a: dict, option_b: dict, model_root: Path = MODEL_ROOT,
                         project_root: Path = PROJECT_ROOT, verify_baseline: bool = True) -> dict:
    """예측가격 2개를 기존 순수 Python A/B 계산기에 입력하되 사용자 시나리오는 변경하지 않음."""
    try:
        a_days, b_days = option_a.get("delay_days"), option_b.get("delay_days")
        forecast = predict_prices(
            as_of=as_of, spec=spec, horizons=(a_days, b_days),
            model_root=model_root, project_root=project_root, verify_baseline=verify_baseline,
        )
        from agents.common.future_pair import compare_future_pair
        result = compare_future_pair(
            quantity=quantity, price_basis=price_basis, start_date=as_of.isoformat(),
            option_a=option_a, option_b=option_b,
            price_a=forecast["forecasts"][str(a_days)]["price_krw_per_kg"],
            price_b=forecast["forecasts"][str(b_days)]["price_krw_per_kg"],
        )
        if result.get("status") != "OK":
            return {"status": "CALCULATION_FAILED", "reason": result.get("status"),
                    "message": result.get("message", "기존 손익 계산에 실패했습니다."),
                    "deployment_approved": False}
        # [모듈 연결] 기존 공용 A/B 계산 엔진의 금액 산식은 재사용한다.
        # 반대로 예측 모델은 손익 산식을 재구현하지 않는다.
        # 기존 함수는 인수로 받은 단가를 '사용자 가정'으로 표시하므로 연구용 ML 근거로 바꿔 적는다.
        for key, days in (("option_a", a_days), ("manual_b", b_days)):
            result["evidence"][key].update(
                price_type="experimental_model_estimate", price_source="연구용 ML 추정",
                summary=f"{'A' if key == 'option_a' else 'B'}안 {days}일 가격 · 연구용 ML 추정 (사용자 입력 단가 아님)",
            )
        # [경고 문구 정정] 기존 계산기의 일반 경고를 ML 추정 모드에 맞게 바꾸되
        # 근거·실제 가격이 아닌 연구용 추정임을 결과의 provenance에도 보존한다.
        result["warnings"] = [
            ("A/B 단가는 연구용 ML 추정이며 감모·비용은 사용자가 지정한 가정입니다."
             if message == "사용자가 제공한 조건에 따른 비교이며 미래가격 예측이 아닙니다." else message)
            for message in result.get("warnings", [])
        ] + forecast["warnings"]
        return {"status": "EXPERIMENTAL_ONLY", "analysis_type": "forecast_comparison_experiment",
                "deployment_approved": False, "is_forecast": True,
                "provenance": forecast, "calculation": result,
                "message": "과거 거래 기준일의 두 미래가격 추정을 기존 A/B 계산기에 전달했습니다. 실서비스에는 연결되지 않았습니다."}
    # 원천 누락·미지원 날짜·비정상 모델값은 BLOCKED로 종료한다.
    # 값이 없을 때 미래가격을 만들어내는 fallback은 금지한다.
    except ForecastUnavailable as exc:
        return {"status": "BLOCKED", "reason": exc.code, "message": str(exc), "deployment_approved": False}
    except (OSError, ValueError, TypeError, KeyError, ImportError, ArithmeticError) as exc:
        # 모델 파일·원천 오류를 가격 0이나 임의의 값으로 바꿔 통과시키지 않는다.
        return {"status": "BLOCKED", "reason": "INTEGRATION_ERROR", "error_type": type(exc).__name__,
                "message": "모델·데이터·계산 모듈 연결을 확인하세요.", "deployment_approved": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="고정 가상 조건으로 백엔드 연결만 시연")
    parser.add_argument("--model-dir", type=Path, default=MODEL_ROOT, help="신뢰하는 로컬 학습 산출물 폴더")
    parser.add_argument("--json-out", type=Path, help="결과를 저장할 경로 (선택)")
    args = parser.parse_args(argv)
    if not args.demo:
        parser.error("실험 연결은 --demo에서만 실행합니다. 실제 사용자 도구에는 등록하지 않았습니다.")
    try:
        source = selected_experiment_source()
        rows = _read_source_prices(source["price_file"])
        key = _spec_key(EXAMPLE_SPEC)
        dates = [r["date"] for r in rows if r["key"] == key]
        if not dates:
            raise ForecastUnavailable("NO_DEMO_SPEC", "시연용 조생귤·특·소·상자 5kg의 정상 자료가 없습니다.")
        as_of = max(dates)
        print("[1/3] 신뢰할 수 있는 학습 모델·경매가격 원천 확인", flush=True)
        print("[2/3] " + as_of.isoformat() + " 기준 3·14일 ML 가격 추정", flush=True)
        output = compare_experimental(
            as_of=as_of, spec=EXAMPLE_SPEC, quantity=3000, price_basis="gross",
            option_a=EXAMPLE_A, option_b=EXAMPLE_B, model_root=args.model_dir,
        )
        print("[3/3] 기존 A/B 계산기로 실험용 손익 비교", flush=True)
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("결과 JSON:", args.json_out)
        if output["status"] == "EXPERIMENTAL_ONLY":
            prices = output["provenance"]["forecasts"]
            result = output["calculation"]
            print("[PASS] 백엔드 실험 연결 성공 (운영·대화 Agent 미반영)")
            print("  가격 데이터:", output["provenance"]["price_dataset_id"])
            print("  관측 기준일:", as_of.isoformat())
            print("  A안 3일 모델가격:", prices["3"]["price_krw_per_kg"], "원/kg")
            print("  B안 14일 모델가격:", prices["14"]["price_krw_per_kg"], "원/kg")
            print("  B안 - A안 가상 비교금액:", result["options_b"][0]["difference_vs_a_krw"], "원")
            print("  경고:", " / ".join(output["provenance"]["warnings"]))
            return 0
        print("[BLOCKED]", output.get("reason"), output.get("message"), file=sys.stderr)
        return 1
    except (OSError, ForecastUnavailable) as exc:
        print("[BLOCKED]", str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
