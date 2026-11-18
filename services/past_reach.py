"""손익분기 가격을 과거 같은 규격 가격쌍이 몇 번 넘었는지 센다.

예측 확률이 아니라 과거 빈도다. 숫자는 모두 여기서 계산하고, 화면·LLM은 문장으로만 옮긴다.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def past_pairs_reaching(change_pcts, price_a, threshold, interval_days):
    """A안 단가에 과거 변동률을 적용한 B안 가정 단가가 손익분기 이상인 쌍의 수."""
    try:
        changes = [Decimal(str(c)) for c in change_pcts or ()]
        a, line = Decimal(str(price_a)), Decimal(str(threshold))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not changes or a <= 0 or line < 0:
        return None
    # 단가 비교는 A×(1+변동률) ≥ 손익분기와 같다. 반올림 전 값으로 비교해 경계 사례가 흔들리지 않게 한다.
    needed = (line / a - 1) * 100
    reached = sum(1 for c in changes if c >= needed)
    total = len(changes)
    return {"reached": reached, "total": total, "interval_days": interval_days,
            "needed_change_pct": str(needed.quantize(Decimal("0.01"), ROUND_HALF_UP)),
            "basis": "날짜가 겹치는 과거 자료이므로 미래에 오를 확률로 해석할 수 없습니다."}


def reach_sentence(reach, *, partial_costs=False):
    if not reach:
        return None
    # 확률로 읽히지 않도록 퍼센트 대신 "몇 쌍 / 전체 쌍"으로만 쓰고, 날짜 겹침 안내를 바로 붙인다.
    target = "이 참고 기준(확인한 비용만 반영)" if partial_costs else "이 기준"
    return (f"과거 같은 규격의 {reach['interval_days']}일 가격 변동을 적용하면, "
            f"{reach['total']}쌍 중 **{reach['reached']}쌍**이 {target}을 충족했습니다.\n"
            f"※ {reach['basis']}")
