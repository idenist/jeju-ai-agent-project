"""Session conditions and the current result for two future shipments."""
from copy import deepcopy
from datetime import date
from decimal import Decimal

from agents.common.data_contract import PAIR_FILES, PRICE_FILE, REPORT_FILE
from agents.common.future_pair import compare_future_pair, preview_future_pair_history
from .comparison import decimal
from .config import PROJECT_ROOT
from .data_refresh import digest, resolve_market_root
from .task_context import data_revision


OPTION_FIELDS = ("delay_days", "loss_percent", "daily_storage_cost", "other_cost_krw", "fee_percent")
MAX_RATE_PERCENT = 99.99
INPUT_FIELDS = ("quantity", "price_basis", "start_date", "price_a", "price_b", "history_filters")
LABELS = {"quantity": "비교 물량(kg)", "price_basis": "수수료 차감 전·후 기준",
          "start_date": "보관 시작일", "price_a": "A안 가정 단가(원/kg)", "price_b": "B안 가정 단가(원/kg)"}
OPTION_LABELS = {"delay_days": "보관일", "loss_percent": "기간 전체 감모율(%)",
                 "daily_storage_cost": "전체 물량 하루 보관비(원)", "other_cost_krw": "별도 비용(원)",
                 "fee_percent": "수수료율(%)"}
COST_QUESTIONS = {
    OPTION_LABELS["daily_storage_cost"]: "감귤 전체의 하루 보관비는 얼마인가요?",
    OPTION_LABELS["other_cost_krw"]: "그 밖에 드는 비용은 얼마인가요?",
    OPTION_LABELS["fee_percent"]: "판매 수수료는 몇 %인가요?",
}
HISTORY_FIELDS = {"variety": "품종", "grade": "등급", "size": "크기", "package": "포장 종류",
                  "package_kg": "포장 중량(kg)", "market_code": "시장코드", "trade_type": "거래방식"}
HISTORY_STATUSES = {"NEEDS_CLARIFICATION": "NEEDS_INPUT", "INTERVAL_NOT_AVAILABLE": "NO_DATA",
                    "NO_CLEAN_COMPARISON": "NO_DATA", "AMBIGUOUS_CODES": "DATA_ERROR"}
HISTORY_QUESTIONS = {
    "variety": "어떤 품종의 감귤인가요?",
    "grade": "출하할 감귤의 등급은 어떻게 되나요?",
    "size": "출하할 감귤의 크기 표기는 어떻게 되나요?",
    "package": "어떤 포장으로 출하하나요?",
    "package_kg": "포장 한 개에 감귤이 몇 kg 들어가나요?",
    "market_code": "어느 도매시장 자료로 비교할까요?",
    "trade_type": "어떤 거래방식으로 출하하나요?",
}
HISTORY_HELP = {
    "variety": "품종은 출하처에 등록한 품종명이나 포장 라벨에서 확인할 수 있어요.",
    "grade": "등급은 포장 라벨이나 선별·출하 명세에서 확인해 보세요.",
    "size": "크기 표기는 포장 라벨이나 선별·출하 명세에서 확인해 보세요.",
    "package": "출하처에 전달할 때 쓰는 포장 종류를 확인해 보세요.",
    "package_kg": "상자 라벨이나 출하 명세에서 상자 한 개에 담긴 감귤 무게를 확인해 보세요.",
    "market_code": "출하처나 경락 내역에서 도매시장 이름을 확인해 보세요.",
    "trade_type": "출하처에 경매 등 어떤 방식으로 거래하는지 확인해 보세요.",
}


def _price_revision():
    try:
        active_price = resolve_market_root(PROJECT_ROOT) / PRICE_FILE
        paths = dict.fromkeys([active_price, *(PROJECT_ROOT / path for path in (PRICE_FILE, *PAIR_FILES, REPORT_FILE))])
        return (*data_revision(), *(digest(path) for path in paths))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def future_pair_feedback(result):
    message = result.get("message") or "비교 조건을 확인해 주세요."
    if preview := result.get("history_preview"):
        start, end = preview["base_date_range"]
        lines = [f"**같은 규격의 과거 {preview['interval_days']}일 가격쌍 {preview['clean_pairs']}개를 찾았습니다.**",
                 f"관측 기준일 {start} ~ {end}",
                 " · ".join(f"{label} {Decimal(row['change_pct']):+,.2f}%"
                            for label, row in zip(("최저", "중앙", "최고"), preview["scenarios"]))]
        if preview["scenarios"][0]["option_b_price_per_kg"] is not None:
            prices = [f"{Decimal(row['option_b_price_per_kg']):,.2f}".rstrip("0").rstrip(".")
                      for row in preview["scenarios"]]
            lines.append(f"나중 출하 가정 단가(원/kg) · 최저 {prices[0]} · 중앙 {prices[1]} · 최고 {prices[2]}")
        lines.append("과거 가격 변동을 참고한 가정입니다.")
        return "\n\n".join([*lines, _missing_questions(result["missing_fields"], result.get("storage_days", {}))])
    fields = result.get("missing_fields", [])
    if not fields:
        return message
    if result.get("comparison_inputs_pending"):
        return _missing_questions(fields, result.get("storage_days", {}))
    lines = [message, "", "확인할 항목: " + ", ".join(HISTORY_FIELDS.get(field, field) for field in fields)]
    for field, choices in result.get("choices", {}).items():
        if field in HISTORY_FIELDS and choices:
            lines.append(f"- {HISTORY_FIELDS[field]} 후보: " + ", ".join(str(choice) for choice in choices))
    return "\n".join(lines)


def history_clarification(result, question=""):
    fields = [field for field in result.get("missing_fields", ()) if field in HISTORY_QUESTIONS]
    if not fields:
        return None
    first = fields[0]
    mentioned = [field for field in fields if HISTORY_FIELDS[field] in question]
    if "package_kg" in fields and "package_kg" not in mentioned and ("상자 무게" in question or "상자 중량" in question):
        mentioned.append("package_kg")
    asks_for_help = any(word in question for word in ("어디", "어떻게", "확인하면"))
    unknown_field = mentioned and any(word in question for word in ("모르", "몰라"))
    if asks_for_help or unknown_field:
        guidance = " ".join(HISTORY_HELP[field] for field in (mentioned or [first]))
        if next_field := next((field for field in fields if field not in (mentioned or [first])), None):
            return f"{guidance}\n\n{HISTORY_QUESTIONS[next_field]}"
        return f"{guidance}\n\n두 출하일의 가정 단가를 알고 있다면 그 가격으로 비교할 수 있어요."
    choices = [choice for choice in result.get("choices", {}).get(first, [])
               if choice not in {"미상", ".", "-"}][:3]
    examples = f"\n\n자료에 있는 예시 · {', '.join(choices)}" if choices else ""
    return HISTORY_QUESTIONS[first] + examples


def _missing_questions(fields, storage_days):
    labels = {"A안": "먼저 출하", "B안": "나중 출하"}
    common_costs = {label for label in COST_QUESTIONS
                    if f"A안 {label}" in fields and f"B안 {label}" in fields}
    questions = []
    for field in fields:
        side, _, label = field.partition(" ")
        if label in common_costs:
            if side == "A안":
                questions.append(COST_QUESTIONS[label])
        elif label == OPTION_LABELS["loss_percent"] and (days := storage_days.get(side)):
            questions.append(f"{labels[side]} · {days}일 보관하는 동안 판매 가능한 양이 몇 % 줄어드나요?")
        elif label in COST_QUESTIONS and side in labels:
            questions.append(f"{labels[side]} · {COST_QUESTIONS[label]}")
        else:
            questions.append(f"{labels[side]} · {label}" if side in labels else field)
    if len(questions) == 1 and questions[0].endswith("?"):
        text = questions[0]
    else:
        text = "\n".join(["**아직 확인할 조건**", "", *(f"- {question}" for question in questions)])
    if common_costs:
        text += "\n\n두 안의 비용이 같으면 한 번만 알려주세요."
    return text


class FuturePairState:
    def __init__(self):
        self.version = 0
        self.inputs = self._empty()
        self.result = None
        self.source_revision = None
        self.pending = None

    @staticmethod
    def _empty():
        return {**dict.fromkeys(INPUT_FIELDS), "option_a": dict.fromkeys(OPTION_FIELDS),
                "option_b": dict.fromkeys(OPTION_FIELDS)}

    def snapshot(self):
        return {"version": self.version, "inputs": deepcopy(self.inputs)}

    def clear_result(self):
        self.result = None
        self.source_revision = None
        self.pending = None

    def current_result(self):
        if self.source_revision is not None and self.source_revision != _price_revision():
            self.clear_result()
        return deepcopy(self.result)

    def replace(self, inputs):
        if inputs != self.inputs:
            self.inputs = deepcopy(inputs)
            self.version += 1
            self.clear_result()

    def reset(self):
        self.replace(self._empty())
        self.clear_result()

    def _response(self, status, message, **details):
        return {"analysis_type": "future_pair", "status": status, "message": message,
                "future_pair_version": self.version, **details}

    def _storage_days(self):
        return {f"{side}안": self.inputs[f"option_{side.lower()}"]["delay_days"] for side in ("A", "B")}

    def calculate(self, *, preview_history=False):
        self.clear_result()
        historical = self.inputs["history_filters"] is not None
        if missing := self._missing(self.inputs):
            if preview_history and historical:
                return self._preview_history(missing)
            return self._response("NEEDS_INPUT", "비교 조건을 확인해 주세요.", missing_fields=missing,
                                  comparison_inputs_pending=preview_history, storage_days=self._storage_days())
        revision = _price_revision() if historical else None
        result = compare_future_pair(**self.inputs)
        if historical and ((revision is None and result["status"] == "OK")
                           or (revision is not None and revision != _price_revision())):
            return self._response("DATA_ERROR", "조회 중 가격 자료가 바뀌었습니다. B안 단가를 직접 입력할 수 있습니다.",
                                  manual_price_required=True)
        if result["status"] == "OK":
            self.result = deepcopy(result)
            self.source_revision = revision
        return self._agent_result({**result, "analysis_type": "future_pair", "future_pair_version": self.version})

    def _preview_history(self, missing):
        start = self.inputs["start_date"]
        a, b = (self.inputs[side]["delay_days"] for side in ("option_a", "option_b"))
        date_labels = {LABELS["start_date"], "A안 보관일", "B안 보관일"}
        if date_fields := [field for field in missing if field in date_labels]:
            return self._response("NEEDS_INPUT", "과거 가격을 조회할 날짜부터 알려주세요.", missing_fields=date_fields)
        revision = _price_revision()
        result = preview_future_pair_history(start, a, b, self.inputs["price_a"], self.inputs["history_filters"])
        if result.get("scenarios"):
            if revision is None or revision != _price_revision():
                return self._response("DATA_ERROR", "조회 중 가격 자료가 바뀌었습니다. B안 단가를 직접 입력할 수 있습니다.",
                                      manual_price_required=True)
            preview = {key: result[key] for key in ("interval_days", "clean_pairs", "base_date_range",
                                                    "scenarios", "conditions", "source")}
            return self._response("NEEDS_INPUT", "과거 가격 근거를 확인했습니다. 손익 계산 조건을 이어서 확인해 주세요.",
                                  source_status="HISTORY_READY", missing_fields=missing, history_preview=preview,
                                  storage_days=self._storage_days())
        return self._agent_result({**result, "analysis_type": "future_pair", "future_pair_version": self.version})

    @staticmethod
    def _agent_result(result):
        status = result["status"]
        if status == "OK":
            return result
        normalized = HISTORY_STATUSES.get(status, status)
        message = result.get("message")
        if status == "ERROR":
            normalized = "INVALID_INPUT" if result.get("error_kind") == "INVALID_INPUT" else "DATA_ERROR"
            if normalized == "INVALID_INPUT":
                message = result.get("error")
        if normalized == "DATA_ERROR" and not message:
            message = "과거 가격 자료를 검증하지 못했습니다. B안 단가를 직접 입력할 수 있습니다."
        response = {**{key: result[key] for key in ("analysis_type", "future_pair_version", "missing_fields", "manual_price_required") if key in result},
                    "status": normalized, "source_status": status, "message": message}
        if result.get("choices"):
            response["choices"] = {field: choices[:8] for field, choices in result["choices"].items() if field in HISTORY_FIELDS}
        if normalized == "DATA_ERROR":
            response["manual_price_required"] = True
        return response

    @staticmethod
    def _missing(inputs):
        missing = [label for field, label in LABELS.items()
                   if inputs[field] is None and (field != "price_b" or inputs["history_filters"] is None)]
        for key, side in (("option_a", "A"), ("option_b", "B")):
            missing += [f"{side}안 {OPTION_LABELS[field]}" for field in OPTION_FIELDS
                        if inputs[key][field] is None and (field != "fee_percent" or inputs["price_basis"] == "gross")]
        return missing

    def apply(self, action, expected_version, patch, clear_fields=None):
        if type(expected_version) is not int or expected_version != self.version:
            return self._response("INVALID_INPUT", "비교 조건 버전이 바뀌었습니다. 최신 조건을 확인해 주세요.")
        if action not in ("new", "edit") or not isinstance(patch, dict):
            return self._response("INVALID_INPUT", "새 비교 또는 조건 수정을 선택해 주세요.")
        candidate = self._empty() if action == "new" else deepcopy(self.inputs)
        try:
            self._apply_patch(candidate, patch, clear_fields)
            self._validate(candidate)
        except ValueError as exc:
            return self._response("INVALID_INPUT", str(exc))
        self.replace(candidate)
        result = self.calculate(preview_history=True)
        if result["status"] in {"NEEDS_INPUT", "NO_DATA"}:
            self.pending = deepcopy(result)
        return result

    @staticmethod
    def _apply_patch(candidate, patch, clear_fields):
        allowed = set(LABELS) | {"option_a", "option_b", "price_mode", "history_filters"}
        if set(patch) - allowed:
            raise ValueError("허용되지 않은 비교 조건입니다.")
        if clear_fields is None:
            clear_fields = []
        if not isinstance(clear_fields, list) or any(not isinstance(field, str) for field in clear_fields):
            raise ValueError("지울 항목은 이름 목록으로 전달해 주세요.")
        mode = patch.get("price_mode")
        filters = patch.get("history_filters")
        if mode not in (None, "manual", "history"):
            raise ValueError("B안 단가의 입력 방식을 확인해 주세요.")
        if filters is not None:
            if not isinstance(filters, dict) or set(filters) - set(HISTORY_FIELDS):
                raise ValueError("과거 가격 거래규격을 확인해 주세요.")
            if any(value is not None and not isinstance(value, str) for value in filters.values()):
                raise ValueError("과거 가격 거래규격은 글자로 입력해 주세요.")
            filters = {key: value.strip() for key, value in filters.items() if value and value.strip()}
        manual_price = patch.get("price_b") is not None
        if (manual_price and (mode == "history" or filters)) or (mode == "manual" and filters):
            raise ValueError("B안 단가는 직접 입력과 과거 변동 가정 중 하나를 선택해 주세요.")
        history = mode == "history" or (mode is None and not manual_price and candidate["history_filters"] is not None)
        if filters and not history:
            raise ValueError("거래규격으로 비교하려면 과거 변동 가정을 선택해 주세요.")
        candidate["history_filters"] = (candidate["history_filters"] or {}) if history else None
        if history:
            candidate["price_b"] = None
        for field in clear_fields:
            side, _, name = field.partition(".")
            if field in LABELS:
                candidate[field] = None
            elif side in ("option_a", "option_b") and name in OPTION_FIELDS:
                candidate[side][name] = None
                if name == "delay_days":
                    candidate[side]["loss_percent"] = None
            elif side == "history_filters" and name in HISTORY_FIELDS and history:
                candidate["history_filters"].pop(name, None)
            else:
                raise ValueError("지울 비교 항목을 확인해 주세요.")
        for field in LABELS:
            if patch.get(field) is not None:
                candidate[field] = patch[field]
        for side in ("option_a", "option_b"):
            updates = patch.get(side)
            if updates is None:
                continue
            if not isinstance(updates, dict) or set(updates) - {*OPTION_FIELDS, "loss_confirmed_for_period"}:
                raise ValueError("A·B안 보관 조건을 확인해 주세요.")
            option = candidate[side]
            updates = dict(updates)
            confirmed = updates.pop("loss_confirmed_for_period", False)
            if type(confirmed) is not bool:
                raise ValueError("새 기간의 감모율 확인 여부를 알려 주세요.")
            if updates.get("delay_days") is not None and updates["delay_days"] != option["delay_days"] and option["delay_days"] is not None:
                option["loss_percent"] = None
                if not confirmed:
                    updates.pop("loss_percent", None)
            option.update({key: value for key, value in updates.items() if value is not None})
        if filters:
            candidate["history_filters"].update(filters)

    @staticmethod
    def _validate(inputs):
        if inputs["price_basis"] not in (None, "gross", "commission_net"):
            raise ValueError("수수료 차감 전·후 단가 기준을 확인해 주세요.")
        start_date = inputs["start_date"]
        if start_date is not None:
            try:
                start = date.fromisoformat(start_date)
                inputs["start_date"] = start.isoformat()
            except (ValueError, TypeError):
                raise ValueError("보관 시작일을 연-월-일로 알려 주세요.") from None
        for field in ("quantity", "price_a", "price_b"):
            value = inputs[field]
            if value is None:
                continue
            number = decimal(value, LABELS[field])
            if number < 0 or (field != "price_b" and number == 0):
                raise ValueError(f"{LABELS[field]}의 범위를 확인해 주세요.")
        for key, side in (("option_a", "A"), ("option_b", "B")):
            for field, value in inputs[key].items():
                if value is None:
                    continue
                label = f"{side}안 {OPTION_LABELS[field]}"
                if field == "delay_days":
                    if type(value) is not int or not 1 <= value <= 365:
                        raise ValueError(f"{label}은 1~365일 사이 정수여야 합니다.")
                    if start_date is not None and value > (date.max - start).days:
                        raise ValueError("보관 시작일과 보관일로 출하일을 계산할 수 없습니다.")
                    continue
                number = decimal(value, label)
                if number < 0 or (field in {"loss_percent", "fee_percent"} and number > Decimal(str(MAX_RATE_PERCENT))):
                    raise ValueError(f"{label}의 범위를 확인해 주세요.")
                if field == "fee_percent" and inputs["price_basis"] == "commission_net" and number != 0:
                    raise ValueError("수수료 차감 후 단가와 별도 수수료율이 함께 입력됐습니다. 단가 기준을 확인해 주세요.")
        a, b = (inputs[side]["delay_days"] for side in ("option_a", "option_b"))
        if a is not None and b is not None and a >= b:
            raise ValueError("B안 보관일은 A안보다 길어야 합니다.")
