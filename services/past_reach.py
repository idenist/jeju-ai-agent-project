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


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def add_past_outcomes(result, change_pcts):
    """과거 쌍마다 B−A 금액차를 계산해, 기준을 넘은 경우와 못 넘은 경우의 크기를 같이 붙인다."""
    reach = result.get("past_reach")
    if not reach or not result.get("options_b"):
        return result
    a, b = result["option_a"], result["options_b"][0]
    try:
        # 비용을 일부 모르면 확인한 비용만 반영한 참고 금액(reference_amount_krw)으로 비교한다.
        a_amount = Decimal(str(a.get("proceeds_krw") or a["reference_amount_krw"]))
        b_cost = Decimal(str(b.get("total_cost_krw") or b.get("known_cost_krw") or 0))
        per_price = Decimal(str(b["remaining_kg"])) * (1 - Decimal(str(b["fee_percent"])) / 100)
        price_a = Decimal(str(a["price_per_kg"]))
        # 표의 B안 가정 단가와 같은 금액이 나오도록 단가는 표와 똑같이 0.01원에서 반올림한다.
        cent = Decimal("0.01")
        changes = [Decimal(str(c)) for c in change_pcts]
        diffs = [per_price * (price_a * (1 + c / 100)).quantize(cent, ROUND_HALF_UP) - b_cost - a_amount
                 for c in changes]
        needed = (Decimal(str(result["break_even_vs_a_krw_per_kg"])) / price_a - 1) * 100
    except (InvalidOperation, KeyError, TypeError, ValueError, ZeroDivisionError):
        return result
    # 넘은/못 넘은 구분은 위 충족 쌍 수와 같은 기준(반올림 전 변동률)으로 나눠 숫자가 서로 어긋나지 않게 한다.
    gains = [d for c, d in zip(changes, diffs) if c >= needed]
    losses = [d for c, d in zip(changes, diffs) if c < needed]
    won = lambda d: str(d.quantize(Decimal("1"), ROUND_HALF_UP))
    reach["outcomes"] = {
        "gain_pairs": len(gains), "loss_pairs": len(losses),
        "gain_median_krw": won(_median(gains)) if gains else None,
        "loss_median_krw": won(_median(losses)) if losses else None,
        "best_krw": won(max(diffs)), "worst_krw": won(min(diffs)),
        "partial_costs": bool(result.get("threshold_only")),
    }
    return result


def judgment_summary(reach):
    """권유가 아니라 과거 사례가 어느 쪽에 많았는지만 사실대로 한 줄로 말한다."""
    reached, total = reach["reached"], reach["total"]
    if reached == 0:
        return "과거 같은 조건에서는 기다려서 이 기준을 넘은 사례가 없었습니다."
    if reached == total:
        return "과거 같은 조건에서는 모든 사례가 이 기준을 넘었습니다."
    if reached * 4 <= total:
        return "과거 같은 조건에서는 기다려서 이 기준을 넘은 사례가 드물었습니다."
    if reached * 2 < total:
        return "과거 같은 조건에서는 기다려서 이 기준을 넘은 사례가 절반보다 적었습니다."
    if reached * 2 == total:
        return "과거 같은 조건에서는 이 기준을 넘은 사례와 못 넘은 사례가 같았습니다."
    if reached * 4 >= total * 3:
        return "과거 같은 조건에서는 대부분의 사례가 이 기준을 넘었습니다."
    return "과거 같은 조건에서는 이 기준을 넘은 사례가 절반보다 많았습니다."


def outcome_lines(reach):
    """넘었을 때 얼마나 남고, 못 넘었을 때 얼마나 잃었는지를 금액으로 보여준다."""
    out = (reach or {}).get("outcomes")
    if not out:
        return []
    fmt = lambda v: f"{int(v):+,}원"
    lines = [judgment_summary(reach)]
    if out["gain_pairs"]:
        lines.append(f"- 넘은 {out['gain_pairs']}쌍: B안이 A안보다 보통 {fmt(out['gain_median_krw'])} "
                     f"(가장 컸던 사례 {fmt(out['best_krw'])})")
    if out["loss_pairs"]:
        lines.append(f"- 못 넘은 {out['loss_pairs']}쌍: B안이 A안보다 보통 {fmt(out['loss_median_krw'])} "
                     f"(가장 컸던 손해 {fmt(out['worst_krw'])})")
    if reach["total"] < 10:
        # 사례가 적으면 중앙값·최댓값이 한두 사례에 좌우되므로 반드시 함께 알린다.
        lines.append(f"- 사례가 {reach['total']}쌍뿐이라 금액이 크게 흔들릴 수 있습니다.")
    if out["partial_costs"]:
        lines.append("- 금액은 확인한 비용만 반영한 참고값입니다.")
    return lines
