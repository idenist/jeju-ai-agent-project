"""Conversation presentation and a narrow monetary provenance check.

This is not an intent classifier: ordinary dates, kg examples and explanations
are unrestricted. State writes and computations still require application tools.
This guard detects novel written amounts, not every possible false claim.
"""
import json
import re
from decimal import Decimal

_NUMBER = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?")
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(천|만|억)?\s*원")
_SCALE = {None: 1, "천": 1000, "만": 10000, "억": 100000000}
_TERM_TOPICS = {
    "감모율": "loss_rate", "감모": "loss_rate", "수수료 차감 후": "price_basis",
    "차감 후 단가": "price_basis", "거래규격": "data_scope",
}
_TERM_QUESTION = re.compile(r"뜻|무슨\s*말|무엇|뭐예요|의미|설명")


def simple_term_topic(text):
    if (len(text) > 80 or not _TERM_QUESTION.search(text)
            or re.search(r"\d|계산|적용|변경|바꿔|조회", text)):
        return None
    return next((topic for term, topic in _TERM_TOPICS.items() if term in text), None)


def has_unsupported_amount(text, context):
    """Allow amounts already present in supplied state, user input or tool evidence."""
    raw = json.dumps(context, ensure_ascii=False, default=lambda item:
                     item.model_dump(mode="json") if hasattr(item, "model_dump") else vars(item))
    known = {Decimal(m.group().replace(",", "")) for m in _NUMBER.finditer(raw)}
    known.update(Decimal(m[1].replace(",", "")) * _SCALE[m[2]] for m in _AMOUNT.finditer(raw))
    return any(Decimal(m[1].replace(",", "")) * _SCALE[m[2]] not in known for m in _AMOUNT.finditer(text))


def remember_visible_reply(messages, question, reply):
    """Record direct Python replies too, without sending local settlement amounts."""
    if reply.get("status") != "OK":
        return
    if not reply.get("metrics", {}).get("model_requests"):
        messages.append({"role": "user", "content": question})
    entries = reply.get("visible_results", [])
    if any(e.get("tool") == "reconcile_settlement" for e in entries):
        text = "검산 결과를 로컬 화면에 표시했습니다. 상세 금액은 모델에 전달하지 않습니다."
    else:
        text = reply["text"]
    last = messages[-1] if messages else None
    if isinstance(last, dict) and last.get("role") == "assistant" and last.get("content") == text:
        return
    if getattr(last, "type", None) == "message":
        existing = "".join(getattr(c, "text", "") for c in last.content)
        if existing == text:
            return
    messages.append({"role": "assistant", "content": text})
