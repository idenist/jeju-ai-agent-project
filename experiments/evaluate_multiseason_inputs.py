"""Walk-forward evaluation of citrus price baselines and optional supply features.

Reads only the checked-in seasonal daily-price tables and the existing Jeju
DataHub #855 shipment snapshot. It does not select a deployment model or alter
the Agent. All features are built using observations available on or before the
forecast base date; targets are exact same-spec prices at the calendar horizon.

Run from the project root:
    python -m experiments.evaluate_multiseason_inputs
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEASONS = ("2023-2024", "2024-2025", "2025-2026", "2026-2027")
HORIZONS = (3, 7, 10, 14)
SPEC_FIELDS = (
    "market_code", "variety_code", "grade_code", "size_code",
    "package", "package_kg", "trade_type",
)
PROVISIONAL_VARIETIES = {"00", "07", "08", "09", "99"}
BASE_NUMERIC = (
    "price", "own_volume_kg", "own_transactions", "weekday", "month",
    "season_day_index", "lag3_price", "lag7_price", "trailing7_mean",
    "trailing7_volume_kg", "trailing7_observed_days",
)
AUCTION_CONTEXT = (
    "market_variety_volume_kg", "market_variety_price_kg",
    "market_citrus_volume_kg", "market_citrus_price_kg",
)
DATAHUB_CONTEXT = (
    "datahub_today_tons", "datahub_trailing7_tons", "datahub_observed_days7",
)
VARIANTS = {
    "price_flow": BASE_NUMERIC,
    "auction_context": BASE_NUMERIC + AUCTION_CONTEXT,
    "datahub_proxy": BASE_NUMERIC + DATAHUB_CONTEXT,
    "auction_plus_datahub": BASE_NUMERIC + AUCTION_CONTEXT + DATAHUB_CONTEXT,
}
FOLDS = (
    {"test": "2024-2025", "train": ("2023-2024",), "role": "holdout"},
    {"test": "2025-2026", "train": ("2023-2024", "2024-2025"), "role": "holdout"},
    {"test": "2026-2027", "train": ("2023-2024", "2024-2025", "2025-2026"), "role": "previously_explored_diagnostic"},
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _number(value: str, *, default: float | None = None) -> float | None:
    try:
        parsed = float(value.replace(",", "").strip())
    except (AttributeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _canon(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number.is_integer() else format(number, ".6g")


def _season_day(day: date) -> int:
    season_start = date(day.year if day.month >= 10 else day.year - 1, 10, 1)
    return (day - season_start).days


def load_price_data() -> tuple[dict[str, list[dict]], dict[str, dict], dict[str, str]]:
    clean_by_season: dict[str, list[dict]] = {}
    aggregate_by_season: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    for season in SEASONS:
        path = ROOT / "data" / "seasons" / season / "daily_prices.csv"
        if not path.is_file():
            continue
        hashes[season] = _sha256(path)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"date", *SPEC_FIELDS, "avg_price_kg", "total_kg",
                        "total_amount_krw", "transaction_count", "quality_flag"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError(f"{path}: 필수 열 누락")
            rows = list(reader)

        clean_rows = []
        aggregate_parts: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
        all_dates: set[str] = set()
        clean_dates: set[str] = set()
        quality = defaultdict(int)
        for raw in rows:
            quality[(raw.get("quality_flag") or "").strip() or "(blank)"] += 1
            day = date.fromisoformat(raw["date"])
            spec = tuple(_canon(raw[field]) for field in SPEC_FIELDS)
            price = _number(raw["avg_price_kg"])
            volume = _number(raw["total_kg"])
            amount = _number(raw["total_amount_krw"])
            transactions = _number(raw["transaction_count"])
            if price is None or volume is None or amount is None or transactions is None:
                continue
            if price <= 0 or volume < 0 or amount < 0 or transactions < 0:
                continue
            all_dates.add(day.isoformat())
            market, variety = spec[0], spec[1]
            for key in ((day, market, "*"), (day, market, variety)):
                aggregate_parts[key][0] += volume
                aggregate_parts[key][1] += amount
                aggregate_parts[key][2] += 1
            if (raw.get("quality_flag") or "").strip() != "OK":
                continue
            clean_dates.add(day.isoformat())
            clean_rows.append({
                "date": day, "spec": spec, "price": price, "volume": volume,
                "amount": amount, "transactions": transactions,
            })

        aggregates = {}
        for key, (volume, amount, row_count) in aggregate_parts.items():
            aggregates[key] = {
                "volume": volume,
                "weighted_price": amount / volume if volume > 0 else None,
                "row_count": int(row_count),
            }
        clean_by_season[season] = clean_rows
        aggregate_by_season[season] = {
            "rows": len(rows), "dates": len(all_dates), "clean_rows": len(clean_rows),
            "clean_dates": len(clean_dates), "clean_specs": len({r["spec"] for r in clean_rows}),
            "date_min": min(all_dates) if all_dates else None,
            "date_max": max(all_dates) if all_dates else None,
            "observed_dates": {date.fromisoformat(value) for value in all_dates},
            "quality_flags": dict(sorted(quality.items())),
            "aggregates": aggregates,
        }
    return clean_by_season, aggregate_by_season, hashes


def load_datahub(path: Path | None = None) -> tuple[dict[date, float], dict]:
    path = path or (ROOT / "data" / "raw" / "jeju_shipments.csv")
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    from scripts.preprocess_shipments import read_source, parse_tons

    rows, encoding = read_source(path)
    daily: dict[date, float] = defaultdict(float)
    valid_dates: set[date] = set()
    missing_date_rows = 0
    for row in rows:
        value = parse_tons(row["출하량"])
        if value is None:
            continue
        if not row["출하일자"]:
            missing_date_rows += 1
            continue
        day = date.fromisoformat(row["출하일자"])
        daily[day] += float(value)
        valid_dates.add(day)
    try:
        display_path = path.relative_to(ROOT).as_posix()
    except ValueError:
        display_path = path.name
    info = {
        "source": "Jeju DataHub #855 static source snapshot",
        "path": display_path,
        "sha256": _sha256(path),
        "encoding": encoding,
        "dated_source_rows": sum(1 for row in rows if row["출하일자"]),
        "missing_date_rows_excluded": missing_date_rows,
        "observed_days": len(valid_dates),
        "first_date": min(valid_dates).isoformat() if valid_dates else None,
        "last_date": max(valid_dates).isoformat() if valid_dates else None,
        "daily_values_are_sum_across_reported_varieties": True,
        "not_market_arrivals": True,
    }
    return dict(daily), info


def build_samples(clean_by_season: dict[str, list[dict]], aggregate_by_season: dict[str, dict],
                  shipments: dict[date, float]) -> dict[str, dict[int, list[dict]]]:
    result: dict[str, dict[int, list[dict]]] = {}
    for season, clean_rows in clean_by_season.items():
        by_spec: dict[tuple, dict[date, dict]] = defaultdict(dict)
        for row in clean_rows:
            by_spec[row["spec"]][row["date"]] = row
        aggregates = aggregate_by_season[season]["aggregates"]
        horizon_rows = {h: [] for h in HORIZONS}
        for spec, dated in by_spec.items():
            for day, row in dated.items():
                trailing = [dated[d] for d in sorted(dated)
                            if day - timedelta(days=6) <= d <= day]
                trailing_mean = statistics.fmean(item["price"] for item in trailing)
                variety_agg = aggregates.get((day, spec[0], spec[1]), {})
                market_agg = aggregates.get((day, spec[0], "*"), {})
                datahub_days = [d for d in shipments if day - timedelta(days=6) <= d <= day]
                datahub_total = sum(shipments[d] for d in datahub_days) if datahub_days else None
                numbers = {
                    "price": row["price"], "own_volume_kg": row["volume"],
                    "own_transactions": row["transactions"], "weekday": day.weekday(),
                    "month": day.month, "season_day_index": _season_day(day),
                    "lag3_price": dated.get(day - timedelta(days=3), {}).get("price"),
                    "lag7_price": dated.get(day - timedelta(days=7), {}).get("price"),
                    "trailing7_mean": trailing_mean,
                    "trailing7_volume_kg": sum(item["volume"] for item in trailing),
                    "trailing7_observed_days": len(trailing),
                    "market_variety_volume_kg": variety_agg.get("volume"),
                    "market_variety_price_kg": variety_agg.get("weighted_price"),
                    "market_citrus_volume_kg": market_agg.get("volume"),
                    "market_citrus_price_kg": market_agg.get("weighted_price"),
                    "datahub_today_tons": shipments.get(day),
                    "datahub_trailing7_tons": datahub_total,
                    "datahub_observed_days7": len(datahub_days),
                }
                for horizon in HORIZONS:
                    target = dated.get(day + timedelta(days=horizon))
                    if target is None:
                        continue
                    horizon_rows[horizon].append({
                        "season": season, "date": day, "target_date": day + timedelta(days=horizon),
                        "spec": spec, "numbers": numbers, "current": row["price"],
                        "target": target["price"], "trailing7": trailing_mean,
                        "lag3": numbers["lag3_price"], "lag7": numbers["lag7_price"],
                    })
        result[season] = horizon_rows
    return result


def _make_pipeline(kind: str, numeric_fields: tuple[str, ...]):
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    num_steps = [("impute", SimpleImputer(strategy="median", add_indicator=True))]
    if kind == "ridge":
        num_steps.append(("scale", StandardScaler()))
    preparation = ColumnTransformer([
        ("numeric", Pipeline(num_steps), list(range(len(numeric_fields)))),
        ("categorical", OneHotEncoder(handle_unknown="ignore", sparse_output=False),
         list(range(len(numeric_fields), len(numeric_fields) + len(SPEC_FIELDS)))),
    ], sparse_threshold=0)
    if kind == "ridge":
        estimator = Ridge(alpha=100.0)
    elif kind == "hist_gbdt":
        estimator = HistGradientBoostingRegressor(
            max_iter=120, max_leaf_nodes=9, min_samples_leaf=25,
            learning_rate=0.04, l2_regularization=100.0, random_state=42,
        )
    elif kind == "extra_trees":
        estimator = ExtraTreesRegressor(
            n_estimators=100, max_depth=7, min_samples_leaf=8,
            max_features=0.8, random_state=42, n_jobs=1,
        )
    else:
        raise ValueError(kind)
    return Pipeline([("prepare", preparation), ("model", estimator)])


def _matrix(rows: list[dict], fields: tuple[str, ...], np):
    values = []
    for row in rows:
        numbers = [row["numbers"].get(name) for name in fields]
        values.append(numbers + list(row["spec"]))
    return np.asarray(values, dtype=object)


def _date_macro_mae(rows: list[dict], predictions: list[float]) -> float:
    per_day: dict[date, list[float]] = defaultdict(list)
    for row, prediction in zip(rows, predictions):
        per_day[row["date"]].append(abs(prediction - row["target"]))
    return statistics.fmean(statistics.fmean(values) for values in per_day.values())


def _bootstrap_delta(rows: list[dict], predictions: list[float], baseline_key: str,
                     seed: int, replicates: int = 800, block_days: int = 14) -> list[float] | None:
    per_day: dict[date, list[float]] = defaultdict(list)
    for row, prediction in zip(rows, predictions):
        baseline = row.get(baseline_key)
        if baseline is not None:
            per_day[row["date"]].append(abs(prediction - row["target"]) - abs(baseline - row["target"]))
    daily = {day: statistics.fmean(values) for day, values in per_day.items()}
    if not daily:
        return None
    start, end = min(daily), max(daily)
    calendar_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if len(calendar_days) < 4 * block_days or len(daily) < 20:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        sample = []
        while len(sample) < len(calendar_days):
            block_start = rng.randrange(len(calendar_days))
            for offset in range(block_days):
                day = calendar_days[(block_start + offset) % len(calendar_days)]
                if day in daily:
                    sample.append(daily[day])
                if len(sample) >= len(calendar_days):
                    break
        estimates.append(statistics.fmean(sample))
    estimates.sort()
    return [round(estimates[int(0.025 * (len(estimates) - 1))], 2),
            round(estimates[int(0.975 * (len(estimates) - 1))], 2)]


def _score(rows: list[dict], predictions: list[float], *, baseline_key: str | None = None,
           seed: int = 42) -> dict:
    errors = [prediction - row["target"] for row, prediction in zip(rows, predictions)]
    predicted_moves = [((prediction > row["current"]) - (prediction < row["current"]))
                       for row, prediction in zip(rows, predictions)]
    actual_moves = [((row["target"] > row["current"]) - (row["target"] < row["current"]))
                    for row in rows]
    result = {
        "n": len(rows), "base_dates": len({row["date"] for row in rows}),
        "row_mae_krw_per_kg": round(statistics.fmean(abs(error) for error in errors), 2),
        "date_macro_mae_krw_per_kg": round(_date_macro_mae(rows, predictions), 2),
        "bias_krw_per_kg": round(statistics.fmean(errors), 2),
        "direction_accuracy": (
            None if all(move == 0 for move in predicted_moves)
            else round(statistics.fmean(float(predicted == actual)
                                        for predicted, actual in zip(predicted_moves, actual_moves)), 4)
        ),
    }
    if baseline_key:
        ci = _bootstrap_delta(rows, predictions, baseline_key, seed)
        paired = [(row, prediction) for row, prediction in zip(rows, predictions)
                  if row.get(baseline_key) is not None]
        if paired:
            paired_delta = statistics.fmean(
                abs(prediction - row["target"]) - abs(row[baseline_key] - row["target"])
                for row, prediction in paired
            )
            daily_delta: dict[date, list[float]] = defaultdict(list)
            for row, prediction in paired:
                daily_delta[row["date"]].append(
                    abs(prediction - row["target"]) - abs(row[baseline_key] - row["target"])
                )
            date_macro_delta = statistics.fmean(
                statistics.fmean(values) for values in daily_delta.values()
            )
            result["paired_n_vs_trailing7"] = len(paired)
            result["row_mae_delta_vs_trailing7"] = round(paired_delta, 2)
            result["date_macro_mae_delta_vs_trailing7"] = round(date_macro_delta, 2)
            result["date_block_bootstrap_95_ci_delta"] = ci
    return result


def _paired_model_comparison(rows: list[dict], candidate: list[float], reference: list[float],
                            seed: int) -> dict:
    daily: dict[date, list[float]] = defaultdict(list)
    row_delta = []
    shadow_rows = []
    for row, prediction, reference_prediction in zip(rows, candidate, reference):
        difference = abs(prediction - row["target"]) - abs(reference_prediction - row["target"])
        row_delta.append(difference)
        daily[row["date"]].append(difference)
        shadow_rows.append({**row, "_reference_prediction": reference_prediction})
    date_delta = statistics.fmean(statistics.fmean(values) for values in daily.values())
    ci = _bootstrap_delta(shadow_rows, candidate, "_reference_prediction", seed)
    return {
        "n": len(rows), "base_dates": len(daily),
        "row_mae_delta_candidate_minus_reference": round(statistics.fmean(row_delta), 2),
        "date_macro_mae_delta_candidate_minus_reference": round(date_delta, 2),
        "date_block_bootstrap_95_ci_delta": ci,
        "ci_supports_lower_candidate_error": bool(ci and ci[1] < 0),
    }


def evaluate(samples: dict[str, dict[int, list[dict]]], seasons: dict[str, dict],
             shipment_info: dict, shipments: dict[date, float]) -> dict:
    import numpy as np

    evaluations = []
    models = ("ridge", "hist_gbdt", "extra_trees")
    for fold_index, fold in enumerate(FOLDS):
        test_season = fold["test"]
        if test_season not in samples:
            continue
        train_seasons = [season for season in fold["train"] if season in samples]
        if not train_seasons:
            continue
        train_counts = Counter()
        for season in train_seasons:
            for row in seasons[season]["clean_rows_for_selection"]:
                if row["spec"][1] not in PROVISIONAL_VARIETIES:
                    train_counts[row["spec"]] += 1
        eligible_specs = {spec for spec, count in train_counts.items() if count >= 10}
        for horizon in HORIZONS:
            train_rows = [row for season in train_seasons for row in samples[season][horizon]
                          if row["spec"] in eligible_specs]
            test_rows = [row for row in samples[test_season][horizon]
                         if row["spec"] in eligible_specs]
            if not train_rows or not test_rows:
                continue
            for row in test_rows:
                row["persistence"] = row["current"]
                row["trailing7_mean"] = row["trailing7"]
            results = {}
            for baseline in ("persistence", "trailing7_mean", "lag3", "lag7"):
                available = [row for row in test_rows if row.get(baseline) is not None]
                if available:
                    preds = [row[baseline] for row in available]
                    results[baseline] = _score(available, preds)

            model_predictions = {}
            for variant, fields in VARIANTS.items():
                train_feature_coverage = statistics.fmean(
                    1.0 if all(row["numbers"].get(field) is not None for field in DATAHUB_CONTEXT[:2]) else 0.0
                    for row in train_rows
                ) if train_rows else 0.0
                test_feature_coverage = statistics.fmean(
                    1.0 if all(row["numbers"].get(field) is not None for field in DATAHUB_CONTEXT[:2]) else 0.0
                    for row in test_rows
                ) if test_rows else 0.0
                uses_hub = any(field in fields for field in DATAHUB_CONTEXT)
                if uses_hub and (train_feature_coverage < 0.70 or test_feature_coverage < 0.70):
                    continue
                x_train = _matrix(train_rows, fields, np)
                x_test = _matrix(test_rows, fields, np)
                y_train = np.asarray([row["target"] - row["current"] for row in train_rows], dtype=float)
                model_names = models if variant == "price_flow" else ("hist_gbdt",)
                for model_name in model_names:
                    model = _make_pipeline(model_name, fields)
                    model.fit(x_train, y_train)
                    guesses = [max(0.0, row["current"] + float(delta))
                               for row, delta in zip(test_rows, model.predict(x_test))]
                    label = f"{variant}:{model_name}"
                    model_predictions[label] = guesses
                    results[label] = _score(
                        test_rows, guesses, baseline_key="trailing7_mean",
                        seed=20261117 + fold_index * 1000 + horizon * 10 + len(results),
                    )
            comparisons = {}
            comparison_pairs = (
                ("auction_context:hist_gbdt", "price_flow:hist_gbdt"),
                ("datahub_proxy:hist_gbdt", "price_flow:hist_gbdt"),
                ("auction_plus_datahub:hist_gbdt", "auction_context:hist_gbdt"),
            )
            for candidate_label, reference_label in comparison_pairs:
                if candidate_label in model_predictions and reference_label in model_predictions:
                    comparisons[f"{candidate_label} vs {reference_label}"] = _paired_model_comparison(
                        test_rows, model_predictions[candidate_label], model_predictions[reference_label],
                        20261117 + fold_index * 2000 + horizon * 20 + len(comparisons),
                    )
            evaluations.append({
                "test_season": test_season, "train_seasons": train_seasons,
                "fold_role": fold["role"], "horizon_days": horizon,
                "eligible_train_specs": len(eligible_specs), "train_rows": len(train_rows),
                "test_rows": len(test_rows), "test_base_dates": len({r["date"] for r in test_rows}),
                "test_date_range": [min(r["date"] for r in test_rows).isoformat(),
                                    max(r["date"] for r in test_rows).isoformat()],
                "datahub_test_rows_with_7d_signal": sum(
                    1 for r in test_rows if r["numbers"].get("datahub_trailing7_tons") is not None
                ),
                "results": results, "feature_ablation_comparisons": comparisons,
            })
    return {
        "status": "RESEARCH_WALK_FORWARD_NOT_APPROVED_FOR_AGENT",
        "prediction_target": "exact same-spec auction price at base date + calendar horizon",
        "clean_price_rule": "only quality_flag == OK rows are eligible for current and target prices",
        "feature_availability": "features use same-date observations and trailing observations through base date only",
        "horizons_days": list(HORIZONS),
        "fold_policy": "2024-2025 and 2025-2026 are rolling-origin holdouts; 2026-2027 is previously explored diagnostic only",
        "model_selection": "none; all fixed models and feature variants are reported; no winner is selected",
        "datahub_note": "DataHub shipment feature is an all-variety reported shipment proxy, not market arrivals; eligible only where source dates cover both train and test.",
        "datahub": shipment_info,
        "season_data": {
            season: {
                key: info[key] for key in (
                    "rows", "dates", "clean_rows", "clean_dates", "clean_specs",
                    "date_min", "date_max", "quality_flags", "sha256",
                ) if key in info
            }
            for season, info in seasons.items()
        },
        "shipment_price_date_overlap": {
            season: {
                "price_observed_days": info["dates"],
                "price_dates_with_shipment_record": len(info["observed_dates"] & set(shipments)),
                "price_date_coverage": round(
                    len(info["observed_dates"] & set(shipments)) / info["dates"], 4
                ) if info["dates"] else None,
            }
            for season, info in seasons.items()
        },
        "input_hashes": {season: info["sha256"] for season, info in seasons.items()
                         if info.get("sha256")},
        "evaluations": evaluations,
    }


def render_markdown(result: dict) -> str:
    lines = [
        "# 제주 감귤 가격 전망 다중 시즌 평가",
        "",
        "작성 기준일: 2026-11-17.",
        "이번 평가는 연구용 시간순 분할이며 Agent에 모델을 연결하지 않는다.",
        "",
        "## 자료 범위",
        "",
        "| 시즌 | 전체 행 | 전체 거래일 | 정상 가격 행 | 정상 거래일 | 규격 수 | 기간 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for season, info in result["season_data"].items():
        lines.append(
            f"| {season} | {info['rows']} | {info['dates']} | {info['clean_rows']} | "
            f"{info['clean_dates']} | {info['clean_specs']} | {info['date_min']} ~ {info['date_max']} |"
        )
    hub = result["datahub"]
    lines += [
        "",
        f"제주데이터허브 #855 원본은 {hub['first_date']} ~ {hub['last_date']}까지 "
        f"날짜가 있는 {hub['observed_days']}일을 포함한다. 날짜 없는 행 {hub['missing_date_rows_excluded']}개는 "
        "모델 피처에 넣지 않았다. 출하 신고량은 경매시장 반입량과 같은 값으로 간주하지 않았다.",
        "",
        "| 시즌 | 가격 관측일 | 출하량 기록이 겹치는 가격일 | 날짜 커버리지 |",
        "|---|---:|---:|---:|",
    ]
    for season, coverage in result["shipment_price_date_overlap"].items():
        lines.append(
            f"| {season} | {coverage['price_observed_days']} | "
            f"{coverage['price_dates_with_shipment_record']} | "
            f"{coverage['price_date_coverage']:.1%} |"
        )
    lines += [
        "",
        "출하량 API를 직접 호출한 결과가 아니라 입력 CSV snapshot 평가다. API 갱신본을 전달하면 이 겹침과 입력효과를 다시 계산할 수 있다.",
        "",
        "",
        "## 평가 설계",
        "",
        "- 예측 대상은 같은 시장·품종·등급·크기·포장·거래방식의 품질 OK 경락가다. 기준일에서 달력상 3·7·10·14일 뒤의 실제 가격이 있는 쌍만 사용했다.",
        "- 기본 피처는 현재가, 동일 규격의 3·7일 시차 가격, 최근 7일 가격 평균·물량, 거래량, 요일·월·시즌 경과일이다. 피처는 기준일까지의 자료만 사용했다.",
        "- 시장 맥락 피처는 같은 시장의 해당 품종 물량·가중평균가와 전체 감귤 물량·가중평균가다. 경매 원천정보는 경락가와 거래중량·품종 정보를 제공하므로, 이 신호는 기존 가격 자료에서 만들 수 있다. [공식 경락가격 API](https://www.data.go.kr/data/15109233/openapi.do)",
        "- DataHub 피처는 날짜가 있는 신고 출하량을 모든 출하 품종에 대해 합산한 당일·최근 7일 proxy다. 경매 품종과 DataHub 품종을 임의로 대응하지 않았다.",
        "- 각 시간 분할에서 Ridge·HistGradientBoosting·ExtraTrees를 고정 설정으로 비교했다. 입력 구성 효과는 같은 HistGradientBoosting 모델의 동일 테스트 행끼리 짝지어 비교했다.",
        "",
        "## 시간순 평가 결과",
        "",
        "MAE는 원/kg. `vs trailing7`의 음수는 같은 평가 행에서 최근 7일 평균보다 오차가 낮았음을 뜻한다. "
        "괄호의 날짜 블록 bootstrap 95% 구간은 구간을 산출할 만큼 날짜가 충분한 경우만 제공한다.",
        "",
        "| 평가 시즌 | 학습 시즌 | 간격 | 입력 구성·모델 | 행/날짜 | MAE | 날짜 평균 MAE | 편향 | 방향 일치 | vs 7일 평균 (95% CI) |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    shown = {
        "persistence", "trailing7_mean", "price_flow:ridge", "price_flow:hist_gbdt",
        "price_flow:extra_trees", "auction_context:hist_gbdt",
        "datahub_proxy:hist_gbdt", "auction_plus_datahub:hist_gbdt",
    }
    for fold in result["evaluations"]:
        for label, metric in fold["results"].items():
            if label not in shown:
                continue
            delta = metric.get("date_macro_mae_delta_vs_trailing7")
            ci = metric.get("date_block_bootstrap_95_ci_delta")
            delta_text = "—"
            direction_text = (
                "—" if metric["direction_accuracy"] is None
                else f"{metric['direction_accuracy']:.3f}"
            )
            if delta is not None:
                delta_text = f"{delta:+.2f}"
                if ci:
                    delta_text += f" ({ci[0]:+.2f}, {ci[1]:+.2f})"
            lines.append(
                f"| {fold['test_season']} ({fold['fold_role']}) | {', '.join(fold['train_seasons'])} | "
                f"{fold['horizon_days']} | {label} | {metric['n']}/{metric['base_dates']} | "
                f"{metric['row_mae_krw_per_kg']:.2f} | {metric['date_macro_mae_krw_per_kg']:.2f} | "
                f"{metric['bias_krw_per_kg']:+.2f} | {direction_text} | {delta_text} |"
            )
    lines += [
        "",
        "## 피처 추가의 짝비교",
        "",
        "같은 테스트 행에 대한 HistGradientBoosting 오차를 입력 구성끼리 직접 비교했다. 음수면 추가 입력 쪽 오차가 낮다.",
        "",
        "| 평가 시즌 | 간격 | 입력 추가 비교 | 행/날짜 | 날짜 평균 MAE 차이 (95% CI) | 개선 지지 |",
        "|---|---:|---|---:|---:|---|",
    ]
    for fold in result["evaluations"]:
        for comparison, metric in fold["feature_ablation_comparisons"].items():
            ci = metric["date_block_bootstrap_95_ci_delta"]
            delta = metric["date_macro_mae_delta_candidate_minus_reference"]
            interval = "자료 부족" if ci is None else f"{ci[0]:+.2f}, {ci[1]:+.2f}"
            supported = "예" if metric["ci_supports_lower_candidate_error"] else "아니오"
            lines.append(
                f"| {fold['test_season']} ({fold['fold_role']}) | {fold['horizon_days']} | {comparison} | "
                f"{metric['n']}/{metric['base_dates']} | {delta:+.2f} ({interval}) | {supported} |"
            )
    hub_supported = []
    hub_holdouts = sorted({
        fold["test_season"] for fold in result["evaluations"]
        if fold["fold_role"] == "holdout" and fold["datahub_test_rows_with_7d_signal"] > 0
    })
    for fold in result["evaluations"]:
        if fold["fold_role"] != "holdout":
            continue
        for comparison, metric in fold["feature_ablation_comparisons"].items():
            if "datahub" in comparison and metric["ci_supports_lower_candidate_error"]:
                ci = metric["date_block_bootstrap_95_ci_delta"]
                hub_supported.append(
                    f"{fold['test_season']} {fold['horizon_days']}일 {comparison}: "
                    f"{metric['date_macro_mae_delta_candidate_minus_reference']:+.2f}원/kg "
                    f"(95% CI {ci[0]:+.2f}~{ci[1]:+.2f})"
                )
    lines += [
        "",
        "## 현재 판정",
        "",
        "- 최신 두 개의 온전한 가격 시즌에서 3일 전망은 모델 후보가 7일 평균에 가까워지거나 낮은 오차를 냈지만, 날짜 블록 신뢰구간은 개선을 확정하지 못했다.",
        "- 7·10·14일 전망은 시즌에 따라 기준선보다 좋아지거나 나빠졌다. 안정적으로 이긴 간격이 없어서 날짜 추천에 쓰지 않는다.",
        f"- DataHub 피처가 검증된 holdout: {', '.join(hub_holdouts) if hub_holdouts else '없음'}. "
        "API를 새로 호출하지 않고 저장된 CSV snapshot만 사용했다. 저장소에는 CSV 전처리기는 있지만 API 수집기·인증 설정·호출 명세는 없다.",
        "- 사용자는 2026-11-17에 #855 제공 페이지가 갱신되지 않고 있음을 확인했다. 현재 스냅샷 갱신본을 요청하는 계획은 중단하고, 해당 자료는 마지막 관측일까지의 역사 참고 자료로 취급한다.",
        ("- DataHub 입력 효과 중 신뢰구간이 0 아래에 놓인 비교: " + "; ".join(hub_supported) + ". "
         "단일 시즌·여러 비교에서 나온 작은 신호라 재현 검증 전에는 채택 근거로 보지 않는다.")
        if hub_supported else "- DataHub 피처를 추가해 오차가 안정적으로 낮아졌다고 지지하는 비교는 없었다.",
        "",
        "## 해석 조건",
        "",
        "- 학습 기간에서만 정상 규격이 10일 이상 관측된 SKU를 골라 평가했다. 테스트 시즌 관측 빈도로 SKU를 선택하지 않았다.",
        "- 가격·거래량 입력은 정산일 기준이다. 농가 실수취액, 감모·보관비, 출하 예약 가능성은 예측 대상이 아니다.",
        "- 2026-2027은 이미 확인한 부분 시즌이므로 진단 자료다. 이를 독립적인 최종 검증으로 세지 않는다.",
        "- DataHub 자료는 신고소별 산지 출하량 합계 proxy다. 시장·품종별 목적지 반입량과 다르고, 과거 시점에 API에서 언제 확정됐는지도 확인되지 않았다.",
        "- 모델 후보와 입력 구성을 고르지 않고 결과를 모두 공개한다. 여러 모델을 비교했고 다중비교 보정도 하지 않았으므로 단일 우위 신호를 운영 성능으로 일반화하지 않는다.",
        "- 가격 전망이 출하 추천으로 이어지려면 별도로 감모·보관비·수수료를 반영한 손익 시나리오와 예측 불확실성을 검증해야 한다.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path,
                        default=ROOT / "artifacts" / "forecast_research" / "multiseason_inputs.json")
    parser.add_argument("--markdown-output", type=Path,
                        default=ROOT / "docs" / "14_가격예측_다중시즌_및_출하량_평가.md")
    parser.add_argument("--datahub-input", type=Path,
                        default=ROOT / "data" / "raw" / "jeju_shipments.csv",
                        help="제주데이터허브 #855 원본 CSV (UTF-8 또는 CP949)")
    args = parser.parse_args(argv)
    clean, seasons, hashes = load_price_data()
    for season, rows in clean.items():
        seasons[season]["clean_rows_for_selection"] = rows
        seasons[season]["sha256"] = hashes.get(season)
    shipments, hub_info = load_datahub(args.datahub_input)
    samples = build_samples(clean, seasons, shipments)
    result = evaluate(samples, seasons, hub_info, shipments)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_markdown(result), encoding="utf-8")
    print(f"평가 보고서: {args.markdown_output.relative_to(ROOT)}")
    print(f"기계 판독 결과: {args.json_output}")
    print(f"평가 조합: {len(result['evaluations'])} season-horizon folds")
    print("판정: 연구용 결과이며 Agent 자동 추천에 연결하지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
