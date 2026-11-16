#!/usr/bin/env python3
"""제주 감귤 출하 시점 비교 베이스라인.

공공데이터 경매가격 CSV를 읽어 최근 관측가격을 확인하고,
사용자가 지정한 보관비·감량률로 지연 출하 손익분기점을 계산한다.
LLM·예측 모델 없이도 결과를 재현하도록 Python 표준 라이브러리만 사용한다.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRICES_FILE = PROJECT_ROOT / "data" / "processed" / "daily_prices.csv"


@dataclass(frozen=True)
# 서로 다른 날짜의 규격별 시장 관측정보를 하나의 자료형으로 묶는다.
class PriceObservation:
    """동일 거래 조건의 특정 날짜 가격 집계."""

    date: date
    market: str
    variety: str
    grade: str
    size: str
    package_kg: Decimal
    price_per_kg: Decimal
    total_kg: Decimal
    transaction_count: int
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
# 계산 엔진의 결과를 LLM과 무관한 자료형으로 분리하여 독립 테스트한다.
class BreakEvenResult:
    """즉시 출하와 지연 출하의 동일 비용 항목 제외 비교 결과."""

    current_gross: Decimal
    remaining_kg: Decimal
    extra_storage_cost: Decimal
    break_even_price_per_kg: Decimal
    required_rise_percent: Decimal
    future_net_if_given: Decimal | None
    difference_if_given: Decimal | None


# 통화·중량은 이진 부동소수 오차를 줄이기 위해 Decimal로 변환한다.
def to_decimal(value: object, field_name: str) -> Decimal:
    """문자열·숫자를 정확한 10진수로 변환한다."""
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name}: 유효한 숫자가 아닙니다.") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name}: 유한한 숫자만 허용합니다.")
    return parsed


# F-02의 핵심: 다른 규격을 섞지 않고 같은 거래 조건의 과거 관측값만 추린다.
def load_price_observations(
    csv_path: Path,
    *,
    variety: str,
    grade: str,
    size: str,
    package_kg: Decimal,
    market_code: str = "110001",
    package: str = "상자",
    as_of: date | None = None,
) -> list[PriceObservation]:
    """시장·품종·등급·크기·포장중량이 같은 경매 거래를 날짜별로 조회한다.

    동일 조건으로 여러 원본 집계 행이 나타날 때는 금액과 물량으로 다시
    가중평균을 계산한다. 다른 등급·크기는 섞지 않는다.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(f"가격 데이터 파일이 없습니다: {csv_path}")
    # 파일 구조가 바뀌어 필수 원자료가 누락되면 조용히 빈 결과를 내지 않고 중단한다.
    required = {
        "date", "market_code", "market", "variety", "grade", "size",
        "package", "package_kg", "trade_type", "total_kg",
        "total_amount_krw", "transaction_count", "quality_flag",
    }
    # 한 날짜에 동일 조건의 집계 행이 여러 개 있을 수 있어 날짜별 총금액·총중량을 누적한다.
    grouped: dict[date, dict] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"가격 CSV 필수 컬럼이 없습니다: {', '.join(sorted(missing))}")
        # 품종·등급·크기·포장 조건이 같지 않으면 비교 가격으로 사용하지 않는다.
        for row in reader:
            if (
                row["market_code"].strip() != market_code
                or row["variety"].strip() != variety
                or row["grade"].strip() != grade
                or row["size"].strip() != size
                or row["package"].strip() != package
                or row["trade_type"].strip() != "경매"
            ):
                continue
            if to_decimal(row["package_kg"], "package_kg") != package_kg:
                continue
            # 기준일(as_of)이 있으면 그 이후 날짜는 조회에서 제외한다.
            day = date.fromisoformat(row["date"])
            if as_of is not None and day > as_of:
                continue
            # 중량과 금액을 따로 합산해 최종 가격을 가중평균한다.
            kg = to_decimal(row["total_kg"], "total_kg")
            amount = to_decimal(row["total_amount_krw"], "total_amount_krw")
            count = int(row["transaction_count"])
            if kg <= 0 or amount <= 0 or count <= 0:
                raise ValueError(f"집계 데이터의 물량·금액·거래건수가 잘못됐습니다: {day}")
            # quality_flag는 유효한 경매라도 품질 경고가 있을 수 있으므로 함께 전파한다.
            entry = grouped.setdefault(day, {
                "market": row["market"].strip(),
                "kg": Decimal(0),
                "amount": Decimal(0),
                "count": 0,
                "flags": set(),
            })
            entry["kg"] += kg
            entry["amount"] += amount
            entry["count"] += count
            flag = row["quality_flag"].strip()
            if flag and flag != "OK":
                entry["flags"].update(flag.split(";"))

    # 날짜순으로 돌려줘 observations[-1]이 가장 최근 관측일이 되게 한다.
    result: list[PriceObservation] = []
    for day, entry in sorted(grouped.items()):
        flags = set(entry["flags"])
        if entry["count"] < 5:
            flags.add("LOW_SAMPLE")
        # 표시용 단가(avg_price_kg)가 아닌 정확한 합계 비율로 가격을 다시 계산한다.
        result.append(PriceObservation(
            date=day,
            market=entry["market"],
            variety=variety,
            grade=grade,
            size=size,
            package_kg=package_kg,
            price_per_kg=entry["amount"] / entry["kg"],
            total_kg=entry["kg"],
            transaction_count=entry["count"],
            quality_flags=tuple(sorted(flags)),
        ))
    return result


# F-03·F-04의 핵심: LLM 없이 같은 입력이면 항상 같은 손익분기 결과를 낸다.
def calculate_break_even(
    *,
    current_price_per_kg: Decimal,
    quantity_kg: Decimal,
    delay_days: int,
    daily_storage_cost: Decimal,
    loss_percent: Decimal,
    future_price_per_kg: Decimal | None = None,
) -> BreakEvenResult:
    """동일한 출하비용을 제외하고 '지금 vs 지연'의 경제적 차이를 계산한다.

    미래 가격은 예측하지 않는다. 입력된 경우에만 가정 시나리오를 계산한다.
    """
    # 물량·가격이 0 이하이면 경제적 비교 자체가 성립하지 않는다.
    if current_price_per_kg <= 0 or quantity_kg <= 0:
        raise ValueError("현재 가격과 출하 물량은 0보다 커야 합니다.")
    if delay_days <= 0:
        raise ValueError("보관기간은 1일 이상이어야 합니다.")
    if daily_storage_cost < 0:
        raise ValueError("하루 보관비는 음수가 될 수 없습니다.")
    # 감량률 100%라면 판매가능 물량이 0이므로 손익분기 단가의 분모가 0이 된다.
    if not Decimal(0) <= loss_percent < Decimal(100):
        raise ValueError("감량률은 0% 이상 100% 미만이어야 합니다.")
    if future_price_per_kg is not None and future_price_per_kg < 0:
        raise ValueError("미래 가정가격은 음수가 될 수 없습니다.")

    # 지금 출하했을 때와 지연 출하했을 때의 금액이 같아지는 단가를 역산한다.
    current_gross = quantity_kg * current_price_per_kg
    # 감량률은 하루마다 반복 적용하지 않고 보관기간 전체 누적 비율로 한 번 반영한다.
    remaining_kg = quantity_kg * (Decimal(1) - loss_percent / Decimal(100))
    # 현재 비교에 넣는 비용은 지연으로 추가 발생하는 보관비다. 공통 비용은 제외한다.
    extra_cost = Decimal(delay_days) * daily_storage_cost
    # 지연 순매출이 현재 매출과 같아지는 최소 경락가격이다.
    break_even = (current_gross + extra_cost) / remaining_kg
    required_rise = (break_even / current_price_per_kg - Decimal(1)) * Decimal(100)

    future_net = None
    difference = None
    # 미래 가격을 추정하지 않는다. 사용자가 가격을 명시한 경우에만 차액을 계산한다.
    if future_price_per_kg is not None:
        future_net = remaining_kg * future_price_per_kg - extra_cost
        difference = future_net - current_gross

    return BreakEvenResult(
        current_gross=current_gross,
        remaining_kg=remaining_kg,
        extra_storage_cost=extra_cost,
        break_even_price_per_kg=break_even,
        required_rise_percent=required_rise,
        future_net_if_given=future_net,
        difference_if_given=difference,
    )


# F-07 표시만 반올림한다. 내부 계산에 이 문자열을 다시 대입하면 정확도가 떨어진다.
def fixed(value: Decimal, digits: int = 2) -> str:
    """표시용 반올림. 내부 계산에서는 반올림하지 않는다."""
    rounding_unit = Decimal("1").scaleb(-digits)
    return f"{value.quantize(rounding_unit, rounding=ROUND_HALF_UP):,.{digits}f}"


# F-06: 과거 한 구간의 가격 변화만 표현하며 전체 시뮬레이션이나 예측은 하지 않는다.
def describe_history(observations: list[PriceObservation], *, days: int) -> str:
    """동일한 거래 조건의 과거 날짜가 있으면 실제 관측가격만 비교한다."""
    latest = observations[-1]
    # 주말·휴장일에 기록이 없으면 가장 가까운 거래일로 대체하지 않는다.
    previous_day = latest.date - timedelta(days=days)
    previous = next((item for item in observations if item.date == previous_day), None)
    if previous is None:
        return f"{days}일 전({previous_day}) 동일 조건 거래 기록 없음 — 가격 변화 비교 생략"
    percent = (latest.price_per_kg / previous.price_per_kg - 1) * 100
    return (
        f"관측된 이전 {days}일 가격 변화: {previous_day} {fixed(previous.price_per_kg)}원/kg → "
        f"{latest.date} {fixed(latest.price_per_kg)}원/kg ({fixed(percent)}%)\n"
        "  ※ 과거 관측값이지, 향후 가격 변화에 대한 예측이 아닙니다."
    )


# 기본 Python 엔진은 OpenAI API 키가 없어도 CMD에서 독립적으로 실행할 수 있다.
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="제주 감귤 출하 경제성 분석 베이스라인")
    parser.add_argument("--prices-file", type=Path, default=DEFAULT_PRICES_FILE,
                        help="가격 CSV 경로 (기본: data/processed/daily_prices.csv)")
    parser.add_argument("--market-code", default="110001", help="시장코드 (기본: 서울가락 110001)")
    parser.add_argument("--variety", required=True, help="표준 품종명 (예: 조생귤)")
    parser.add_argument("--grade", required=True, help="등급 (예: 특)")
    parser.add_argument("--size", required=True, help="크기 (예: 소, 미상)")
    parser.add_argument("--package", default="상자", help="포장형태 (기본: 상자)")
    parser.add_argument("--package-kg", required=True, help="포장 단위중량 kg (예: 5)")
    parser.add_argument("--quantity-kg", required=True, help="출하 물량 kg (예: 3000)")
    parser.add_argument("--days", required=True, type=int, help="출하 지연일수 (예: 7)")
    parser.add_argument("--storage-cost-per-day", required=True, help="하루 추가 보관비 원 (예: 20000)")
    parser.add_argument("--loss-pct", required=True, help="지연기간 전체 감량률 % (예: 2)")
    parser.add_argument("--as-of", help="가격 조회 기준일 YYYY-MM-DD (기본: 데이터 내 최신 날짜)")
    parser.add_argument("--current-price-kg", help="사용자가 알고 있는 현재 원/kg 가격으로 덮어쓰기")
    parser.add_argument("--future-price-kg", help="미래 가격의 가정 시나리오 원/kg (예측값 아님)")
    return parser.parse_args(argv)


# 사용자 인수를 실제 자료 조회와 계산 함수에 연결하는 CLI 진입점이다.
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        package_kg = to_decimal(args.package_kg, "포장중량")
        if package_kg <= 0:
            raise ValueError("포장중량은 0보다 커야 합니다.")
        # 조건에 맞는 데이터가 없을 때는 사용자 현재 단가가 있어야 계산을 진행할 수 있다.
        observations = load_price_observations(
            args.prices_file,
            variety=args.variety,
            grade=args.grade,
            size=args.size,
            package_kg=package_kg,
            market_code=args.market_code,
            package=args.package,
            as_of=as_of,
        )
        if not observations and args.current_price_kg is None:
            raise ValueError(
                "조건에 맞는 경매가격이 없습니다. 품종·등급·크기·포장중량을 확인하거나 "
                "--current-price-kg로 가격을 직접 입력하세요."
            )
        latest = observations[-1] if observations else None
        # 사용자 지정 단가를 우선하되 출처를 결과에 명확하게 표시한다.
        current_price = (
            to_decimal(args.current_price_kg, "현재 가격")
            if args.current_price_kg is not None
            else latest.price_per_kg
        )
        future_price = (
            to_decimal(args.future_price_kg, "미래 가정가격")
            if args.future_price_kg is not None
            else None
        )
        # 표시형식을 제외한 계산을 모두 같은 함수에 위임해 Agent 결과와 일치시킨다.
        result = calculate_break_even(
            current_price_per_kg=current_price,
            quantity_kg=to_decimal(args.quantity_kg, "물량"),
            delay_days=args.days,
            daily_storage_cost=to_decimal(args.storage_cost_per_day, "하루 보관비"),
            loss_percent=to_decimal(args.loss_pct, "감량률"),
            future_price_per_kg=future_price,
        )
    except (OSError, ValueError, InvalidOperation) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1

    print("\n[감귤 출하 시점 분석 — 베이스라인]")
    print(f"시장: {latest.market if latest else args.market_code} | 품종: {args.variety} | "
          f"등급: {args.grade} | 크기: {args.size} | 포장: {args.package} {fixed(package_kg, 0)}kg")
    # 즉시 출하 매출은 관측가격을 가정한 금액이지 실제 정산 보장이 아니다.
    if args.current_price_kg is None:
        print(f"현재 비교 기준가격: {fixed(current_price)}원/kg "
              f"(최근 관측일 {latest.date}, 거래 {latest.transaction_count:,}건)")
    else:
        print(f"현재 비교 기준가격: {fixed(current_price)}원/kg (사용자 입력값)")
        if latest is not None:
            print(f"참고: CSV 최근 관측가격 {latest.date} {fixed(latest.price_per_kg)}원/kg")
    if latest and latest.quality_flags:
        print(f"데이터 주의: {', '.join(latest.quality_flags)}")
    print(f"지금 출하 판매금액: {fixed(result.current_gross, 0)}원")
    print(f"{args.days}일 후 판매 가능 물량(가정): {fixed(result.remaining_kg, 2)}kg")
    print(f"추가 보관비: {fixed(result.extra_storage_cost, 0)}원")
    # 원 단위만 입력하는 상황이라면 손익분기값을 올림해야 손실을 피할 수 있다.
    min_whole_krw = result.break_even_price_per_kg.quantize(Decimal("1"), rounding=ROUND_CEILING)
    print(f"지연 출하 이론상 손익분기 가격: {fixed(result.break_even_price_per_kg)}원/kg")
    print(f"원 단위 가격 기준 최소 필요 단가: {fixed(min_whole_krw, 0)}원/kg")
    print(f"필요한 가격 상승률: {fixed(result.required_rise_percent)}%")
    if future_price is not None:
        print(f"미래 가정가격: {fixed(future_price)}원/kg")
        print(f"지연 출하 시 가정 판매금액(보관비 차감): {fixed(result.future_net_if_given, 0)}원")
        print(f"지금 대비 차액: {fixed(result.difference_if_given, 0)}원")
    else:
        print("미래 가격을 예측하지 않았으므로 지금/지연 출하 중 어느 쪽이 유리한지 단정하지 않습니다.")
    if latest is not None:
        print(describe_history(observations, days=args.days))
    print("※ 경락가격은 농가 실수취가격이 아니며, 두 방식에서 달라지는 추가 비용만 반영했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
