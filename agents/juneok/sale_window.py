"""Typed, isolated dialogue state for constraints on future shipment dates.

This workflow cannot convert a future quote into a current market price or
forecast an optimal date. It only retains explicit user facts and asks for
comparison inputs; future A/B arithmetic remains in the verified engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass
class SaleWindowState:
    earliest_sale_date: str | None = None
    latest_sale_date: str | None = None
    candidate_a_date: str | None = None
    candidate_b_date: str | None = None
    quantity_kg: Decimal | None = None
    quoted_price_per_kg: Decimal | None = None
    price_basis: str | None = None
    # The future price is not an independently verified offer until confirmed.
    quote_confirmed_for_date: str | None = None

    def apply(self, facts: dict, *, today: date) -> None:
        """Validate an entire turn before committing any part of it."""
        values = self.__dict__.copy()
        for field in ("earliest_sale_date", "latest_sale_date", "candidate_a_date", "candidate_b_date"):
            raw = facts.get(field)
            if raw is not None:
                try:
                    parsed = date.fromisoformat(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field}: YYYY-MM-DD 날짜가 필요합니다.") from exc
                if parsed < today:
                    raise ValueError(f"{field}: 출하일이 이미 지났습니다.")
                values[field] = parsed.isoformat()
        for field in ("quantity_kg", "quoted_price_per_kg"):
            raw = facts.get(field)
            if raw is None:
                continue
            try:
                number = Decimal(str(raw))
            except (TypeError, ValueError, InvalidOperation) as exc:
                raise ValueError(f"{field}: 숫자를 확인할 수 없습니다.") from exc
            if not number.is_finite() or number <= 0:
                raise ValueError(f"{field}: 양수여야 합니다.")
            values[field] = number
        if facts.get("price_basis") is not None:
            if facts["price_basis"] not in ("gross", "commission_net"):
                raise ValueError("수수료 기준은 확인된 값만 저장합니다.")
            values["price_basis"] = facts["price_basis"]
        if values["earliest_sale_date"] and values["latest_sale_date"] and values["earliest_sale_date"] > values["latest_sale_date"]:
            raise ValueError("출하 가능 시작일이 종료일보다 늦습니다.")
        for candidate in ("candidate_a_date", "candidate_b_date"):
            val = values[candidate]
            if val and values["earliest_sale_date"] and val < values["earliest_sale_date"]:
                raise ValueError("비교 날짜가 출하 가능한 가장 빠른 날보다 앞섭니다.")
            if val and values["latest_sale_date"] and val > values["latest_sale_date"]:
                raise ValueError("비교 날짜가 출하 가능 기간을 벗어났습니다.")
        if (values["candidate_a_date"] and values["candidate_b_date"]
                and values["candidate_a_date"] >= values["candidate_b_date"]):
            raise ValueError("비교할 두 출하일을 시간순으로 알려주세요.")
        self.__dict__.update(values)

    def reply(self, *, clarify=False) -> str:
        info = []
        if self.earliest_sale_date:
            info.append(f"**{self.earliest_sale_date} 이후**에만 출하할 수 있습니다.")
        if self.latest_sale_date:
            info.append(f"출하 가능 종료일은 **{self.latest_sale_date}**입니다.")
        if self.quantity_kg is not None:
            info.append(f"물량 **{self.quantity_kg:,.0f}kg**을 확인했습니다.")
        if self.quoted_price_per_kg is not None:
            info.append(f"말씀하신 단가 **{self.quoted_price_per_kg:,.0f}원/kg**은 보존했습니다. 적용할 날짜와 가격의 성격은 아직 확인되지 않았습니다.")
        if clarify:
            info.append("현재 출하할 수 없다면 '오늘 판매'와 비교해서는 안 됩니다. 출하 가능한 날짜 중에서 비교해야 합니다.")
        info.append("현재 자료만으로 미래의 **최적 출하일을 확정할 수는 없습니다.**")
        if self.earliest_sale_date is None:
            info.append("**가장 빠르게 출하할 수 있는 날짜**가 언제인가요?")
        elif self.candidate_a_date is None or self.candidate_b_date is None:
            info.append("**비교하려는 두 출하 예정일**을 알려주세요. (출하 가능 기간 안에서 정해 주세요.)")
        elif self.quoted_price_per_kg is None:
            info.append("각 날짜의 판매 단가를 알고 계신가요? 모르면 과거 관측자료만 참고할 수 있으며 미래가격으로 단정하지 않습니다.")
        else:
            info.append("이 가격은 어느 날짜에 적용되는 **실제 제시가격**인가요, 아니면 **가정한 단가**인가요? 다른 출하일의 가격도 확인해야 손익 비교를 시작할 수 있습니다.")
        return "\n\n".join(info)
