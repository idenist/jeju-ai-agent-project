"""Conversation presentation and monetary claim detection for model prose."""
import re

_MONEY = re.compile(
    r"(?:₩|￦|\bKRW)\s*\d|(?<![\d,.])\d[\d,.]*\s*(?:(?:[십백천만억]\s*){0,4}원(?!칙|산지|래|본)|KRW\b)"
    r"|(?<![가-힣])(?:[일이삼사오육칠팔구십백천만억]\s*){0,12}[십백천만억]"
    r"(?:\s*[일이삼사오육칠팔구십백천만억]){0,12}\s*원(?!칙|산지|래|본)",
    re.I,
)
_INLINE_MARKUP = str.maketrans("", "", "*_`\u200b")
_TERM_TOPICS = {
    "감모율": "loss_rate", "감모": "loss_rate", "수수료 차감 후": "price_basis",
    "차감 후 단가": "price_basis", "거래규격": "data_scope", "일할": "storage_billing", "정액": "storage_billing",
}
_TERM_QUESTION = re.compile(r"뜻|무슨\s*말|무엇|뭐예요|의미|설명")
_BILLING_DIFFERENCE = re.compile(
    r"\s*(?:일할\s*(?:과|이랑)\s*정액|정액\s*(?:과|이랑)\s*일할)"
    r"(?:은|는|이|가)?\s*(?:뭐가\s*(?:달라요|다른가요)|어떻게\s*달라요|차이(?:가)?\s*뭐예요)[.!?？。\s]*"
)
_EXPLICIT_TERM_QUESTION = re.compile(
    r"\s*(?:(?:잠깐|참|아)[,，\s]+)?[‘“'\"]?"
    r"(?:(?P<loss_rate>감모율|감모|감량률)|"
    r"(?P<price_basis>수수료(?:\s*기준)?|(?:수수료\s*)?차감\s*(?:전|후|뒤)(?:\s*단가)?)|"
    r"(?P<storage_billing>(?:보관비\s*)?(?:일할(?:\s*계산)?|정액(?:\s*청구)?))|"
    r"(?P<data_scope>거래\s*규격|(?:[가-힣]{2,7}\s+)?과거\s*(?:신고\s*)?출하량(?:\s*조회)?|#\s*855))[’”'\"]?"
    r"(?:\s*(?:이|가|은|는|을|를|의|이란|란))?"
    r"(?:(?:\s*(?:뭐(?:야|죠|예요|에요|지)?|뭔가요|뭔지|무엇(?:인가요|이죠)?|"
    r"무슨\s*(?:뜻|말)(?:이야|이에요|인가요|인지)?|뜻(?:을|이\s*(?:뭐야|뭐예요|뭔가요|뭐죠))?|의미)"
    r"(?:\s*(?:알려|설명\s*해)\s*(?:줘|주세요))?"
    r"|\s*(?:쉽게\s*)?(?:설명해|알려)\s*(?:줘|주세요))[.!?？。\s]*"
    r"|\s*[?？])"
)


def explicit_term_topic(text: str) -> str | None:
    """Match a standalone glossary clause before separating it from an edit."""
    if len(text) > 80:
        return None
    if _BILLING_DIFFERENCE.fullmatch(text):
        return "storage_billing"
    match = _EXPLICIT_TERM_QUESTION.fullmatch(text)
    return match.lastgroup if match else None


def simple_term_topic(text):
    if topic := explicit_term_topic(text):
        return topic
    if (len(text) > 80 or not _TERM_QUESTION.search(text)
            or re.search(r"\d|계산|적용|변경|바꿔|조회", text)):
        return None
    return next((topic for term, topic in _TERM_TOPICS.items() if term in text), None)


def model_mentions_money(text):
    """Leave exact prices and receipts to the application's verified result presenters."""
    return bool(_MONEY.search(text.translate(_INLINE_MARKUP)))


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
