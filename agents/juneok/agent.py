"""A bounded Responses tool loop; numerical output always comes from Python."""
from __future__ import annotations

import json
import time
from uuid import uuid4
from .config import Settings
from .state import ScenarioStore
from .tools import TOOLS, execute_tool, explain_term

SYSTEM_PROMPT = """당신은 제주 감귤 출하 대안 비교 Agent입니다. 한국어로 필요한 조건을 간단히 확인합니다.
사용자가 제공한 값만 update_scenario로 저장하고 계산·관측 숫자는 반드시 도구에서 얻습니다.
숫자를 암산하거나 가격·감모·비용·미래 시세를 만들어내지 마세요.
가격은 commission_net(수수료 차감 후) 또는 gross(차감 전)인지 확인합니다.
commission_net은 수수료만 차감된 단가이므로 다른 포함비용은 별도 확인합니다.
included_costs는 사용자가 단가에 포함됐다고 알려준 항목만 저장하세요.
보관비 외 기타비용을 동일하다고 사용자가 확인한 경우에만 cost_policy=equal_excluded.
사용자가 기타비용을 모른다면 확인 질문, 또는 가상 비교에 동일비용 제외 가정 동의 요청을 하세요.
일별 감모율과 보관기간 전체 감모율은 다릅니다. 도구 loss_percent는 전체 기간 비율입니다.
기간 변경 시 기존 감모율을 자동 재사용하지 말고 새 기간의 감모를 다시 확인하세요.
update의 patch에서 변경하지 않을 항목은 null. options를 바꾸면 기존 대안 ID·나머지 조건을 유지한 전체 배열을 전송합니다.
이미 알던 조건을 모름으로 철회하면 update_scenario의 clear_fields에 해당 이름을 넣으세요. patch null은 철회가 아닙니다.
사용자가 준 조건은 계산 전에 update_scenario에 저장하세요. 용어·자료 해석 질문에는 explain_term 도구를 사용하세요.
새 기간 감모를 사용자가 명시 확인한 경우에만 loss_confirmed_for_period=true.
현재 단가가 있으면 손익계산에 시장 등급·크기·포장을 요구하지 않습니다.
저장·출하 변경 가능성은 사용자가 말하지 않으면 null로 두며, 가상 경제성 비교와 실행 가능성을 구분합니다.
사용자 요청에 맞는 자료만 조회합니다. 모든 계산에 #855를 강제 호출하지 않습니다.
출하 품종(노지온주,하우스온주 등)과 시장 품종(조생귤,하우스감귤 등)을 자동 매핑하지 않습니다.
#855는 과거 월별 사실이며 2026 현재 공급량이 아닙니다. 과거 물량으로 미래가격을 예측하지 마세요.
현재 state 버전을 보고 update 후 새 버전으로 compare를 호출합니다.
사용자가 '다음 주 얼마?'라고 하면 예측 불가를 설명하고 조건부 손익분기를 제안하세요.
도구가 NEEDS_INPUT이면 해당 누락만 질문하세요. 모델 최종 숫자 문장 대신 앱이 Python 결과를 표시합니다.
즉시 출하는 별도 기준입니다. options에는 1~365일의 지연 보관 대안만 최대 3개 넣습니다.
이번 사용자 메시지에 있는 조건을 가능한 한 번의 update_scenario에 모두 반영하세요.
조건 반영·조회가 모두 끝나면 도구 인수 finish_turn=true로 종료하세요. update_scenario도 true로 끝내면 Python이 최신 조건을 비교하거나 누락 질문을 표시합니다. 별도 compare와 최종 답변을 반복 호출하지 않습니다.
여러 자료 조회나 조회 후 계산이 필요한 복합 요청은 중간 도구에 finish_turn=false, 마지막 도구에만 true입니다. 입력·데이터 오류는 수정 후 다시 호출하세요.
민감도 요청에는 사용자가 지정한 감모·미래가격 목록으로 compare_sensitivity를 호출하세요. 범위의 끝값만 받았다면 그 끝값만 사용하고 중간값을 만들지 않습니다. 감모 목록이 없으면 loss_values=null로 호출해 확인 질문을 표시합니다. 같은 기간의 원래 감모·미래 단가를 민감도 가정으로 덮어쓰지 않습니다.
"""


class AgentSession:
    def __init__(self, settings: Settings | None = None, client=None):
        self.settings = settings
        self.client = client
        self.store = ScenarioStore()
        self.messages: list = []

    def run(self, question: str) -> dict:
        from .presentation import select_results, render_dialogue, report_for
        settings = self.settings or Settings.load()
        if not isinstance(question, str) or not question.strip() or len(question) > 6000:
            return {"status": "INVALID_INPUT", "text": "질문은 1~6,000자로 입력하세요.", "tool_results": [], "events": [], "state": self.store.snapshot()}
        if self.client is None and not settings.api_key:
            return {"status": "KEY_REQUIRED", "text": "프로젝트 .env에 OPENAI_API_KEY를 입력하세요. 직접 조건 비교와 과거 출하통계는 키 없이 사용할 수 있습니다.",
                    "tool_results": [], "events": [], "state": self.store.snapshot()}
        if self.client is None:
            from openai import OpenAI
            self.client = OpenAI(api_key=settings.api_key, timeout=30, max_retries=1)
        # Every new user turn includes authoritative Python state. Old model text is not state.
        turn_start = len(self.messages)
        self.messages.append({"role": "user", "content": f"현재 확인된 조건: {json.dumps(self.store.snapshot(), ensure_ascii=False)}\n사용자: {question.strip()}"})
        results, events = [], []
        started = time.monotonic()
        status, calls = "OK", 0
        run_id = uuid4().hex
        metrics = {"model_requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "request_seconds": []}
        try:
            for _ in range(6):
                if time.monotonic() - started > 90:
                    raise TimeoutError()
                metrics["model_requests"] += 1
                request_started = time.monotonic()
                response = self.client.responses.create(
                    model=settings.model, instructions=SYSTEM_PROMPT, input=self.messages,
                    tools=TOOLS, tool_choice="auto", parallel_tool_calls=False,
                    store=False, max_output_tokens=4000,
                )
                metrics["request_seconds"].append(round(time.monotonic() - request_started, 3))
                usage = getattr(response, "usage", None)
                if usage:
                    metrics["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                    metrics["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
                    details = getattr(usage, "input_tokens_details", None)
                    metrics["cached_input_tokens"] += getattr(details, "cached_tokens", 0) or 0
                if getattr(response, "model", None):
                    metrics["response_model"] = response.model
                if getattr(response, "status", "completed") == "incomplete":
                    status = "MODEL_INCOMPLETE"
                    break
                self.messages.extend(response.output)
                tool_calls = [item for item in response.output if item.type == "function_call"]
                if not tool_calls:
                    break
                finished = False
                for call in tool_calls:
                    calls += 1
                    if calls > 12:
                        raise TimeoutError()
                    try:
                        arguments = json.loads(call.arguments)
                    except (TypeError, json.JSONDecodeError):
                        arguments = None
                    tool_started = time.monotonic()
                    self.store.origin_turn_id = run_id
                    result = execute_tool(call.name, arguments, self.store)
                    item = {"tool": call.name, "result": result}
                    results.append(item)
                    events.append({"tool": call.name, "status": result["status"], "scenario_version": self.store.version,
                                   "elapsed_seconds": round(time.monotonic() - tool_started, 3)})
                    self.messages.append({"type": "function_call_output", "call_id": call.call_id,
                                          "output": json.dumps(result, ensure_ascii=False)})
                    if isinstance(arguments, dict) and arguments.get("finish_turn") is True and result["status"] in {
                            "OK", "NEEDS_INPUT", "NEEDS_CLARIFICATION", "NO_DATA"}:
                        finished = True
                if finished:
                    break
            else:
                status = "LIMIT_REACHED"
        except Exception as exc:
            # Never expose the SDK exception, request, header, or key.
            status = "API_ERROR"
            events.append({"error_type": type(exc).__name__})
            # Drop a potentially incomplete provider conversation, keeping atomic
            # Python state and verified results for the next retry.
            del self.messages[turn_start:]
        comparisons = [x for x in results if x["tool"] == "compare_shipment_options"]
        updated = any(x["tool"] == "update_scenario" and x["result"]["status"] == "OK" for x in results)
        if (comparisons and comparisons[-1]["result"].get("scenario_version") != self.store.version) or (
                updated and not comparisons and not any(x["tool"] == "compare_sensitivity" for x in results)):
            refreshed = execute_tool("compare_shipment_options", {"expected_version": self.store.version}, self.store)
            results.append({"tool": "compare_shipment_options", "result": refreshed})
            events.append({"tool": "compare_shipment_options", "status": refreshed["status"],
                           "scenario_version": self.store.version, "origin": "application_refresh"})
        visible = select_results(results, self.store.version)
        if not visible:
            # A free-form final response cannot establish market facts or money.
            # Missing-field guidance is generated from application state too.
            from .comparison import compare_shipment_options
            topics = [("price_basis", ("수수료", "실수취", "차감")), ("loss_rate", ("감모",)),
                      ("cost_policy", ("비용",)), ("data_scope", ("자료 범위", "관측", "출하량")),
                      ("forecast_limits", ("예측", "다음 주", "미래가격"))]
            help_intent = any(x in question for x in ("뜻", "뭐", "무엇", "설명", "의미", "예측", "다음 주"))
            topic = next((t for t, words in topics if help_intent and any(w in question for w in words)), None)
            missing = compare_shipment_options(self.store.scenario)
            if topic:
                entry = {"tool": "explain_term", "result": explain_term(topic)}
                results.append(entry)
                visible = [entry]
            else:
                visible = [{"tool": "compare_shipment_options", "result": missing}] if missing["status"] == "NEEDS_INPUT" else []
        text = render_dialogue(visible) if visible else "비교·조회할 조건을 말씀해 주세요. 확인된 조회·계산 결과가 없으면 수치를 제시하지 않습니다."
        report = report_for(visible)
        if status != "OK":
            warning = "\n\n모델 응답을 완료하지 못했습니다. 위에 표시된 도구 결과까지만 확인된 내용입니다. 설정·연결을 확인해 다시 시도해 주세요."
            text += warning
            report += warning
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return {"status": status, "text": text, "report": report, "visible_results": visible,
                "tool_results": results, "events": events, "run_id": run_id,
                "state": self.store.snapshot(), "metrics": metrics}
