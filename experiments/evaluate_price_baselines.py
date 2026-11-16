"""Evaluate leakage-safe price persistence and lag baselines by exact horizon.

This is a time-split data evaluation, not a test suite. It reads the versioned
prediction_v2 CSVs, verifies their contract hashes, and never fits or saves a
forecast model. The partial 2026-2027 split is reported as diagnostic only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "seasons" / "prediction_v2"
CONTRACT_PATH = DATA / "데이터계약_v2.json"
SPLITS = {
    "train": DATA / "2024-2025_train.csv",
    "validation": DATA / "2025-2026_validation.csv",
    "evaluation": DATA / "2026-2027_partial_evaluation.csv",
}
HORIZONS = (3, 7, 14)
SPEC_FIELDS = (
    "market_code", "variety_code", "grade_code", "size_code",
    "package", "package_kg", "trade_type",
)
PREDICTORS = {
    "persistence": "feature_price_kg",
    "lag3": "feature_lag3_price_kg",
    "lag7": "feature_lag7_price_kg",
    "trailing7_mean": "feature_trailing7_mean_price_kg",
}
BOOTSTRAPS = 3000
SEED = 20261116
BLOCK_DAYS = 14


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: str, *, positive=False) -> float | None:
    if value is None or not value.strip():
        return None
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"유효하지 않은 수치: {value!r}")
    return result


def load_split(name: str, path: Path, contract: dict) -> list[dict]:
    expected_hash = contract["outputs"][name]["sha256"]
    actual_hash = sha256(path)
    if actual_hash != expected_hash:
        raise ValueError(f"{name}: 데이터계약 해시 불일치 ({actual_hash})")
    allowed_inputs = set(contract["model_input_fields"])
    used_fields = {"feature_price_kg", "feature_lag3_price_kg",
                   "feature_lag7_price_kg", "feature_trailing7_mean_price_kg"}
    if not used_fields <= allowed_inputs:
        raise ValueError(f"모델 허용 입력에 없는 기준 피처: {sorted(used_fields - allowed_inputs)}")
    if used_fields & set(contract["not_model_inputs"]):
        raise ValueError("금지 입력이 기준선에 포함되어 있습니다.")

    required = {
        "split", "horizon_days", "base_date", "target_date", "target_price_kg",
        *SPEC_FIELDS, *used_fields,
    }
    rows = []
    identities = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        if len(header) != len(set(header)) or not required <= set(header):
            raise ValueError(f"{path.name}: 필수 열 누락 또는 중복")
        for line, raw in enumerate(reader, 2):
            if raw.get("split") != name:
                raise ValueError(f"{path.name} {line}행: split이 {name!r}이 아닙니다")
            horizon = int(raw["horizon_days"])
            if horizon not in HORIZONS:
                raise ValueError(f"{path.name} {line}행: 지원하지 않는 간격 {horizon}")
            base = date.fromisoformat(raw["base_date"])
            target = date.fromisoformat(raw["target_date"])
            if target != base + timedelta(days=horizon):
                raise ValueError(f"{path.name} {line}행: 기준일과 대상일 간격 불일치")
            if any(not raw[field].strip() for field in SPEC_FIELDS):
                raise ValueError(f"{path.name} {line}행: 거래 규격 키 누락")
            key = (horizon, base.isoformat(), *(raw[field] for field in SPEC_FIELDS))
            if key in identities:
                raise ValueError(f"{path.name} {line}행: 기준일·규격 중복")
            identities.add(key)
            current = _number(raw["feature_price_kg"], positive=True)
            target_price = _number(raw["target_price_kg"], positive=True)
            rows.append({
                "split": name,
                "horizon": horizon,
                "base_date": base.isoformat(),
                "target_date": target.isoformat(),
                "current": current,
                "target": target_price,
                "predictors": {
                    label: current if label == "persistence" else _number(raw[column], positive=True)
                    for label, column in PREDICTORS.items()
                },
            })
    return rows


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _moving_block_bootstrap_ci(date_values: dict[str, float], seed: int) -> list[float] | None:
    if len(date_values) < 2:
        return None
    start = date.fromisoformat(min(date_values))
    end = date.fromisoformat(max(date_values))
    calendar_days = [(start + timedelta(days=offset)).isoformat()
                     for offset in range((end - start).days + 1)]
    if len(calendar_days) < 4 * BLOCK_DAYS:
        return None
    if sum(day in date_values for day in calendar_days) < 2:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(BOOTSTRAPS):
        sample = []
        cursor = 0
        while cursor < len(calendar_days):
            block_start = rng.randrange(len(calendar_days))
            block_length = min(BLOCK_DAYS, len(calendar_days) - cursor)
            for offset in range(block_length):
                day = calendar_days[(block_start + offset) % len(calendar_days)]
                if day in date_values:
                    sample.append(date_values[day])
            cursor += block_length
        if sample:
            estimates.append(statistics.fmean(sample))
    return [round(_percentile(estimates, 0.025), 2), round(_percentile(estimates, 0.975), 2)]


def _metrics(rows: list[dict], label: str, seed: int) -> dict | None:
    eligible = [row for row in rows if row["predictors"][label] is not None]
    if not eligible:
        return None
    errors = [row["predictors"][label] - row["target"] for row in eligible]
    absolute = [abs(error) for error in errors]
    daily = defaultdict(list)
    for row, error in zip(eligible, absolute):
        daily[row["base_date"]].append(error)
    daily_mae = {day: statistics.fmean(values) for day, values in daily.items()}
    predicted_directions = [
        (row["predictors"][label] > row["current"])
        - (row["predictors"][label] < row["current"])
        for row in eligible
    ]
    actual_directions = [
        (row["target"] > row["current"]) - (row["target"] < row["current"])
        for row in eligible
    ]
    direction_accuracy = (
        None if not any(predicted_directions)
        else round(sum(predicted == actual for predicted, actual in zip(
            predicted_directions, actual_directions,
        )) / len(eligible), 4)
    )
    return {
        "rows": len(eligible),
        "base_dates": len(daily),
        "row_mae_krw_per_kg": round(statistics.fmean(absolute), 2),
        "date_macro_mae_krw_per_kg": round(statistics.fmean(daily_mae.values()), 2),
        "date_macro_mae_moving_block_bootstrap_95_ci": _moving_block_bootstrap_ci(daily_mae, seed),
        "median_absolute_error_krw_per_kg": round(statistics.median(absolute), 2),
        "bias_prediction_minus_actual_krw_per_kg": round(statistics.fmean(errors), 2),
        "direction_accuracy": direction_accuracy,
    }


def _paired_delta(rows: list[dict], label: str, seed: int) -> dict | None:
    paired = [row for row in rows if row["predictors"][label] is not None]
    if label == "persistence" or not paired:
        return None
    differences = defaultdict(list)
    for row in paired:
        candidate_error = abs(row["predictors"][label] - row["target"])
        persistence_error = abs(row["current"] - row["target"])
        differences[row["base_date"]].append(candidate_error - persistence_error)
    date_delta = {day: statistics.fmean(values) for day, values in differences.items()}
    mean_delta = statistics.fmean(date_delta.values())
    ci = _moving_block_bootstrap_ci(date_delta, seed)
    return {
        "paired_rows": len(paired),
        "paired_base_dates": len(date_delta),
        "candidate_minus_persistence_date_macro_mae": round(mean_delta, 2),
        "bootstrap_95_ci": ci,
        "ci_supports_improvement": bool(ci and ci[1] < 0),
    }


def evaluate(rows_by_split: dict[str, list[dict]], contract: dict) -> dict:
    result = {
        "status": "BASELINES_ONLY_NOT_A_FORECAST_MODEL",
        "contract_status": contract["status"],
        "horizons_calendar_days": list(HORIZONS),
        "predictors": PREDICTORS,
        "cluster_unit": "base_date; multiple specifications on one date are kept together",
        "bootstrap": {
            "replicates": BOOTSTRAPS,
            "seed": SEED,
            "interval": "percentile 95% CI",
            "resampling": "circular moving blocks over calendar days; all rows from each base date stay together",
            "block_days": BLOCK_DAYS,
            "minimum_calendar_span_days": 4 * BLOCK_DAYS,
        },
        "selection_policy": "validation is the only selection split; partial 2026-2027 is diagnostic only",
        "splits": {},
    }
    for split, rows in rows_by_split.items():
        split_record = {
            "rows": len(rows),
            "base_dates": len({row["base_date"] for row in rows}),
            "base_date_range": [min(row["base_date"] for row in rows), max(row["base_date"] for row in rows)],
            "horizons": {},
        }
        for horizon in HORIZONS:
            horizon_rows = [row for row in rows if row["horizon"] == horizon]
            metrics = {}
            for index, label in enumerate(PREDICTORS):
                metric = _metrics(horizon_rows, label, SEED + horizon * 100 + index)
                if metric is not None:
                    metric["paired_vs_persistence"] = _paired_delta(
                        horizon_rows, label, SEED + horizon * 100 + index,
                    )
                metrics[label] = metric
            split_record["horizons"][str(horizon)] = {
                "labeled_rows": len(horizon_rows),
                "labeled_base_dates": len({row["base_date"] for row in horizon_rows}),
                "baselines": metrics,
            }
        result["splits"][split] = split_record
    return result


def markdown_report(result: dict, hashes: dict[str, str]) -> str:
    lines = [
        "# 가격 지속·후행 기준선 평가",
        "",
        "작성 기준일: 2026-11-16",
        "",
        f"이 평가는 모델 학습이나 운영 전망 승인 결과가 아니다. 각 예측 CSV에 이미 기록된 시점 기준 피처를 현재가 유지·후행가격 기준선과 비교했다. 같은 기준일의 여러 규격 행을 묶고 {BLOCK_DAYS}일 연속 구간을 보존하는 이동블록 bootstrap으로 95% 구간을 계산했다.",
        "",
        "## 자료 무결성과 범위",
        "",
        "| 분할 | 행 수 | 기준일 수 | 기준일 범위 | CSV SHA-256 |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for split, info in result["splits"].items():
        lines.append(
            f"| {split} | {info['rows']} | {info['base_dates']} | "
            f"{info['base_date_range'][0]} ~ {info['base_date_range'][1]} | `{hashes[split]}` |"
        )
    lines += [
        "",
        "## 검증 시즌 비교",
        "",
        f"차이는 같은 피처가 있는 행만 사용해 날짜별 MAE를 먼저 계산한 뒤 평균했다. 음수면 해당 후행 기준선이 지속예측보다 낮은 오차를 보였다는 뜻이다. 신뢰구간은 {BLOCK_DAYS}일 연속 날짜 블록을 보존해 계산했다. 구간이 0을 포함하면 우위가 분명하지 않다.",
        "",
        "| 간격 | 기준선 | 행/기준일 | MAE (원/kg) | 날짜별 paired MAE 차이 (95% CI) |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    validation = result["splits"]["validation"]["horizons"]
    for horizon in HORIZONS:
        entry = validation[str(horizon)]["baselines"]
        for label in PREDICTORS:
            metric = entry[label]
            if metric is None:
                continue
            paired = metric["paired_vs_persistence"]
            if paired is None:
                delta_text = "기준"
            else:
                ci = paired["bootstrap_95_ci"]
                if ci is None:
                    delta_text = f"{paired['candidate_minus_persistence_date_macro_mae']:+.2f} (구간 산출 자료 부족)"
                else:
                    delta_text = f"{paired['candidate_minus_persistence_date_macro_mae']:+.2f} ({ci[0]:+.2f}, {ci[1]:+.2f})"
            lines.append(
                f"| {horizon} | {label} | {metric['rows']}/{metric['base_dates']} | "
                f"{metric['row_mae_krw_per_kg']:.2f} | {delta_text} |"
            )
    lines += [
        "",
        "## 해석 제한",
        "",
        "- 모델은 학습하지 않았다. `scikit-learn` 후보 모델은 별도 의존성 문제로 평가하지 않았다.",
        "- 이 표는 정확한 대상일 경락가격이 있는 행만 평가한다. 대상일 미관측·품질경고·관측기간 밖 예측 기회는 점수에서 제외된다.",
        f"- {BLOCK_DAYS}일 연속 구간을 보존하는 날짜 단위 이동블록 bootstrap을 사용했고, 최소 4개 블록(달력 기간 {4 * BLOCK_DAYS}일)이 되는 경우에만 구간을 표시했다. 짧은 부분 시즌과 독립 시즌이 1개인 한계는 해결하지 않는다.",
        "- 몇 가지 기준선을 탐색한 결과이며 다중 비교 보정은 하지 않았다. 개선 신호를 확정적인 우위나 운영 승인으로 해석하지 않는다.",
        "- 2026-2027 부분 시즌은 탐색 이력이 있으므로 아래 JSON에는 진단용으로만 남기며 모델 선택에 사용하지 않는다.",
        "- 가격은 경락 평균이며 농가 실수취액, 보관 효과, 출하 가능성은 이 평가에 포함되지 않는다.",
        "",
        f"입력 데이터 계약 상태: `{result['contract_status']}`. 전체 지표와 학습·부분평가 분할은 `{(ROOT / 'data/reports/price_forecast_baseline_evaluation.json').relative_to(ROOT).as_posix()}`에 저장했다.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path,
                        default=ROOT / "data/reports/price_forecast_baseline_evaluation.json")
    parser.add_argument("--markdown-output", type=Path,
                        default=ROOT / "docs/11_가격전망_기준선_평가.md")
    args = parser.parse_args(argv)
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8-sig"))
    if contract.get("status") != "LABELED_DATA_NOT_TRAINED_MODEL":
        raise ValueError("예상하지 않은 학습 데이터 계약 상태입니다.")
    rows_by_split = {name: load_split(name, path, contract) for name, path in SPLITS.items()}
    hashes = {name: sha256(path) for name, path in SPLITS.items()}
    result = evaluate(rows_by_split, contract)
    result["input_sha256"] = hashes
    result["contract_sha256"] = sha256(CONTRACT_PATH)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(markdown_report(result, hashes), encoding="utf-8")
    print(f"검증·평가 기록: {args.markdown_output.relative_to(ROOT)}")
    print(f"전체 수치: {args.json_output.relative_to(ROOT)}")
    print("2026-2027 부분 시즌은 진단용이며 선택에 사용하지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
