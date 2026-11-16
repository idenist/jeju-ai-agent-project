from decimal import Decimal
from io import BytesIO
import json
import os
import unittest
from unittest.mock import patch

from services.future_pair import compare_future_pair
from core.config import PROJECT_ROOT
from ui.pdf_report import render_pdf_report
from presenters.presentation import select_results
from presenters.reporting import render_report


FILTERS = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}
OPTION_A = {"delay_days": 3, "loss_percent": 1, "daily_storage_cost": 10000, "other_cost_krw": 5000, "fee_percent": 5}
OPTION_B = {"delay_days": 10, "loss_percent": 4, "daily_storage_cost": 10000, "other_cost_krw": 5000, "fee_percent": 7}
COMMON = {"quantity": 3000, "price_basis": "gross", "start_date": "2026-11-12", "option_a": OPTION_A, "price_a": 2500}


class FuturePairTests(unittest.TestCase):
    def test_box_quote_waits_for_the_missing_weight_and_confirmation(self):
        from agents.agent import AgentSession
        from agents.box_price import mentions_box_price
        from test_integration_ui_journey import 모의_대화_클라이언트

        self.assertFalse(mentions_box_price("상자당 5kg이고 먼저 팔 때 kg당 2,500원이에요."))
        for weight, expected in ((" 상자는 5kg이에요.", "2,500원/kg"), ("", "정확히 몇 kg"),
                                 (" 5kg 상자 또는 10kg 상자일지 몰라요.", "정확히 몇 kg")):
            with self.subTest(weight=weight):
                client = 모의_대화_클라이언트("compare_future_pair", {
                    "action": "new", "expected_version": 0,
                    "patch": {**COMMON, "option_b": OPTION_B, "price_mode": "history", "history_filters": FILTERS},
                    "box_price": {"side": "A", "phrase": "상자당 12,500원"},
                })
                agent = AgentSession(client=client)
                first = agent.run("감귤 3톤을 사흘 뒤와 열흘 뒤에 출하할 때 비교해 주세요. A안은 상자당 12,500원이에요." + weight + " B안 단가는 과거 경락가격을 참고해 주세요.")
                self.assertIn(expected, first["text"])
                self.assertIsNone(agent.future_pair.inputs["price_a"])
                self.assertIsNone(agent.future_pair.current_result())
                corrected = agent.run("상자 한 개에 5kg이에요.")
                self.assertIn("12,500원 ÷ 5kg", corrected["text"])
                confirmed = agent.run("네, 맞아요.")
                self.assertEqual(len(client.requests), 1)
                self.assertEqual(agent.future_pair.inputs["quantity"], 3000)
                self.assertEqual(agent.future_pair.current_result()["option_a"]["price_per_kg"], "2500")
                self.assertIn("2,500원/kg", confirmed["text"])

    def test_box_quotes_without_a_particle_require_confirmation(self):
        from agents.agent import AgentSession
        from test_integration_ui_journey import 모의_대화_클라이언트

        for quote, entered_amount, unit_price in (("5kg 상자 12,500원", 12500, 2500),
                                                 ("한 박스 2만원, 5kg 상자", 20000, 4000)):
            with self.subTest(quote=quote):
                client = 모의_대화_클라이언트("compare_future_pair", {
                    "action": "new", "expected_version": 0,
                    "patch": {**COMMON, "price_a": entered_amount, "option_b": OPTION_B,
                              "price_mode": "history", "history_filters": FILTERS},
                })
                agent = AgentSession(client=client)
                first = agent.run(f"감귤 3톤을 사흘 뒤와 열흘 뒤에 출하할 때를 비교해 주세요. A안은 {quote}예요. B안 가격은 과거 경락가격을 참고해 주세요.")
                self.assertIn(f"{unit_price:,}원/kg", first["text"])
                self.assertIsNone(agent.future_pair.inputs["price_a"])
                self.assertIsNone(agent.future_pair.current_result())

                agent.run("네, 맞아요.")
                self.assertEqual(agent.future_pair.current_result()["option_a"]["price_per_kg"], str(unit_price))

    def test_box_price_confirmation_preserves_the_separate_history_package(self):
        from agents.box_price import proposed_box_price
        from agents.future_pair_state import FuturePairState

        pair = FuturePairState()
        quote = proposed_box_price("A안은 5kg 상자 12,500원이고 과거 경락가격은 10kg 상자 규격으로 봐 주세요.",
                                   {"side": "A", "phrase": "5kg 상자 12,500원"})
        filters = {**FILTERS, "package_kg": "10"}
        pair.apply("new", pair.version, {**COMMON, "option_b": OPTION_B,
                   "price_mode": "history", "history_filters": filters}, box_price=quote)
        confirmed = pair.follow_up_box_price("네, 맞아요.")
        self.assertEqual(Decimal(confirmed["confirmed_box_price"]), 2500)
        self.assertEqual(pair.inputs["history_filters"], filters)

    def test_box_quote_asks_for_sales_weight_when_only_history_weight_is_given(self):
        from agents.box_price import proposed_box_price
        from agents.future_pair_state import FuturePairState

        for message in (
            "A안은 한 박스 2만원이에요. 과거 경락가격은 5kg 상자 규격으로 봐 주세요.",
            "A안은 한 박스 2만원이고 5kg 상자 기준으로 과거 시세를 찾아 주세요.",
            "A안은 한 박스 2만원이에요. 과거 시세 조회는 가락시장, 조생귤 특 소 5kg 상자 기준으로 부탁해요.",
            "A안은 한 박스 2만원이에요. 과거 시세는 가락시장, 조생귤 특 소, 5kg 상자로 봐 주세요.",
            "A안은 한 박스 2만원이에요. 과거 시세는 가락시장,\n조생귤 특 소 5kg 상자로 봐 주세요.",
        ):
            with self.subTest(message=message):
                pair = FuturePairState()
                quote = proposed_box_price(message, {"side": "A", "phrase": "한 박스 2만원"})
                filters = {**FILTERS, "package_kg": "5"}
                initial = pair.apply("new", pair.version, {**COMMON, "price_a": None, "option_b": OPTION_B,
                                     "price_mode": "history", "history_filters": filters}, box_price=quote)
                self.assertIn("정확히 몇 kg", initial["message"])
                self.assertIsNone(pair.follow_up_box_price("과거 시세는 가락시장, 5kg 상자로 봐 주세요."))
                pair.follow_up_box_price("네, 맞아요.")
                self.assertIsNone(pair.inputs["price_a"])
                self.assertIsNone(pair.current_result())

                correction = pair.follow_up_box_price("판매 상자는 4kg이에요.")
                self.assertIn("5,000원/kg", correction["message"])
                self.assertIsNone(pair.inputs["price_a"])
                confirmed = pair.follow_up_box_price("네, 맞아요.")
                self.assertEqual(Decimal(confirmed["confirmed_box_price"]), 5000)
                self.assertEqual(pair.inputs["history_filters"], filters)

    def test_box_quote_accepts_explicit_sales_weight_after_history_filters(self):
        from agents.box_price import proposed_box_price

        examples = (
            ("A안은 한 박스 2만원이에요. 과거 시세는 가락시장, 조생귤 특 소 5kg 상자로 보고, 실제 판매 상자는 4kg이에요.", 5000),
            ("과거 시세는 조생귤 특 소 10kg 상자로 보고, A안 판매가는 5kg 상자 12,500원이에요.", 2500),
        )
        for question, expected in examples:
            with self.subTest(question=question):
                quote = proposed_box_price(question, None)
                self.assertEqual(quote.unit_price, Decimal(expected))

    def test_ambiguous_sales_weight_still_requires_confirmation_in_history_clause(self):
        from agents.box_price import proposed_box_price

        quote = proposed_box_price(
            "A안은 5kg 상자에 12,500원인데 10kg인지 헷갈리고 B안 과거 시세는 5kg 상자로 봐 주세요.", None,
        )
        self.assertIsNone(quote.weight)
        self.assertIn("실제 판매할 상자", quote.question())

    def test_clear_future_shipment_choice_keeps_the_reported_quantity(self):
        from agents.agent import AgentSession
        from test_integration_ui_journey import 모의_대화_클라이언트

        client = 모의_대화_클라이언트("compare_future_pair", {
            "action": "new", "expected_version": 0,
            "patch": {"option_a": {"delay_days": 3}, "option_b": {"delay_days": 10},
                      "start_date": "2026-11-15", "quantity": None, "price_mode": "manual"},
        })
        agent = AgentSession(client=client)
        reply = agent.run("감귤 3톤인데 사흘 뒤에 팔지 열흘 뒤에 팔지 고민이에요.")
        self.assertEqual(client.requests[0]["tool_choice"], {"type": "function", "name": "compare_future_pair"})
        self.assertEqual(agent.future_pair.inputs["quantity"], 3000)
        self.assertIsNone(agent.future_pair.inputs["history_filters"])
        self.assertEqual(reply["visible_results"][0]["tool"], "compare_future_pair")
        self.assertNotIn("비교 물량(kg)", reply["visible_results"][0]["result"]["missing_fields"])

    def test_future_pair_routing_distinguishes_sales_from_other_requests(self):
        from datetime import date
        from agents.future_pair_intent import explicit_shipment_quantity, is_future_pair_request

        today = date(2026, 11, 15)
        for question in ("3,000kg을 3일 후랑 10일 후 중 언제 출하할까요?",
                         "11월 18일에 팔까, 11월 25일에 팔까?", "3일 뒤와 11월 25일 출하를 비교해줘"):
            self.assertTrue(is_future_pair_request(question, today), question)
        for question in ("오늘 팔지 10일 뒤 팔지 고민이에요", "출하량 지난 3일과 10일 비교해줘",
                         "일주일 뒤(7일 후) 출하를 비교해줘", "사흘 뒤(3일 후)에 팔면 얼마나 나을까요?",
                         "11월 15일부터 보관해서 11월 25일에 출하할 때 가격 비교해줘",
                         "3일 후와 10일 후 판매 수수료의 뜻을 설명해줘",
                         "노지온주 11월 18일과 11월 25일 출하의 과거 공급 집중도를 비교해 주세요.",
                         "#855 자료로 노지온주 11월 18일과 11월 25일 출하 시기를 비교해 주세요.",
                         "3일 후, 10일 후, 17일 후 각각 출하하면 언제 유리한지 비교해 주세요."):
            self.assertFalse(is_future_pair_request(question, today), question)
        self.assertEqual(explicit_shipment_quantity("감귤 3톤, 상자에 5kg씩 포장"), 3000)
        self.assertEqual(explicit_shipment_quantity("5kg 상자로 포장한 3,000 kg"), 3000)
        self.assertEqual(explicit_shipment_quantity("감귤 3톤, A안 상자당 12,500원, 상자는 5kg이에요."), 3000)
        self.assertIsNone(explicit_shipment_quantity("포장 한 개 5kg"))
        self.assertIsNone(explicit_shipment_quantity("감귤 2~3톤"))
        self.assertIsNone(explicit_shipment_quantity("감귤 2톤인지 3톤인지 몰라요"))

    def test_regulation_help_without_a_model_tool_keeps_the_pending_future_comparison(self):
        from types import SimpleNamespace
        from agents.agent import AgentSession
        from test_integration_ui_journey import 모의_대화_클라이언트

        client = 모의_대화_클라이언트("compare_future_pair", {
            "action": "new", "expected_version": 0,
            "patch": {**COMMON, "option_b": OPTION_B, "price_mode": "history",
                      "history_filters": {"variety": "조생귤", "size": "소", "package": "상자"}},
        })
        agent = AgentSession(client=client)
        first = agent.run("과거 경락가격 근거로 3일 뒤와 10일 뒤를 비교해 주세요.")
        self.assertIn("감귤의 등급은 어떻게 되나요?", first["text"])
        self.assertNotIn("몇 kg 들어가나요?", first["text"])
        before = agent.future_pair.snapshot()
        with patch.object(client, "create", return_value=SimpleNamespace(output=[], status="completed")):
            reply = agent.run("등급이나 상자 무게는 아직 모르겠어요. 어디부터 확인하면 돼요?")
            term = agent.run("잠깐, 감모율이 무슨 뜻인지 설명해 주세요.")
        self.assertEqual(agent.future_pair.snapshot(), before)
        self.assertEqual(reply["visible_results"][0]["tool"], "compare_future_pair")
        self.assertIn("등급은 포장 라벨", reply["text"])
        self.assertIn("상자 라벨", reply["text"])
        self.assertNotIn("비교할 감귤은 몇 kg", reply["text"])
        self.assertIn("출하 비교는 이어갈 수", reply["text"])
        self.assertIn("가정 판매단가를 직접 입력", reply["text"])
        self.assertNotIn("몇 kg 들어가나요?", reply["text"])
        self.assertNotIn("감귤의 등급은 어떻게 되나요?", reply["text"])
        self.assertEqual(term["visible_results"][0]["tool"], "explain_term")
        self.assertIn("100kg", term["text"])
        self.assertTrue(agent.future_pair.specs_deferred)

        client.arguments = {"action": "edit", "expected_version": before["version"],
                            "patch": {"history_filters": {"grade": "특", "package_kg": "5"}}}
        result = agent.run("특 등급에 상자당 5kg이에요.")
        self.assertEqual(result["visible_results"][0]["result"]["status"], "OK")
        self.assertEqual(agent.future_pair.inputs["quantity"], 3000)
        self.assertIsNone(agent.future_pair.pending)

    def test_bare_unknown_grade_preserves_ab_task_without_model_call(self):
        from agents.agent import AgentSession
        from test_integration_ui_journey import 모의_대화_클라이언트

        client = 모의_대화_클라이언트("compare_future_pair", {
            "action": "new", "expected_version": 0,
            "patch": {**COMMON, "option_b": OPTION_B, "price_mode": "history",
                      "history_filters": {"variety": "조생귤", "size": "소", "package": "상자"}},
        })
        agent = AgentSession(client=client)
        first = agent.run("과거 경락가격 근거로 3일 후와 10일 후 출하를 비교해 주세요.")
        self.assertIn("등급", first["text"])
        self.assertEqual(len(client.requests), 1)
        before = agent.future_pair.snapshot()
        reply = agent.run("몰라")
        self.assertEqual(reply["metrics"]["model_requests"], 0)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(agent.future_pair.snapshot(), before)
        self.assertIn("가정 판매단가를 직접 입력", reply["text"])
        self.assertNotIn("같은 날짜의 과거 출하량부터", reply["text"])
        self.assertTrue(agent.future_pair.specs_deferred)

    def test_price_source_requires_explicit_historical_intent(self):
        from agents.future_pair_intent import requests_historical_pair_price
        from agents.future_pair_state import future_pair_feedback
        self.assertFalse(requests_historical_pair_price("감귤 3톤, 사흘 뒤와 열흘 뒤 중 언제 팔까요?"))
        self.assertTrue(requests_historical_pair_price("과거 경락가격 근거로 두 미래 출하일 비교해 주세요"))
        self.assertFalse(requests_historical_pair_price("과거 가격은 보지 말고 두 출하일만 비교해 주세요"))
        self.assertFalse(requests_historical_pair_price("과거 경락가격은 필요 없어요"))
        text = future_pair_feedback({"status": "NEEDS_INPUT", "analysis_type": "future_pair",
            "comparison_inputs_pending": True, "missing_fields": [
                "A안 가정 단가(원/kg)", "B안 가정 단가(원/kg)",
                "A안 기간 전체 감모율(%)", "B안 기간 전체 감모율(%)"],
            "storage_days": {"A안": 3, "B안": 10}})
        self.assertIn("A안의 판매단가", text)
        self.assertNotIn("아직 확인할 조건", text)
        self.assertNotIn("과거 출하량", text)

    def test_history_precedes_missing_costs_and_manual_recovery_preserves_inputs(self):
        from agents.future_pair_state import FuturePairState, future_pair_feedback

        pair = FuturePairState()
        base = {**COMMON, "option_a": {**OPTION_A, "loss_percent": None, "daily_storage_cost": None},
                "option_b": {**OPTION_B, "loss_percent": None, "daily_storage_cost": None},
                "price_mode": "history", "history_filters": FILTERS}
        with patch("agents.future_pair_state.compare_future_pair", side_effect=AssertionError):
            preview = pair.apply("new", pair.version, base)
            self.assertEqual(preview["source_status"], "HISTORY_READY")
            self.assertEqual(preview["history_preview"]["clean_pairs"], 20)
            self.assertTrue(preview["history_preview"]["source"]["comparison_sha256"])
            self.assertEqual(len(preview["missing_fields"]), 4)
            feedback = future_pair_feedback(preview)
            self.assertIn("과거 7일 가격쌍 20개", feedback)
            self.assertEqual(feedback.count("하루 보관비는 얼마인가요?"), 1)
            self.assertIn("두 안의 비용이 같으면 한 번만 알려주세요.", feedback)
            self.assertIsNone(pair.current_result())
            no_price = pair.apply("edit", pair.version, {}, ["price_a"])
            self.assertEqual(no_price["source_status"], "HISTORY_READY")
            self.assertIsNone(no_price["history_preview"]["scenarios"][0]["option_b_price_per_kg"])
            unsupported = pair.apply("edit", pair.version, {"price_a": 2500, "option_b": {"delay_days": 8}})
            self.assertEqual(unsupported["source_status"], "INTERVAL_NOT_AVAILABLE")
            self.assertTrue(unsupported["manual_price_required"])
            manual = pair.apply("edit", pair.version, {"price_mode": "manual", "price_b": 2750})
            self.assertEqual(manual["missing_fields"], preview["missing_fields"])
            self.assertEqual(pair.inputs["quantity"], 3000)
            self.assertEqual(pair.inputs["option_a"]["fee_percent"], 5)
            self.assertIsNone(pair.inputs["option_a"]["loss_percent"])
        finished = pair.apply("edit", pair.version, {
            "option_a": {"loss_percent": 1, "daily_storage_cost": 10000},
            "option_b": {"loss_percent": 4, "daily_storage_cost": 10000},
        })
        self.assertEqual(finished["status"], "OK")
        self.assertEqual(Decimal(finished["options_b"][0]["difference_vs_a_krw"]), 261850)

    def test_unknown_cost_can_continue_with_historical_volume_without_losing_inputs(self):
        from agents.agent import AgentSession
        from presenters.presentation import render_dialogue
        from ui.workspace import SharedWorkspace

        workspace = SharedWorkspace(AgentSession())
        pair = workspace.session.future_pair
        preview = pair.apply("new", pair.version, {
            **COMMON, "start_date": "2026-11-15", "price_mode": "history", "history_filters": FILTERS,
            "option_a": {"delay_days": 3}, "option_b": {"delay_days": 10},
        })
        before = pair.snapshot()
        entry = {"tool": "compare_future_pair", "result": preview}
        question = "감귤이 몇 퍼센트 줄지 모르고 보관료도 아직 몰라요. 지금 알 수 있는 건 뭐예요?"
        text = render_dialogue([entry], question=question)
        self.assertIn("모르는 감모율·비용은 미확인", text)
        self.assertIn("가격쌍 20개", text)
        self.assertNotIn("과거 출하량부터 볼까요", text)
        self.assertNotIn("**아직 확인할 조건**", text)
        cost_prompt = render_dialogue([entry])
        self.assertEqual(cost_prompt.count("하루 보관비"), 1)
        self.assertEqual(cost_prompt.count("판매 수수료"), 1)
        self.assertIn("다른 항목만 먼저·나중을 나눠", cost_prompt)
        timing = workspace.lookup_shipment_timing("노지온주", pair.version)
        self.assertEqual(timing["status"], "OK")
        report = timing["visible_results"][0]["result"]
        self.assertEqual(report["input"]["option_a_date"], "2026-11-18")
        self.assertEqual(report["input"]["option_b_date"], "2026-11-25")
        self.assertEqual(pair.snapshot(), before)
        self.assertIsNone(pair.current_result())
        self.assertEqual(workspace.lookup_shipment_timing("노지온주", pair.version - 1)["status"], "INVALID_INPUT")
        pair.costs_deferred = True
        manual = pair.apply("edit", pair.version, {"price_mode": "manual", "price_b": 2750})
        self.assertEqual(manual["status"], "NEEDS_INPUT")
        self.assertEqual(pair.inputs["price_b"], 2750)
        self.assertIsNone(pair.inputs["option_a"]["daily_storage_cost"])
        self.assertTrue(pair.costs_deferred)
        self.assertNotIn("하루 보관비는 얼마인가요?", render_dialogue(
            [{"tool": "compare_future_pair", "result": manual}], pair_costs_deferred=pair.costs_deferred,
        ))
        workspace.clear_conversation()
        self.assertFalse(pair.costs_deferred)

    def test_three_and_ten_day_comparison_uses_actual_history_and_net_amounts(self):
        result = compare_future_pair(**COMMON, option_b=OPTION_B, history_filters=FILTERS)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["interval_days"], 7)
        self.assertEqual(len(result["options_b"]), 3)
        a_net = Decimal(2970 * 2500) * Decimal(".95") - 35000
        self.assertEqual(Decimal(result["option_a"]["proceeds_krw"]), a_net)
        for row in result["options_b"]:
            self.assertEqual(row["delay_days"], 10)
            self.assertEqual(result["evidence"][row["id"]]["clean_pairs"], 20)
            b_net = Decimal(2880) * Decimal(row["price_per_kg"]) * Decimal(".93") - 105000
            self.assertEqual(Decimal(row["difference_vs_a_krw"]), b_net - a_net)
        threshold = (a_net + 105000) / (Decimal(2880) * Decimal(".93"))
        self.assertEqual(Decimal(result["break_even_vs_a_krw_per_kg"]), threshold)
        self.assertEqual(int(result["minimum_whole_krw_per_kg"]), 2660)
        report = render_report([{"tool": "compare_future_pair", "result": result}])
        self.assertLess(report.index("A안 대비 차액"), report.index("기간 전체 감모"))
        self.assertIn("과거 동일 규격 경락가격쌍", report)
        self.assertNotIn("trade_type", report)
        from presenters.presentation import render_dialogue
        reply = render_dialogue([{"tool": "compare_future_pair", "result": result}])
        for output in (reply, report):
            self.assertIn("중앙값 가정(2,409.37원/kg)에서 A안(11/15)의 비교금액이 B안(11/22)보다 670,493원", output)
            self.assertIn("최소 단가는 2,660원/kg", output)
            self.assertIn("B안(11/22)이 유리한 가격 가정은 최고 4,732.49원/kg", output)
        self.assertIn("| B안(11/22) | 최저 | 1,661.44원/kg | 4,345,001원 | -2,673,749원 |", report)
        self.assertEqual(Decimal(result["options_b"][0]["proceeds_krw"]), Decimal("4345000.896"))
        self.assertNotIn("2배를 넘습니다", reply)

    def test_exact_whole_won_break_even_can_be_a_tie(self):
        option_a = {**OPTION_A, "loss_percent": 0, "daily_storage_cost": 0, "other_cost_krw": 0, "fee_percent": 0}
        result = compare_future_pair(
            **{**COMMON, "option_a": option_a},
            option_b={**option_a, "delay_days": 10}, price_b=2500,
        )
        self.assertEqual(Decimal(result["minimum_whole_krw_per_kg"]), 2500)
        self.assertEqual(Decimal(result["options_b"][0]["difference_vs_a_krw"]), 0)
        report = render_report([{"tool": "compare_future_pair", "result": result}])
        self.assertIn("최소 단가는 2,500원/kg", report)

    def test_b_date_change_preserves_a_and_uses_new_pairs_and_costs(self):
        first = compare_future_pair(**COMMON, option_b=OPTION_B, history_filters=FILTERS)
        updated = compare_future_pair(**COMMON, option_b={**OPTION_B, "delay_days": 17}, history_filters=FILTERS)
        self.assertEqual(updated["status"], "OK")
        self.assertEqual(updated["option_a"], first["option_a"])
        self.assertEqual(updated["interval_days"], 14)
        self.assertEqual(updated["options_b"][0]["delay_days"], 17)
        self.assertEqual(Decimal(updated["options_b"][0]["total_cost_krw"]), 175000)
        self.assertEqual(updated["evidence"][updated["options_b"][0]["id"]]["clean_pairs"], 15)
        self.assertNotEqual(updated["minimum_whole_krw_per_kg"], first["minimum_whole_krw_per_kg"])

    def test_missing_interval_allows_manual_and_exports_same_amounts(self):
        from pypdf import PdfReader

        option_b = {**OPTION_B, "delay_days": 8}
        unavailable = compare_future_pair(**COMMON, option_b=option_b, history_filters=FILTERS)
        self.assertEqual(unavailable["status"], "INTERVAL_NOT_AVAILABLE")
        self.assertTrue(unavailable["manual_price_required"])
        with patch("services.future_pair.verify_cached_price_sources", side_effect=AssertionError):
            manual = compare_future_pair(**COMMON, option_b=option_b, price_b=2750)
        row = manual["options_b"][0]
        self.assertEqual(Decimal(row["proceeds_krw"]), 7280600)
        self.assertEqual(Decimal(row["difference_vs_a_krw"]), 261850)
        self.assertEqual(json.loads(json.dumps(manual)), manual)
        entries = [{"tool": "compare_future_pair", "result": manual}]
        pdf = PdfReader(BytesIO(render_pdf_report(entries)))
        text = "\n".join(page.extract_text() for page in pdf.pages)
        for value in ("7,280,600원", "261,850원"):
            self.assertIn(value, render_report(entries))
            self.assertIn(value, text)

    def test_all_warned_price_pairs_open_manual_entry_without_creating_a_price(self):
        from agents.future_pair_state import FuturePairState

        pair = FuturePairState()
        unavailable = pair.apply("new", pair.version, {
            **COMMON, "option_b": OPTION_B, "price_mode": "history",
            "history_filters": {**FILTERS, "size": "미상"},
        })
        self.assertEqual(unavailable["source_status"], "NO_CLEAN_COMPARISON")
        self.assertTrue(unavailable["manual_price_required"])
        self.assertIn("품질 기준", unavailable["message"])
        self.assertIsNone(pair.inputs["price_b"])
        self.assertIsNone(pair.current_result())
        manual = pair.apply("edit", pair.version, {"price_mode": "manual", "price_b": 2750})
        self.assertEqual(manual["status"], "OK")
        self.assertEqual(manual["option_a"]["sale_date"], "2026-11-15")
        self.assertEqual(Decimal(manual["options_b"][0]["price_per_kg"]), 2750)

    def test_chat_basis_choice_needs_a_missing_basis_and_keeps_unknown_empty(self):
        from agents.agent import AgentSession
        from ui.workspace import SharedWorkspace
        from ui.chat import _chat_basis_target, _reply_to_chat_basis

        workspace = SharedWorkspace(AgentSession())
        pair = workspace.session.future_pair
        result = pair.apply("new", pair.version, {
            **COMMON, "price_basis": None, "price_b": 2750,
            "option_a": {**OPTION_A, "fee_percent": None}, "option_b": {**OPTION_B, "fee_percent": None},
        })
        reply = {"visible_results": [{"tool": "compare_future_pair", "result": result}]}
        target = _chat_basis_target(workspace, reply)
        self.assertEqual(target, ("future_pair", pair.version))
        before = pair.snapshot()
        unknown = _reply_to_chat_basis(workspace, *target, None)
        self.assertEqual(unknown["deferred_basis"], target)
        self.assertEqual(pair.snapshot(), before)
        answered = _reply_to_chat_basis(workspace, *target, "commission_net")
        self.assertEqual(answered["status"], "OK")
        self.assertEqual(pair.inputs["price_b"], 2750)
        self.assertEqual(pair.inputs["price_basis"], "commission_net")
        self.assertIsNone(_chat_basis_target(workspace, answered))
        self.assertEqual(_reply_to_chat_basis(workspace, *target, "gross")["status"], "INVALID_INPUT")

    def test_new_chat_comparison_drops_an_inherited_basis_and_continuation_keeps_it(self):
        from agents.agent import AgentSession
        from agents.future_pair_state import LABELS
        from test_integration_ui_journey import 모의_대화_클라이언트

        client = 모의_대화_클라이언트("compare_future_pair", {})
        agent = AgentSession(client=client)
        pair = agent.future_pair
        conditions = {**COMMON, "price_b": 2750, "option_b": OPTION_B}
        client.arguments = {"action": "new", "expected_version": pair.version, "patch": conditions}
        agent.run("먼저 출하할 가격은 수수료 차감 전이에요. 두 출하안을 비교해 주세요.")
        self.assertEqual(pair.inputs["price_basis"], "gross")

        client.arguments = {"action": "new", "expected_version": pair.version,
                            "patch": {"price_b": 2800, "price_basis": "commission_net"}}
        continued = agent.run("기존 비교에서 B안 단가만 2,800원으로 바꿔 주세요.")
        self.assertEqual(continued["visible_results"][0]["result"]["status"], "OK")
        self.assertEqual(pair.inputs["price_basis"], "gross")
        self.assertEqual(pair.inputs["price_a"], 2500)

        client.arguments = {"action": "edit", "expected_version": pair.version, "patch": conditions}
        fresh = agent.run("이전과 별개로 새 비교입니다. 먼저 2,500원/kg, 나중은 직접 입력 2,750원/kg입니다.")
        self.assertIn(LABELS["price_basis"], fresh["visible_results"][0]["result"]["missing_fields"])
        self.assertIsNone(pair.inputs["price_basis"])
        self.assertIsNone(pair.current_result())
        self.assertEqual(len(client.requests[-1]["input"]), 1)

        # A new case with identical unknown conditions also gets its own confirmation.
        previous_version = pair.version
        client.arguments = {"action": "edit", "expected_version": previous_version, "patch": conditions}
        fresh = agent.run("새 비교입니다. 먼저 2,500원/kg, 나중은 직접 입력 2,750원/kg입니다.")
        self.assertIn(LABELS["price_basis"], fresh["visible_results"][0]["result"]["missing_fields"])
        self.assertGreater(pair.version, previous_version)

        client.arguments = {"action": "edit", "expected_version": pair.version, "patch": conditions}
        stated = agent.run("새 출하 비교입니다. 수수료를 빼기 전 2,500원/kg으로 비교해 주세요.")
        self.assertEqual(stated["visible_results"][0]["result"]["status"], "OK")
        self.assertEqual(pair.inputs["price_basis"], "gross")

    def test_new_current_shipment_comparison_clears_old_basis(self):
        from agents.agent import AgentSession
        from agents.state import ScenarioStore, demo_scenario
        from test_integration_ui_journey import 모의_대화_클라이언트

        client = 모의_대화_클라이언트("update_scenario", {"expected_version": 0, "clear_fields": [],
            "patch": {"quantity_kg": 2500, "current_price_per_kg": 2800, "price_basis": "commission_net"}})
        agent = AgentSession(client=client)
        agent.store = ScenarioStore(demo_scenario())
        response = agent.run("새 출하 비교입니다. 지금 팔면 2,500kg에 kg당 2,800원입니다.")
        result = response["visible_results"][0]["result"]
        self.assertIn("price_basis", result["missing_fields"])
        self.assertEqual(agent.store.scenario["current_price_per_kg"], 2800)
        self.assertIsNone(agent.store.scenario["price_basis"])
        self.assertEqual(agent.store.history[-1]["scenario"]["price_basis"], "commission_net")

    def test_screen_clears_old_result_and_switches_to_manual_price(self):
        import streamlit as st
        from pypdf import PdfReader
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
        ws = app.session_state.workspace
        original_scenario = ws.store.snapshot()
        app.selectbox(key="future_pair_basis").set_value("수수료 차감 전").run()
        app.number_input(key="future_pair_quantity").set_value(3000.0)
        app.number_input(key="future_pair_price_a").set_value(2500.0)
        for side, option in (("A", OPTION_A), ("B", OPTION_B)):
            for suffix, field in [(f"loss_{option['delay_days']}", "loss_percent"), ("storage", "daily_storage_cost"),
                                  ("other", "other_cost_krw"), ("fee", "fee_percent")]:
                app.number_input(key=f"future_pair_{side}_{suffix}").set_value(float(option[field]))
        for key, value in FILTERS.items():
            app.text_input(key=f"future_pair_filter_{key}").set_value(value)
        app.button(key="future_pair_calculate").click().run()
        self.assertFalse(app.exception)
        first_a = ws.session.future_pair.current_result()["option_a"]
        self.assertIn("2,660원/kg", "\n".join(item.value for item in app.markdown))

        app.number_input(key="future_pair_B_days").set_value(8).run()
        self.assertIsNone(app.number_input(key="future_pair_B_loss_8").value)
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))
        app.number_input(key="future_pair_B_loss_8").set_value(4.0)
        app.button(key="future_pair_calculate").click().run()
        warning = next(item.value for item in app.warning if "5일" in item.value)
        self.assertEqual(warning.count("B안 단가를 직접 입력"), 1)
        pair = ws.session.future_pair
        snapshot = pair.snapshot()
        conflict = pair.apply("edit", pair.version, {"price_mode": "history", "price_b": 2750})
        self.assertEqual(conflict["status"], "INVALID_INPUT")
        self.assertEqual(pair.snapshot(), snapshot)

        app.radio(key="future_pair_price_mode").set_value("직접 입력").run()
        app.number_input(key="future_pair_price_b").set_value(2750.0)
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            app.button(key="future_pair_calculate").click().run()
        self.assertFalse(app.exception)
        outputs = {call.args[0]: call.args[1] for call in download.call_args_list}
        result = json.loads(outputs["미래 A/B 결과 JSON 내려받기"])["result"]
        self.assertEqual(result["option_a"], first_a)
        self.assertEqual(Decimal(result["options_b"][0]["difference_vs_a_krw"]), 261850)
        pdf = PdfReader(BytesIO(outputs["두 출하일 비교 PDF 저장"]))
        pdf_text = "\n".join(page.extract_text() for page in pdf.pages)
        screen = "\n".join(item.value for item in app.markdown)
        for value in ("7,018,750원", "7,280,600원", "261,850원", "같거나 높아지는 최소 단가"):
            self.assertIn(value, screen)
            self.assertIn(value, pdf_text)
        self.assertEqual(app.session_state.workspace.store.snapshot(), original_scenario)

        app.radio(key="future_pair_price_mode").set_value("과거 변동으로 가정").run()
        self.assertEqual(app.text_input(key="future_pair_filter_variety").value, "조생귤")
        self.assertEqual(app.text_input(key="future_pair_filter_grade").value, "특")
        self.assertIsNone(pair.inputs["price_b"])
        app.radio(key="future_pair_price_mode").set_value("직접 입력").run()
        self.assertEqual(app.number_input(key="future_pair_price_b").value, 2750)
        self.assertIsNone(pair.inputs["history_filters"])
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))

        app.checkbox(key="future_pair_shared_costs").check().run()
        self.assertFalse(app.exception)
        self.assertEqual(pair.inputs["option_b"]["fee_percent"], 5)
        self.assertIsNone(pair.current_result())
        app.checkbox(key="future_pair_shared_costs").uncheck().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.number_input(key="future_pair_B_fee").value, 7)
        self.assertEqual(pair.inputs["option_b"]["fee_percent"], 7)
        app.checkbox(key="future_pair_shared_costs").check().run()
        next(button for button in app.button if button.label == "새 비교 시작 · 조건도 초기화").click().run()
        self.assertFalse(app.checkbox(key="future_pair_shared_costs").value)
        self.assertIsNone(pair.current_result())
        self.assertIsNone(app.number_input(key="future_pair_price_b").value)
        app.radio(key="future_pair_price_mode").set_value("과거 변동으로 가정").run()
        self.assertEqual(app.text_input(key="future_pair_filter_variety").value, "")

    def test_manual_price_over_twice_a_warns_without_altering_the_comparison(self):
        from presenters.presentation import render_dialogue
        from presenters.reporting import manual_price_warning
        self.assertIsNone(manual_price_warning(2500, 5000))
        self.assertIsNone(manual_price_warning(2500, 2600))
        result = compare_future_pair(**COMMON, option_b=OPTION_B, price_b=26000)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(Decimal(result["options_b"][0]["price_per_kg"]), 26000)
        entries = [{"tool": "compare_future_pair", "result": result}]
        for output in (render_dialogue(entries), render_report(entries)):
            self.assertIn("26,000원/kg", output)
            self.assertIn("2배를 넘습니다", output)

    def test_missing_input_actions_open_only_the_needed_sections_and_preserve_values(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
        app.selectbox(key="future_pair_basis").set_value("수수료 차감 전").run()
        app.number_input(key="future_pair_quantity").set_value(3000.0)
        app.number_input(key="future_pair_price_a").set_value(2500.0)
        app.number_input(key="future_pair_A_loss_3").set_value(1.0)
        app.text_input(key="future_pair_filter_variety").set_value("조생귤")
        app.button(key="future_pair_calculate").click().run()
        state = app.session_state.workspace.session.future_pair
        before = state.snapshot()
        self.assertFalse(any(button.key == "future_pair_open_history" for button in app.button))
        app.button(key="future_pair_open_costs").click().run()
        self.assertTrue(app.session_state.future_pair_costs_open)
        self.assertFalse(app.session_state.future_pair_history_open)
        self.assertEqual(state.snapshot(), before)
        self.assertIsNone(state.current_result())

        for side, option in (("A", OPTION_A), ("B", OPTION_B)):
            for suffix, field in ((f"loss_{option['delay_days']}", "loss_percent"), ("storage", "daily_storage_cost"),
                                  ("other", "other_cost_krw"), ("fee", "fee_percent")):
                app.number_input(key=f"future_pair_{side}_{suffix}").set_value(float(option[field]))
        app.button(key="future_pair_calculate").click().run()
        before = state.snapshot()
        self.assertFalse(any(button.key == "future_pair_open_costs" for button in app.button))
        app.button(key="future_pair_open_history").click().run()
        self.assertTrue(app.session_state.future_pair_history_open)
        self.assertEqual(state.snapshot(), before)
        self.assertEqual(app.text_input(key="future_pair_filter_variety").value, "조생귤")
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))

        for key, value in FILTERS.items():
            app.text_input(key=f"future_pair_filter_{key}").set_value(value)
        app.button(key="future_pair_calculate").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(state.current_result()["status"], "OK")
        self.assertNotIn("future_pair_missing", app.session_state)

    def test_chat_conditions_project_to_card_and_only_current_result_is_exported(self):
        import streamlit as st
        from pypdf import PdfReader
        from streamlit.testing.v1 import AppTest
        from test_integration_ui_journey import 모의_대화_클라이언트

        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
        ws = app.session_state.workspace
        ws.load_demo()
        before_entries = ws.current_entries()
        self.assertTrue(before_entries)
        original = ws.store.snapshot()
        state = ws.session.future_pair
        client = 모의_대화_클라이언트("compare_future_pair", {})
        ws.session.client = client

        def say(action, changes, version=None):
            client.arguments = {"action": action, "patch": changes,
                                "expected_version": state.version if version is None else version}
            return ws.run("두 미래 출하안을 비교합니다.")["visible_results"][0]["result"]

        def rerun():
            with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
                app.run()

        missing = say("new", {**COMMON, "option_b": {**OPTION_B, "delay_days": 8}})
        self.assertEqual(missing["status"], "NEEDS_INPUT")
        self.assertIn("B안 가정 단가(원/kg)", missing["missing_fields"])
        first = say("edit", {"price_b": 2750})
        self.assertEqual(first["status"], "OK")
        self.assertEqual(Decimal(first["options_b"][0]["difference_vs_a_krw"]), 261850)
        revision = state.version
        self.assertEqual(say("edit", {"option_b": {"delay_days": 1}})["status"], "INVALID_INPUT")
        self.assertEqual(state.version, revision)
        self.assertEqual(say("edit", {"price_basis": "commission_net"})["status"], "INVALID_INPUT")
        self.assertEqual(say("edit", {"option_b": {"fee_percent": 99.999}})["status"], "INVALID_INPUT")
        rerun()
        self.assertFalse(app.exception)
        self.assertEqual(app.number_input(key="future_pair_B_days").value, 8)
        self.assertEqual(app.number_input(key="future_pair_price_b").value, 2750)
        changed = say("edit", {"option_b": {"delay_days": 9, "loss_percent": 4}})
        self.assertEqual(changed["status"], "NEEDS_INPUT")
        self.assertIn("B안 기간 전체 감모율(%)", changed["missing_fields"])
        entries = [{"tool": "compare_future_pair", "result": result} for result in (first, changed)]
        self.assertEqual(select_results(entries, ws.store.version), entries[1:])
        self.assertEqual(say("edit", {"price_b": 1}, version=revision)["status"], "INVALID_INPUT")
        rerun()
        self.assertFalse(app.exception)
        self.assertEqual(app.number_input(key="future_pair_B_days").value, 9)
        self.assertIsNone(app.number_input(key="future_pair_B_loss_9").value)
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))

        final = say("edit", {"option_b": {"loss_percent": 4}})
        self.assertEqual(final["option_a"], first["option_a"])
        self.assertEqual(Decimal(final["options_b"][0]["difference_vs_a_krw"]), 251850)
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            rerun()
        self.assertFalse(app.exception)
        outputs = {call.args[0]: call.args[1] for call in download.call_args_list}
        result = json.loads(outputs["미래 A/B 결과 JSON 내려받기"])["result"]
        self.assertEqual(result, state.current_result())
        self.assertIn("251,850원", "\n".join(item.value for item in app.markdown))
        pdf = PdfReader(BytesIO(outputs["두 출하일 비교 PDF 저장"]))
        self.assertIn("251,850원", "\n".join(page.extract_text() for page in pdf.pages))
        self.assertEqual(ws.store.snapshot(), original)
        self.assertEqual(ws.current_entries(), before_entries)

        client.arguments = {"action": "edit", "patch": {"price_b": 2800}, "expected_version": state.version}
        requests_before = len(client.requests)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            self.assertFalse(app.text_input(key="shipment_question").disabled)
            app.text_input(key="shipment_question").set_value("B안 단가를 2,800원으로 바꿔줘")
            app.button(key="shipment_send").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(client.requests), requests_before + 1)
        self.assertEqual(state.current_result()["options_b"][0]["price_per_kg"], "2800")
        self.assertEqual(app.number_input(key="future_pair_price_b").value, 2800)
        self.assertTrue(any("385,770원" in item.value for item in app.markdown))
        self.assertTrue(any(button.proto.id.endswith("-future_pair_pdf_chat") for button in app.get("download_button")))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            app.number_input(key="future_pair_price_b").set_value(2750).run()
        self.assertIsNone(state.current_result())
        self.assertEqual(state.inputs["price_b"], 2750)
        self.assertTrue(any(item.label == "이전 두 출하일 비교 응답" for item in app.expander))
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))

    def test_chat_history_recalculates_exact_gap_and_recovers_with_manual_price(self):
        import streamlit as st
        from pypdf import PdfReader
        from streamlit.testing.v1 import AppTest
        from test_integration_ui_journey import 모의_대화_클라이언트

        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
        ws = app.session_state.workspace
        pair = ws.session.future_pair
        client = 모의_대화_클라이언트("compare_future_pair", {})
        ws.session.client = client

        def compare(action, changes):
            client.arguments = {"action": action, "patch": changes, "expected_version": pair.version}
            reply = ws.run("두 미래 출하안의 손익을 비교합니다.")
            self.assertEqual(reply["metrics"]["model_requests"], 1)
            return reply["visible_results"][0]["result"]

        first = compare("new", {**COMMON, "option_b": OPTION_B, "price_mode": "history",
                                "history_filters": {**FILTERS, "grade": None}})
        self.assertEqual(first["status"], "NEEDS_INPUT")
        self.assertEqual(first["source_status"], "NEEDS_CLARIFICATION")
        self.assertIn("특", first["choices"]["grade"])
        self.assertIsNone(pair.current_result())
        seven = compare("edit", {"history_filters": {"grade": "특"}})
        self.assertEqual(seven["interval_days"], 7)
        self.assertEqual(len(seven["options_b"]), 3)
        self.assertEqual(seven["evidence"][seven["options_b"][0]["id"]]["clean_pairs"], 20)

        fourteen = compare("edit", {"option_b": {"delay_days": 17, "loss_percent": 4, "loss_confirmed_for_period": True}})
        self.assertEqual(fourteen["option_a"], seven["option_a"])
        self.assertEqual(fourteen["interval_days"], 14)
        self.assertEqual(pair.inputs["history_filters"], FILTERS)
        self.assertIsNone(pair.inputs["price_b"])
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            app.run()
        self.assertFalse(app.exception)
        files = {call.args[0]: call.args[1] for call in download.call_args_list}
        saved = json.loads(files["미래 A/B 결과 JSON 내려받기"])["result"]
        self.assertEqual(saved, pair.current_result())
        source = saved["evidence"][saved["options_b"][0]["id"]]
        self.assertEqual(source["clean_pairs"], 15)
        self.assertTrue(source["source"]["comparison_sha256"])
        pdf = PdfReader(BytesIO(files["두 출하일 비교 PDF 저장"]))
        pdf_text = "\n".join(page.extract_text() for page in pdf.pages)
        screen = "\n".join(item.value for item in app.markdown)
        for value in ("7,018,750원", "15쌍", "과거 동일 규격 경락가격쌍", *source["base_date_range"]):
            self.assertIn(value, pdf_text)
            self.assertIn(value, screen)

        with patch("agents.future_pair_state._price_revision", return_value=("changed",)):
            self.assertIsNone(pair.current_result())
        unsupported = compare("edit", {"option_b": {"delay_days": 8, "loss_percent": 4, "loss_confirmed_for_period": True}})
        self.assertEqual(unsupported["status"], "NO_DATA")
        self.assertEqual(unsupported["source_status"], "INTERVAL_NOT_AVAILABLE")
        self.assertEqual(unsupported["message"].count("B안 단가를 직접 입력"), 1)
        app.run()
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))
        with patch("services.future_pair.verify_cached_price_sources", return_value={"status": "DATA_ERROR"}):
            error = compare("edit", {"option_b": {"delay_days": 10, "loss_percent": 4, "loss_confirmed_for_period": True}})
        self.assertEqual(error["status"], "DATA_ERROR")
        self.assertTrue(error["manual_price_required"])
        self.assertIsNone(pair.current_result())
        self.assertEqual(pair.inputs["history_filters"], FILTERS)
        with patch("services.future_pair.verify_cached_price_sources", side_effect=AssertionError):
            manual = compare("edit", {"price_mode": "manual", "price_b": 2750,
                                      "option_b": {"delay_days": 8, "loss_percent": 4, "loss_confirmed_for_period": True}})
        self.assertEqual(manual["option_a"], seven["option_a"])
        self.assertEqual(Decimal(manual["options_b"][0]["difference_vs_a_krw"]), 261850)
        self.assertIsNone(pair.inputs["history_filters"])

    def test_manual_price_accepts_redundant_clear_of_history_filters(self):
        from agents.future_pair_state import FuturePairState

        pair = FuturePairState()
        pair.apply("new", pair.version, {**COMMON, "option_b": OPTION_B, "price_mode": "history"})
        result = pair.apply("edit", pair.version, {"price_mode": "manual", "price_b": 2750},
                            ["history_filters", "history_filters.variety"])
        self.assertEqual(result["status"], "OK")
        self.assertEqual(pair.inputs["option_a"], OPTION_A)
        self.assertIsNone(pair.inputs["history_filters"])

    def test_chat_date_action_only_asks_for_new_b_loss_and_replaces_download(self):
        from datetime import date
        from streamlit.testing.v1 import AppTest
        from test_integration_ui_journey import 모의_대화_클라이언트

        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
            ws = app.session_state.workspace
            pair = ws.session.future_pair
            client = 모의_대화_클라이언트("compare_future_pair", {
                "action": "new", "expected_version": pair.version,
                "patch": {**COMMON, "option_b": OPTION_B, "price_mode": "history", "history_filters": FILTERS},
            })
            ws.session.client = client
            next(button for button in app.button if button.label == "두 출하일 비교 시작").click().run()
            messages = app.session_state.agent_messages
            self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
            self.assertEqual(messages[0]["content"], "두 미래 출하일에 감귤을 팔 때를 비교해 주세요.")
            self.assertEqual(messages[-1]["reply"]["progress_steps"],
                             ["알려주신 출하 조건과 더 필요한 정보를 확인합니다.",
                              "같은 거래규격의 경락가격을 로컬 CSV에서 확인합니다.",
                              "입력하신 감모율과 비용을 반영해 출하 금액을 비교합니다."])
            self.assertGreaterEqual(messages[-1]["reply"]["progress_elapsed"], 0)
            original = pair.current_result()
            self.assertEqual(original["interval_days"], 7)
            next(field for field in app.date_input if field.label == "새 나중 출하일").set_value(date(2026, 11, 29))
            next(button for button in app.button if button.label == "이 날짜로 변경").click().run()

            self.assertFalse(app.exception)
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(pair.inputs["option_a"], OPTION_A)
            self.assertEqual(pair.inputs["option_b"], {**OPTION_B, "delay_days": 17, "loss_percent": None})
            self.assertIsNone(pair.current_result())
            self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))
            reply = app.session_state.agent_messages[-1]["reply"]
            self.assertEqual(reply["progress_steps"], ["알려주신 출하 조건과 더 필요한 정보를 확인합니다.",
                                                       "같은 거래규격의 경락가격을 로컬 CSV에서 확인합니다."])
            pending = reply["visible_results"][0]["result"]
            self.assertEqual(pending["missing_fields"], ["B안 기간 전체 감모율(%)"])
            self.assertEqual(pending["history_preview"]["clean_pairs"], 15)
            self.assertIn("17일 보관", reply["text"])
            self.assertNotIn("두 안의 비용이 같으면", reply["text"])

            client.arguments = {"action": "edit", "expected_version": pair.version,
                                "patch": {"option_b": {"loss_percent": 4}}}
            app.text_input(key="shipment_question").set_value("17일 전체로 4% 줄어요.")
            app.button(key="shipment_send").click().run()
            self.assertFalse(app.exception)
            final = pair.current_result()
            self.assertEqual(final["interval_days"], 14)
            self.assertEqual(final["option_a"], original["option_a"])
            self.assertTrue(any(button.proto.id.endswith("-future_pair_pdf_chat") for button in app.get("download_button")))
