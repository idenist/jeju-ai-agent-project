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
            "share_pct": str((Decimal(reached) * 100 / total).quantize(Decimal("1"), ROUND_HALF_UP)),
            "needed_change_pct": str(needed.quantize(Decimal("0.01"), ROUND_HALF_UP)),
            "basis": "과거 빈도이며 미래가격 예측 확률이 아닙니다."}


def reach_sentence(reach, later="B안"):
    if not reach:
        return None
    change = Decimal(reach["needed_change_pct"])
    # 필요한 변동이 음수면 가격이 조금 내려도 손익분기를 넘는다는 뜻이라 문장을 나눠 쓴다.
    if change > 0:
        need = f"A안보다 {change:,.2f}% 이상 올라야 합니다"
    elif change < 0:
        need = f"A안보다 {-change:,.2f}% 내려도 넘습니다"
    else:
        need = "A안과 같기만 해도 됩니다"
    head = (f"과거 같은 규격 {reach['interval_days']}일 가격쌍 {reach['total']}쌍 중 "
            f"**{reach['reached']}쌍({reach['share_pct']}%)**이 이 손익분기 가격 이상이었습니다")
    return f"{head}. {later} 가격은 {need}. {reach['basis']}"
