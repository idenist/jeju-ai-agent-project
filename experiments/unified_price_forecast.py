"""Fit one citrus auction price model for user-selected future dates.

    python -m experiments.unified_price_forecast train
    python -m experiments.unified_price_forecast predict --as-of 2026-11-17 --days 2 9 30
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean, pstdev

from experiments.evaluate_multiseason_inputs import (
    PROVISIONAL_VARIETIES,
    ROOT,
    SPEC_FIELDS,
    _make_pipeline,
    _season_day,
    _sha256,
)
from experiments.forecast_bridge import EXAMPLE_SPEC, _read_source_prices, _spec_key

MODEL_ROOT = ROOT / "artifacts" / "price_forecast_unified"
MODEL_FILE = "unified_price.joblib"
CONTRACT_VERSION = 1
RECENT_DAYS = 7
MIN_SPEC_OBSERVATIONS = 10
ONE_DAY = timedelta(days=1)

SNAPSHOT_FEATURES = (
    "as_of_weekday", "as_of_season_day", "price_age_days", "log_recent_price",
    "last_price_log_ratio", "previous_week_price_log_ratio", "price_variation",
    "observed_days", "log_recent_volume", "log_last_transactions",
    "log_variety_volume", "variety_volume_log_change",
)
FEATURES = SNAPSHOT_FEATURES + ("horizon_days", "target_weekday", "target_season_day")

Spec = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Observation:
    price: float
    kg: float
    transactions: int


@dataclass(slots=True)
class Season:
    prices: dict[Spec, dict[date, Observation]] = field(default_factory=lambda: defaultdict(dict))
    variety_volume: dict[tuple[date, str, str, str], float] = field(
        default_factory=lambda: defaultdict(float)
    )


@dataclass(frozen=True, slots=True)
class Snapshot:
    spec: Spec
    as_of: date
    last_date: date
    last_price: float
    recent_price: float
    features: tuple

    def model_row(self, target: date) -> tuple:
        return (
            *self.features, (target - self.as_of).days, target.weekday(),
            _season_day(target), *self.spec,
        )


@dataclass(frozen=True, slots=True)
class Case:
    snapshot: Snapshot
    target: date
    actual: float

    @property
    def horizon(self) -> int:
        return (self.target - self.snapshot.as_of).days


def _season_start(day: date) -> int:
    return day.year - (day.month < 10)


def _dates(start: date, end: date):
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def load_history(files: list[Path], *, through: date | None = None) -> dict[int, Season]:
    seasons: dict[int, Season] = defaultdict(Season)
    for path in files:
        for row in _read_source_prices(path):
            day, spec = row["date"], row["key"]
            if (through is not None and day > through) or spec[1] in PROVISIONAL_VARIETIES:
                continue
            season = seasons[_season_start(day)]
            history = season.prices[spec]
            point = Observation(float(row["price"]), float(row["kg"]), int(row["count"]))
            if day in history:
                if history[day] != point:
                    raise ValueError(f"서로 다른 가격 자료가 겹칩니다 ({day}, {spec}).")
                continue
            history[day] = point
            season.variety_volume[(day, spec[0], spec[1], spec[-1])] += point.kg
    if not seasons:
        raise ValueError("기준일까지 품질을 확인한 경락 자료가 없습니다.")
    return dict(seasons)


def snapshot_at(season: Season, spec: Spec, as_of: date) -> Snapshot | None:
    history = season.prices.get(spec, {})
    current_days = list(_dates(as_of - timedelta(days=RECENT_DAYS - 1), as_of))
    previous_days = [day - timedelta(days=RECENT_DAYS) for day in current_days]
    current = [(day, history[day]) for day in current_days if day in history]
    if not current:
        return None
    previous = [history[day].price for day in previous_days if day in history]
    last_day, last = current[-1]
    prices = [point.price for _, point in current]
    recent = fmean(prices)
    variety = (spec[0], spec[1], spec[-1])
    current_volume = sum(season.variety_volume.get((day, *variety), 0.0) for day in current_days)
    previous_volume = sum(season.variety_volume.get((day, *variety), 0.0) for day in previous_days)
    features = (
        as_of.weekday(), _season_day(as_of), (as_of - last_day).days,
        math.log(recent), math.log(last.price / recent),
        math.log(fmean(previous) / recent) if previous else math.nan,
        pstdev(prices) / recent, len(current),
        math.log1p(sum(point.kg for _, point in current)),
        math.log1p(last.transactions), math.log1p(current_volume),
        math.log(current_volume / previous_volume) if previous_volume else math.nan,
    )
    return Snapshot(spec, as_of, last_day, last.price, recent, features)


def build_cases(season: Season) -> list[Case]:
    cases = []
    for spec, history in season.prices.items():
        observed_days = sorted(history)
        for as_of in _dates(observed_days[0], observed_days[-1] - ONE_DAY):
            snapshot = snapshot_at(season, spec, as_of)
            if snapshot is None:
                continue
            for target in observed_days[bisect_right(observed_days, as_of):]:
                cases.append(Case(snapshot, target, history[target].price))
    return cases


def training_cases(seasons: list[Season], cases: list[Case]) -> list[Case]:
    counts: Counter = Counter()
    for season in seasons:
        counts.update({spec: len(rows) for spec, rows in season.prices.items()})
    return [case for case in cases if counts[case.snapshot.spec] >= MIN_SPEC_OBSERVATIONS]


def _coverage(cases: list[Case]) -> dict[Spec, dict]:
    counts: dict[Spec, Counter] = defaultdict(Counter)
    seasons: dict[Spec, set[int]] = defaultdict(set)
    phase_bounds: dict[Spec, list[int]] = {}
    for case in cases:
        spec = case.snapshot.spec
        counts[spec][case.horizon] += 1
        seasons[spec].add(_season_start(case.target))
        phase = _season_day(case.target)
        bounds = phase_bounds.setdefault(spec, [phase, phase])
        bounds[0] = min(bounds[0], phase)
        bounds[1] = max(bounds[1], phase)
    return {
        spec: {
            "pairs_by_horizon": dict(sorted(count.items())),
            "training_seasons": len(seasons[spec]),
            "target_season_days": phase_bounds[spec],
        }
        for spec, count in counts.items()
    }


def _has_coverage(coverage: dict, as_of: date, target: date) -> bool:
    first, last = coverage["target_season_days"]
    return (
        (target - as_of).days in coverage["pairs_by_horizon"]
        and _season_start(as_of) == _season_start(target)
        and first <= _season_day(target) <= last
    )


def fit_model(cases: list[Case]):
    import numpy as np
    from threadpoolctl import threadpool_limits

    if not cases:
        raise ValueError("동일 규격으로 연결할 수 있는 과거 가격쌍이 없습니다.")
    x = np.asarray([case.snapshot.model_row(case.target) for case in cases], dtype=object)
    y = np.asarray([math.log(case.actual / case.snapshot.recent_price) for case in cases])
    date_counts = Counter(case.snapshot.as_of for case in cases)
    average_count = len(cases) / len(date_counts)
    weights = np.asarray([average_count / date_counts[case.snapshot.as_of] for case in cases])
    model = _make_pipeline("hist_gbdt", FEATURES).set_params(
        model__loss="absolute_error", model__early_stopping=False, model__min_samples_leaf=60,
    )
    # A date contributes the same total weight even when it has many recorded specs or leads.
    with threadpool_limits(limits=4):
        model.fit(x, y, model__sample_weight=weights)
    return model


def estimate(model, queries: list[tuple[Snapshot, date]]) -> list[float]:
    import numpy as np
    from threadpoolctl import threadpool_limits

    if not queries:
        return []
    x = np.asarray([snapshot.model_row(target) for snapshot, target in queries], dtype=object)
    with threadpool_limits(limits=4):
        deltas = model.predict(x)
    prices = [snapshot.recent_price * math.exp(delta) for (snapshot, _), delta in zip(queries, deltas)]
    if any(not math.isfinite(price) or price <= 0 for price in prices):
        raise ValueError("유효한 가격을 예측하지 못했습니다.")
    return prices


def _mean_by_date(rows: list[tuple[date, tuple]]) -> list[float]:
    grouped = defaultdict(list)
    for day, values in rows:
        grouped[day].append(values)
    daily = [tuple(map(fmean, zip(*values))) for values in grouped.values()]
    return [round(fmean(column), 2) for column in zip(*daily)]


def score_prices(cases: list[Case], predictions: list[float]) -> dict:
    errors = defaultdict(list)
    for case, predicted in zip(cases, predictions):
        snapshot = case.snapshot
        errors[case.horizon].append((snapshot.as_of, (
            abs(predicted - case.actual), abs(snapshot.recent_price - case.actual),
            abs(snapshot.last_price - case.actual),
        )))

    def summarize(rows):
        model, recent, last = _mean_by_date(rows)
        return {
            "pairs": len(rows), "base_dates": len({day for day, _ in rows}),
            "model_mae": model, "recent_7d_mae": recent, "last_price_mae": last,
        }

    return {
        "overall": summarize([row for rows in errors.values() for row in rows]),
        "by_horizon": {str(h): summarize(rows) for h, rows in sorted(errors.items())},
    }


def score_date_choices(cases: list[Case], predictions: list[float]) -> dict:
    # Conditional on the option dates having realized, quality-checked prices.
    # Define week-long windows throughout the season to compare choices at different leads.
    windows = defaultdict(list)
    for case, predicted in zip(cases, predictions):
        group = (case.snapshot.as_of, case.snapshot.spec, (case.horizon - 1) // 7)
        windows[group].append((case.target, case.actual, predicted))
    scores = []
    for (as_of, _, _), options in windows.items():
        if len(options) < 2:
            continue
        first = min(options)
        chosen = max(options, key=lambda option: (option[2], -option[0].toordinal()))
        best_actual = max(option[1] for option in options)
        scores.append((as_of, (chosen[1] - first[1], best_actual - chosen[1], best_actual - first[1])))
    if not scores:
        return {"windows": 0}
    gain, model_regret, first_regret = _mean_by_date(scores)
    return {
        "windows": len(scores),
        "base_dates": len({day for day, _ in scores}),
        "model_minus_first_krw_per_kg": gain,
        "model_gap_from_best_krw_per_kg": model_regret,
        "first_gap_from_best_krw_per_kg": first_regret,
    }


def evaluate(seasons: dict[int, Season], cases: dict[int, list[Case]]) -> list[dict]:
    folds = []
    previous: list[int] = []
    for year in sorted(seasons):
        if previous:
            training = training_cases([seasons[y] for y in previous], [c for y in previous for c in cases[y]])
            coverage = _coverage(training)
            held_out = [
                c for c in cases[year]
                if (support := coverage.get(c.snapshot.spec)) and _has_coverage(support, c.snapshot.as_of, c.target)
            ]
            fold = {
                "test_season": f"{year}-{year + 1}",
                "train_seasons": [f"{y}-{y + 1}" for y in previous],
                "observed_through": max(day for rows in seasons[year].prices.values() for day in rows).isoformat(),
                "training_pairs": len(training),
                "available_test_pairs": len(cases[year]),
                "scored_test_pairs": len(held_out),
            }
            if held_out:
                model = fit_model(training)
                predicted = estimate(model, [(case.snapshot, case.target) for case in held_out])
                fold["prices_krw_per_kg"] = score_prices(held_out, predicted)
                fold["date_choice"] = score_date_choices(held_out, predicted)
            folds.append(fold)
        previous.append(year)
    return folds


def train_bundle(files: list[Path], output: Path = MODEL_ROOT, *, through: date | None = None) -> Path:
    import joblib
    import sklearn

    seasons = load_history(files, through=through)
    cases = {year: build_cases(season) for year, season in seasons.items()}
    folds = evaluate(seasons, cases)
    training = training_cases(list(seasons.values()), [case for rows in cases.values() for case in rows])
    model = fit_model(training)
    coverage = _coverage(training)
    through_observed = max(day for season in seasons.values() for rows in season.prices.values() for day in rows)
    bundle = output / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ")
    bundle.mkdir(parents=True, exist_ok=False)
    file = bundle / MODEL_FILE
    joblib.dump(model, file, compress=3)
    sample = training[0]
    estimate(joblib.load(file), [(sample.snapshot, sample.target)])
    metadata = {
        "contract_version": CONTRACT_VERSION,
        "status": "RESEARCH_ONLY",
        "model": MODEL_FILE,
        "model_sha256": _sha256(file),
        "scikit_learn": sklearn.__version__,
        "features": list(FEATURES + SPEC_FIELDS),
        "target": "log of future exact-spec auction price divided by the recent 7-calendar-day price mean",
        "training_data_through": through_observed.isoformat(),
        "training_pairs": len(training),
        "training_base_dates": len({case.snapshot.as_of for case in training}),
        "horizon_range_days": [min(c.horizon for c in training), max(c.horizon for c in training)],
        "spec_coverage": [
            {"spec": dict(zip(SPEC_FIELDS, spec)), **support}
            for spec, support in sorted(coverage.items())
        ],
        "sources": [{"file": str(path.resolve()), "sha256": _sha256(path)} for path in files],
        "evaluation": {
            "method": "Each season is predicted using earlier seasons only. Price and choice metrics average within base date first.",
            "scope": "Retrospective. Choice compares observed candidate days in seven-day windows by gross auction price.",
            "folds": folds,
        },
    }
    metrics_file = bundle / "metrics.json"
    pending = metrics_file.with_suffix(".tmp")
    pending.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(metrics_file)
    return bundle


def predict_dates(*, files: list[Path], bundle: Path, spec: dict, as_of: date, targets: list[date]) -> dict:
    import joblib
    import sklearn

    meta = json.loads((bundle / "metrics.json").read_text(encoding="utf-8"))
    if (
        meta.get("contract_version") != CONTRACT_VERSION
        or meta.get("status") != "RESEARCH_ONLY"
        or meta.get("features") != list(FEATURES + SPEC_FIELDS)
        or meta.get("scikit_learn") != sklearn.__version__
    ):
        raise ValueError("모델과 현재 실행 환경의 버전 또는 입력 형식이 다릅니다.")
    if as_of < date.fromisoformat(meta["training_data_through"]):
        raise ValueError("학습 종료일보다 이른 기준일입니다. 그 날짜로 --train-through를 지정해 학습하세요.")
    key = _spec_key(spec)
    support = next((item for item in meta["spec_coverage"] if _spec_key(item["spec"]) == key), None)
    if support is None:
        raise ValueError("이 상품 규격은 학습에 사용된 거래 기록이 부족합니다.")
    support["pairs_by_horizon"] = {int(h): count for h, count in support["pairs_by_horizon"].items()}
    history = load_history(files, through=as_of)
    season = history.get(_season_start(as_of))
    snapshot = snapshot_at(season, key, as_of) if season else None
    if snapshot is None:
        raise ValueError("기준일까지 최근 7일 안에 확인된 동일 규격 가격이 없습니다.")

    result = []
    scored: list[tuple[Snapshot, date]] = []
    for target in sorted(set(targets)):
        horizon = (target - as_of).days
        if horizon < 1:
            raise ValueError("예측 날짜는 모두 기준일 이후여야 합니다.")
        item = {"date": target.isoformat(), "days_ahead": horizon}
        if _has_coverage(support, as_of, target):
            item["training_pairs_at_horizon"] = support["pairs_by_horizon"][horizon]
            scored.append((snapshot, target))
        else:
            item["status"] = "OUTSIDE_OBSERVED_COVERAGE"
        result.append(item)
    if scored:
        model_file = bundle / MODEL_FILE
        if _sha256(model_file) != meta["model_sha256"]:
            raise ValueError("저장된 모델 파일의 해시가 학습 기록과 다릅니다.")
        prices = iter(estimate(joblib.load(model_file), scored))
        for item in result:
            if "training_pairs_at_horizon" in item:
                item.update(status="ESTIMATED", auction_price_krw_per_kg=round(next(prices), 2))
    return {
        "status": "RESEARCH_ONLY",
        "as_of": as_of.isoformat(),
        "last_observed_price_date": snapshot.last_date.isoformat(),
        "last_observed_price_krw_per_kg": snapshot.last_price,
        "age_of_last_observed_price_days": (as_of - snapshot.last_date).days,
        "recent_7d_average_price_krw_per_kg": round(snapshot.recent_price, 2),
        "model_bundle": str(bundle.resolve()),
        "spec": dict(zip(SPEC_FIELDS, key)),
        "forecasts": result,
        "price_basis": "지정 상품이 후보일에 거래되는 경우의 수수료 차감 전 시장 경락가",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    train = subcommands.add_parser("train", help="시즌 순서대로 검증한 후 단일 모델을 학습")
    train.add_argument("--out-dir", type=Path, default=MODEL_ROOT)
    train.add_argument("--train-through", type=date.fromisoformat, help="학습에 포함할 마지막 관측일")
    predict = subcommands.add_parser("predict", help="기준일과 원하는 미래 날짜의 경락가를 조회")
    predict.add_argument("--as-of", type=date.fromisoformat, required=True)
    predict.add_argument("--model-dir", type=Path, help="이 도구로 학습한 로컬 모델 폴더")
    targets = predict.add_mutually_exclusive_group(required=True)
    targets.add_argument("--days", type=int, nargs="+", help="기준일로부터의 달력상 일수")
    targets.add_argument("--target-date", type=date.fromisoformat, nargs="+", help="예측할 날짜 (YYYY-MM-DD)")
    targets.add_argument(
        "--window", type=date.fromisoformat, nargs=2, metavar=("START", "END"),
        help="예측할 날짜 범위. 시작일과 종료일 포함",
    )
    for field_name, default in EXAMPLE_SPEC.items():
        predict.add_argument(f"--{field_name.replace('_', '-')}", default=default)
    for command in (train, predict):
        command.add_argument(
            "--price-file", action="append", type=Path,
            help="여러 번 지정할 수 있는 daily_prices.csv. 기본은 data/seasons의 가격 자료",
        )
    args = parser.parse_args(argv)
    files = args.price_file or sorted((ROOT / "data" / "seasons").glob("*/daily_prices.csv"))
    try:
        if args.command == "train":
            bundle = train_bundle(files, args.out_dir, through=args.train_through)
            print(bundle.resolve())
            return 0
        bundle = args.model_dir or max(
            (path.parent for path in MODEL_ROOT.glob("*/metrics.json")), default=None,
        )
        if bundle is None:
            raise ValueError("학습된 모델이 없습니다. train 명령을 먼저 실행하세요.")
        if args.days is not None:
            dates = [args.as_of + timedelta(days=days) for days in args.days]
        elif args.window is not None:
            dates = list(_dates(*args.window))
            if not dates:
                raise ValueError("출하 가능 기간의 시작일과 종료일을 확인하세요.")
        else:
            dates = args.target_date
        result = predict_dates(
            files=files, bundle=bundle, spec={name: getattr(args, name) for name in SPEC_FIELDS},
            as_of=args.as_of, targets=dates,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if any(row["status"] == "ESTIMATED" for row in result["forecasts"]) else 1
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        print(f"가격 예측 실험 실패: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
