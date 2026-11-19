"""Handle plainly unsupported requests before they reach a shipment tool."""

import re


_OTHER_CROPS = r"딸기|사과|배|포도|복숭아|키위|감자|당근"
_CITRUS_REFERENCE = re.compile(r"귤|온주|만감|한라봉|천혜향|레드향|황금향|청견|카라향")
_CITRUS_PROXY = re.compile(
    rf"\s*(?:이번에는\s*)?(?P<crop>{_OTHER_CROPS})"
    r"(?=\s*(?:[0-9]|[을를은는]|출하|가격|시세))"
    r"[^\n]{0,140}?(?:감귤|조생귤|노지온주|한라봉|천혜향|레드향)\s*"
    r"(?:시세|가격|자료|기준)\s*(?:으?로)?\s*(?:대신|대체로?)\s*"
    r"(?:계산|비교|적용|사용)(?:해\s*(?:도\s*(?:돼(?:요)?|좋아요)|줘(?:요)?|주세요)?|해주세요)?[.!?。\s]*",
)
_OTHER_CROP_PRICE = re.compile(
    rf"(?<![가-힣])(?P<crop>{_OTHER_CROPS})\s*(?:의\s*)?"
    r"(?:(?:도매|경락|시장)\s*)?(?:시세|가격|단가)",
)
_CREATIVE_REQUEST = re.compile(
    r"(?P<context>[^.!?。;；\n]{0,100}?)(?:소설|동화|시\s*한\s*편|노래\s*가사)\s*(?:을|를)?\s*"
    r"(?:써|지어|만들어)\s*(?:줘(?:요)?|주세요|줄래(?:요)?)?[.!?。\s]*",
)
_SHIPMENT_CONTEXT = re.compile(r"출하|시세|경락|가격|감모|보관비|손익|정산|보고서")
_PAST_AS_PRICE_BASIS = re.compile(
    r"(?:과거|작년|지난\s*(?:해|달|주))\s*(?:의\s*)?(?:경락\s*)?"
    r"(?:가격|시세|변동|자료|기록)(?:\s*(?:변동|자료|기록))?\s*"
    r"(?:만?으로|[을를]\s*(?:근거로|바탕으로|참고해(?:서)?|보고))"
)
_SET_ACTUAL_FUTURE_PRICE = re.compile(
    r"(?:다음\s*(?:주|달)|나중\s*출하|미래|실제)\s*(?:에\s*)?"
    r"(?:실제\s*)?(?:판매\s*|받을\s*|출하\s*)?(?:단가|가격|판매가)"
    r"(?:[을를])?\s*(?:(?:미리|하나로)\s*)?(?:확정|결정|정해)"
)


def scope_reply(question: str) -> str | None:
    if _PAST_AS_PRICE_BASIS.search(question) and _SET_ACTUAL_FUTURE_PRICE.search(question):
        return ("미래 실제 판매 단가는 과거 시세만으로 확정할 수 없어요. "
                "판매처에서 받은 단가가 있으면 그 값으로 손익을 비교할 수 있어요.")
    if proxy := _CITRUS_PROXY.fullmatch(question):
        return (f"{proxy['crop']} 출하에 감귤 시세를 적용하면 실제 조건과 맞지 않아요. "
                "제주 감귤의 과거 시세나 출하 시기 비교는 도와드릴 수 있어요.")
    if not _CITRUS_REFERENCE.search(question) and (other_price := _OTHER_CROP_PRICE.search(question)):
        return f"{other_price['crop']} 가격은 여기서 조회할 수 없어요. 제주 감귤의 시세는 확인해 드릴 수 있어요."
    if creative := _CREATIVE_REQUEST.fullmatch(question):
        if not _SHIPMENT_CONTEXT.search(creative["context"]):
            return "여기서는 제주 감귤의 시세나 출하 시기를 함께 살펴볼 수 있어요."
    return None
