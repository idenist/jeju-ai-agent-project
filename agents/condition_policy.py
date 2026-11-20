"""Turn-local boundaries for tool writes and explicitly unconfirmed costs."""

import re

from agents.future_pair_state import unavailable_cost


MUTATING_TOOLS = frozenset({"update_scenario", "compare_future_pair"})
UNCHANGED_NOTICE = "기존 비교 조건을 그대로 뒀어요. 변경할 값이 확정되면 어떤 항목에 적용할지 알려주세요."

_ACTION = r"바꾸|바꿔|변경|수정|저장|삭제|초기화|덮어|반영|적용|입력|넣"
_FIELD = r"조건|물량|단가|가격|수수료|보관(?:비|료|일|기간)|비용|감모|감량|출하일|[AB]안"
_QUOTE = re.compile(r"“[^”\n]+”|‘[^’\n]+’|\"[^\"\n]+\"|'[^'\n]+'", re.IGNORECASE)
_CLAUSE = re.compile(r"[!?。;；\n]+|\.(?!\d)|,\s+|\s+(?:대신|다만|그리고|그런데)\s+")
_DECLINED = re.compile(
    rf"(?:{_ACTION})\w*\s*(?:지\s*(?:말|마|않)|하지\s*(?:말|마|않)|려다가)"
    r"|(?:변경|수정|입력|저장|적용)\s*(?:을\s*)?취소"
    r"|(?:원래|기존|현재)(?:의)?\s*(?:조건|값|비교)?\s*그대로\s*(?:두|둬|유지)",
    re.IGNORECASE,
)
_QUESTIONED = re.compile(
    rf"(?:{_ACTION})\w*\s*(?:도\s*(?:되|돼|괜찮|될)|면\s*(?:되|돼|괜찮|어떨)|는\s*게\s*맞)",
    re.IGNORECASE,
)
_REPORTED = re.compile(
    rf"(?:{_ACTION})\w*\s*라고.{{0,25}}(?:적혀|적었|쓰여|써\s*있|하던|했는|들었|받았|말했|알려줬)"
    r"|(?:관리자|공급자|제3자|명세서|메모|문서).{0,24}(?:공지|지시|내용).{0,20}(?:복사|인용)\w*\s*[:：]",
    re.IGNORECASE,
)
_READ = re.compile(
    rf"(?:{_FIELD}).{{0,35}}?(?:얼마(?:였|인가|예요)|얼마인지(?!\s*(?:모르|몰라))|몇\s*(?:이었|인가)|"
    r"어떤\s*(?:값|조건)|무슨\s*뜻|말이\s*뭔|뜻인지|방법|어떻게\s*확인|확인만|설명만)"
    r"|(?:지금|현재|기존|저장된)\s*(?:비교\s*)?(?:조건|값|단가|보관비)\s*(?:만\s*)?(?:보여|알려|확인)",
    re.IGNORECASE,
)
_APPROVED = re.compile(
    rf"(?:{_FIELD}).{{0,45}}(?:{_ACTION})\w*\s*(?:\s*해\s*)?(?:줘|주세요|주십시오|부탁|할게|해요)(?:요)?\s*$",
    re.IGNORECASE,
)
_CLAIMED_WRITE = re.compile(
    r"(?:바꿨|변경\s*했|수정\s*했|저장\s*했|삭제\s*했|초기화\s*했|반영\s*했|적용\s*했|입력\s*했|넣었)(?:어요|습니다|다)",
)
_FORCED_ZERO = re.compile(
    r"(?<![\d.])(?:0(?:\.0+)?|영|제로)\s*(?:원|%|퍼센트)?\s*(?:으?로|이라고|가정하고)"
    r"|(?:없다|없는\s*걸)\s*(?:고|로)\s*(?:처리|입력|간주|저장|기록|가정)",
)
_DECLARED_ASSUMPTION = re.compile(r"(?:별도\s*)?(?:가정|예시|참고|시나리오)(?:값)?(?:으?로만|만|으로\s*표시)")


def condition_request(question: str) -> tuple[str, bool]:
    """Return a tool-facing request and whether it may save comparison inputs."""
    quoted_directive = False

    def mask_directive(match):
        nonlocal quoted_directive
        if re.search(_ACTION, match.group()):
            quoted_directive = True
            return "[인용문]"
        return match.group()

    outside_quotes = _QUOTE.sub(mask_directive, question)
    clauses = [clause.strip() for clause in _CLAUSE.split(outside_quotes) if clause.strip()]
    checks = (_DECLINED, _QUESTIONED, _REPORTED, _DECLARED_ASSUMPTION)
    allowed = [clause for clause in clauses if not any(check.search(clause) for check in checks)]
    protected = quoted_directive or len(allowed) != len(clauses)
    approved = any(_APPROVED.search(clause) for clause in allowed)
    if protected:
        # Only the independently approved words reach a write-capable tool.
        return (". ".join(allowed), True) if approved else (question, False)
    return question, approved or not bool(_READ.search(question))


def claims_state_was_changed(text: str) -> bool:
    return bool(_CLAIMED_WRITE.search(text))


def requests_zero_for_unknown_cost(question: str) -> bool:
    """Recover a forced zero while respecting an explicitly labeled scenario."""
    return bool(unavailable_cost(question) and _FORCED_ZERO.search(question)
                and not _DECLARED_ASSUMPTION.search(question))
