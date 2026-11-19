from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from agents.agent import AgentSession
from agents.shipment_candidates import requested_candidate_dates
from presenters.reporting import render_result
from services.shipment_candidates import compare_shipment_candidates
from test_juneok_harness import FakeClient, call, response


TODAY = date(2026, 11, 19)
DATES = (date(2026, 11, 23), date(2026, 11, 28), date(2026, 11, 30))
COMMON = {"start": date(2026, 11, 20), "quantity": 3000, "basis": "gross", "fee": 5,
          "daily_storage": 10000, "other_cost": 5000}
QUOTES = dict(zip(DATES, ((2500, 1), (2650, 3), (2800, 5))))


class CandidateTests(TestCase):
    def draft_for_quote(self, quotes):
        agent = AgentSession(client=FakeClient([]))
        agent.run("감귤 3톤을 11/20부터 보관해서 11/23, 11/28, 11/30 출하 후보일을 비교해줘.")
        draft = agent.current_shipment_candidates()
        draft.common.update(COMMON)
        draft.by_date.update(quotes)
        draft.result = draft.calculate()
        return agent, draft

    def test_same_costs_use_each_dates_storage_duration_and_order(self):
        result = compare_shipment_candidates(DATES, excluded_dates=[], by_date=QUOTES, **COMMON)
        self.assertEqual(result["status"], "OK")
        self.assertEqual([Decimal(row["storage_cost_krw"]) for row in result["rows"]], [30000, 80000, 100000])
        self.assertEqual([Decimal(row["proceeds_krw"]) for row in result["rows"]], [7018750, 7240925, 7476000])
        self.assertEqual(result["highest_dates"], ["2026-11-30"])
        self.assertFalse(result["is_future_forecast"])

    def test_excluded_date_never_requires_inputs_or_gets_ranked(self):
        result = compare_shipment_candidates(DATES, excluded_dates=[DATES[-1]],
                                             by_date={day: QUOTES[day] for day in DATES[:2]}, **COMMON)
        self.assertEqual(result["excluded_dates"], ["2026-11-30"])
        self.assertEqual([row["sale_date"] for row in result["rows"]], ["2026-11-23", "2026-11-28"])
        self.assertEqual(result["highest_dates"], ["2026-11-28"])
        self.assertIn("222,175원", render_result(result))

    def test_quote_unknown_keeps_comparison_amount_blank_and_rounds_the_required_price_up(self):
        result = compare_shipment_candidates(DATES, excluded_dates=[],
                                             by_date={**QUOTES, DATES[1]: (None, 3)}, **COMMON)
        self.assertEqual(result["reference_date"], "2026-11-23")
        self.assertEqual(Decimal(result["reference_proceeds_krw"]), 7018750)
        self.assertFalse(result["ranking_complete"])
        unknown = result["rows"][1]
        self.assertIsNone(unknown["price_per_kg"])
        self.assertIsNone(unknown["proceeds_krw"])
        self.assertIsNone(unknown["difference_vs_reference_krw"])
        self.assertEqual(unknown["minimum_whole_krw_per_kg"], "2570")
        self.assertEqual(result["highest_dates"], ["2026-11-30"])
        self.assertIn("견적 미확인 · 2,570원/kg 이상 필요", render_result(result))
        for quote, expected in ((2569, False), (2570, True)):
            with self.subTest(quote=quote):
                repriced = compare_shipment_candidates(DATES, excluded_dates=[],
                                                       by_date={**QUOTES, DATES[1]: (quote, 3)}, **COMMON)
                self.assertEqual(Decimal(repriced["rows"][1]["difference_vs_reference_krw"]) >= 0, expected)

    def test_earliest_unknown_uses_first_eligible_quote_and_leaves_old_first_based_delta_blank(self):
        inputs = {**QUOTES, DATES[0]: (None, 1)}
        result = compare_shipment_candidates(DATES, excluded_dates=[], by_date=inputs, **COMMON)
        self.assertEqual(result["reference_date"], "2026-11-28")
        self.assertEqual(result["rows"][0]["minimum_whole_krw_per_kg"], "2579")
        self.assertTrue(all(row["difference_vs_first_krw"] is None for row in result["rows"]))
        self.assertEqual(Decimal(result["rows"][2]["difference_vs_reference_krw"]), 235075)
        excluded = compare_shipment_candidates(DATES, excluded_dates=[DATES[1]], by_date=inputs, **COMMON)
        self.assertEqual(excluded["reference_date"], "2026-11-30")

    def test_all_unknown_quotes_ask_for_one_and_explicit_zero_quote_is_invalid(self):
        result = compare_shipment_candidates(DATES, excluded_dates=[],
                                             by_date={day: (None, loss) for day, (_, loss) in QUOTES.items()}, **COMMON)
        self.assertEqual(result, {"status": "NEEDS_INPUT", "message": "기준으로 삼을 후보일 한 곳의 kg당 단가를 입력해 주세요."})
        invalid = compare_shipment_candidates(DATES, excluded_dates=[],
                                              by_date={**QUOTES, DATES[1]: (0, 3)}, **COMMON)
        self.assertEqual(invalid["status"], "INVALID_INPUT")

    def test_same_day_missing_quote_uses_net_basis_without_reapplying_fees(self):
        result = compare_shipment_candidates(DATES, excluded_dates=[], by_date={**QUOTES, DATES[0]: (None, None)},
                                             **{**COMMON, "start": DATES[0], "basis": "commission_net", "fee": 0})
        self.assertEqual(result["reference_date"], "2026-11-28")
        self.assertEqual(Decimal(result["reference_proceeds_krw"]), 7656500)
        self.assertEqual(result["rows"][0]["minimum_whole_krw_per_kg"], "2554")

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_guided_entry_asks_one_field_and_computes_only_supported_rows(self, _today):
        agent = AgentSession(client=FakeClient([]))
        agent.run("감귤 3톤을 11/20부터 보관해서 11/23, 11/28, 11/30 출하 후보일을 비교해줘, 11/23은 kg당 2,500원.")
        draft = agent.current_shipment_candidates()
        self.assertEqual(draft.by_date[DATES[0]][0], 2500)
        draft.save_initial_inputs(3000, {DATES[0]: 2500, DATES[1]: None, DATES[2]: 2800})
        for name, day, answer in (("basis", None, "차감 전"), ("fee", None, "5%"),
                                  ("daily_storage", None, "하루 보관비 10,000원"), ("other_cost", None, "5000원"),
                                  ("loss", DATES[0], "1%"), ("loss", DATES[1], "3%"),
                                  ("loss", DATES[2], "모르겠어요")):
            with self.subTest(name=name, day=day):
                step = draft.next_condition()
                self.assertEqual((step.field, step.day), (name, day))
                self.assertIsNone(draft.result)
                reply = agent.run(answer)
                self.assertEqual(reply["metrics"]["dialogue_route"], "shipment_candidate_condition")
                self.assertLessEqual(reply["text"].count("?"), 1)
        self.assertIsNone(draft.next_condition())
        first, unquoted, skipped = draft.result["rows"]
        self.assertEqual(Decimal(first["proceeds_krw"]), 7018750)
        self.assertEqual(unquoted["minimum_whole_krw_per_kg"], "2570")
        self.assertEqual(Decimal(skipped["price_per_kg"]), 2800)
        self.assertIsNone(skipped["proceeds_krw"])
        self.assertIsNone(skipped["loss_percent"])
        self.assertIsNone(skipped["minimum_whole_krw_per_kg"])
        self.assertFalse(draft.result["ranking_complete"])
        report = render_result(draft.result)
        self.assertIn("2,570원/kg 이상 필요", report)
        self.assertIn("미확인", report)
        self.assertIn("2026-11-23 견적을 기준으로", report)
        self.assertNotIn("가장 높습니다", report)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_guided_skipped_common_cost_stays_unknown_and_known_zero_is_distinct(self, _today):
        agent = AgentSession(client=FakeClient([]))
        agent.run("감귤 3톤을 11/20부터 보관해서 11/23, 11/28, 11/30 출하 후보일을 비교해줘.")
        draft = agent.current_shipment_candidates()
        saved = draft.save_initial_inputs(3000, {DATES[0]: None})
        self.assertEqual(saved["status"], "NEEDS_INPUT")
        self.assertIsNone(draft.next_condition())
        self.assertIn("단가", draft.condition_feedback())
        draft.save_initial_inputs(3000, {DATES[0]: 2500})
        agent.run("수수료 차감 후")
        unknown = agent.run("보관비는 몰라요")
        self.assertIsNone(draft.common["daily_storage"])
        self.assertIsNone(draft.common.get("other_cost"))
        self.assertIsNone(draft.common.get("fee"))
        self.assertIsNone(draft.next_condition())
        self.assertIsNone(draft.result)
        self.assertEqual(unknown["text"], "저장고 견적서에서 전체 물량의 하루 보관비를 확인해 주세요.")
        self.assertEqual(draft.storage_pause, "waiting")
        before = deepcopy(draft.by_date), draft.common.copy(), draft.eligible
        agent.run("나중에 계속")
        self.assertEqual(draft.storage_pause, "later")
        self.assertEqual(before, (draft.by_date, draft.common, draft.eligible))
        self.assertIsNone(draft.answer_condition("11월 28일은 kg당 2,650원이에요"))
        self.assertIn("따로 드는 비용", agent.run("하루 보관비는 0원이에요")["text"])
        self.assertIsNone(draft.storage_pause)
        self.assertEqual(draft.common["daily_storage"], 0)
        for answer in ("없어요", "1%", "3%", "5%"):
            agent.run(answer)
        self.assertEqual(draft.common["other_cost"], 0)
        self.assertEqual(draft.result["status"], "OK")

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_returning_with_only_storage_cost_reuses_confirmed_comparison_conditions(self, _today):
        agent, draft = self.draft_for_quote({**QUOTES, DATES[1]: (None, 3), DATES[2]: (None, 5)})
        before = deepcopy(draft.result)
        draft.save_form_inputs({**COMMON, "daily_storage": None}, draft.by_date)
        draft.save_initial_inputs(3000, {DATES[0]: 2500})
        agent.run("모르겠어요")
        self.assertIsNone(draft.result)
        draft.postpone_storage()
        agent.run("10,000원")
        self.assertIsNone(draft.storage_pause)
        self.assertEqual(draft.result, before)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_storage_cost_resume_checks_units_before_saving(self, _today):
        for daily_cost in ("하루에 1만원이에요", "보관료는 하루에 만 원", "1만원/일", "하루 보관비는 총 1만원"):
            with self.subTest(daily_cost=daily_cost):
                agent, draft = self.draft_for_quote(QUOTES)
                before = deepcopy(draft.result)
                draft.save_form_inputs({**COMMON, "daily_storage": None}, draft.by_date)
                draft.save_initial_inputs(3000, {DATES[0]: 2500})
                agent.run("모르겠어요")
                for non_daily in ("총 10만원", "10일에 총 10만원", "상자당 500원", "월 30만원"):
                    reply = agent.run(non_daily)
                    self.assertIn("하루 보관하는 데 드는 금액", reply["text"])
                    self.assertIsNone(draft.common["daily_storage"])
                    self.assertIsNone(draft.result)
                agent.run(daily_cost)
                self.assertEqual(draft.common["daily_storage"], 10000)
                self.assertEqual(draft.result, before)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_changing_storage_start_rechecks_losses_for_new_period_before_reporting(self, _today):
        agent, draft = self.draft_for_quote(QUOTES)
        draft.initial_inputs_saved = True
        self.assertFalse(draft.save_form_inputs(COMMON | {"start": DATES[0]}, QUOTES))
        self.assertIsNone(draft.result)
        self.assertEqual(draft.common["quantity"], 3000)
        self.assertEqual(draft.by_date, {day: (price, None) for day, (price, _) in QUOTES.items()})
        self.assertEqual(draft.next_condition().day, DATES[1])
        agent.run("2%")
        agent.run("3%")
        self.assertEqual([Decimal(row["loss_percent"]) for row in draft.result["rows"]], [0, 2, 3])

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_initial_conflicting_quotes_leave_only_that_date_unconfirmed(self, _today):
        agent = AgentSession(client=FakeClient([]))
        opening = "감귤 3톤을 11/20부터 보관해 11/23, 11/28, 11/30 출하 후보일을 비교해줘"
        reply = agent.run(opening + "; 11/23은 kg당 2,500원; 11/23은 kg당 2,750원; 11/28은 kg당 2,650원")
        draft = agent.current_shipment_candidates()
        self.assertIn("11월 23일 견적에 서로 다른 금액이 있어 비워뒀어요", reply["text"])
        self.assertIsNone(draft.by_date[DATES[0]][0])
        self.assertEqual(draft.by_date[DATES[1]][0], Decimal(2650))
        self.assertEqual(draft.common["quantity"], 3000)
        draft.save_initial_inputs(3000, {DATES[0]: 2500, DATES[1]: 2650})
        self.assertFalse(draft.conflicting_quotes)
        self.assertEqual(draft.by_date[DATES[0]][0], 2500)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_form_basis_change_requires_new_quotes_and_preserves_other_inputs(self, _today):
        agent, draft = self.draft_for_quote(QUOTES)
        original_pair = agent.future_pair.snapshot()
        agent.run("11/28에 수수료 차감 후 kg당 2,600원")
        self.assertIsNotNone(draft.pending_quote)
        self.assertTrue(draft.save_form_inputs({**COMMON, "basis": "commission_net"}, QUOTES))
        self.assertEqual(draft.dates, DATES)
        self.assertEqual(draft.common, {**COMMON, "basis": "commission_net", "fee": None})
        self.assertEqual(draft.by_date, dict(zip(DATES, ((None, 1), (None, 3), (None, 5)))))
        self.assertIsNone(draft.pending_quote)
        self.assertIsNone(draft.result)
        self.assertEqual(draft.calculate()["status"], "NEEDS_INPUT")

        net = {DATES[0]: (2350, 1), DATES[1]: (2480, 3), DATES[2]: (None, 5)}
        self.assertFalse(draft.save_form_inputs(draft.common, net))
        result = draft.calculate()
        self.assertEqual(result["status"], "OK")
        self.assertIsNone(result["fee_percent"])
        self.assertEqual(Decimal(result["reference_proceeds_krw"]), 6944500)
        self.assertEqual(Decimal(result["rows"][1]["proceeds_krw"]), 7131800)

        self.assertTrue(draft.save_form_inputs(COMMON | {"fee": None}, net))
        self.assertEqual(draft.common, COMMON)
        self.assertTrue(all(price is None for price, _ in draft.by_date.values()))
        self.assertEqual(agent.future_pair.snapshot(), original_pair)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_form_first_basis_keeps_new_inputs_and_rechecks_saved_unclassified_quotes(self, _today):
        agent = AgentSession(client=FakeClient([]))
        agent.run("감귤 3톤을 11/20부터 보관해서 11/23, 11/28, 11/30 출하 후보일을 비교해줘.")
        draft = agent.current_shipment_candidates()
        self.assertFalse(draft.save_form_inputs({**COMMON, "basis": "commission_net"}, QUOTES))
        self.assertEqual(draft.by_date, QUOTES)
        self.assertIsNone(draft.calculate()["fee_percent"])

        agent.run("새 후보일 11/23, 11/28, 11/30 출하 비교해줘")
        draft = agent.current_shipment_candidates()
        agent.run("11/23 kg당 2,500원")
        self.assertTrue(draft.save_form_inputs(COMMON, QUOTES))
        self.assertTrue(all(price is None for price, _ in draft.by_date.values()))

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_committed_chat_quote_updates_only_its_date_and_replaces_current_result(self, _today):
        agent, draft = self.draft_for_quote({**QUOTES, DATES[1]: (None, 3)})
        before, pair = deepcopy(draft.result), agent.future_pair.snapshot()
        reply = agent.run("11/28에 kg당 2,600원 받기로 했어요")
        self.assertEqual(reply["metrics"], {"model_requests": 0, "dialogue_route": "shipment_candidate_quote"})
        self.assertIn("83,950원 더 남습니다", reply["text"])
        self.assertEqual(draft.common, COMMON)
        self.assertEqual(draft.result["rows"][0], before["rows"][0])
        self.assertEqual(draft.result["rows"][2], before["rows"][2])
        self.assertEqual(draft.by_date[DATES[1]], (Decimal(2600), 3))
        self.assertEqual(Decimal(draft.result["rows"][1]["proceeds_krw"]), 7102700)
        self.assertIsNone(draft.result["rows"][1]["minimum_whole_krw_per_kg"])
        self.assertEqual(draft.revision, 1)
        self.assertIn("이미", agent.run("11월 28일 2,600원/kg입니다")["text"])
        self.assertEqual(draft.revision, 1)
        self.assertEqual(agent.future_pair.snapshot(), pair)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_box_quote_converts_without_changing_other_candidates_or_pair(self, _today):
        agent, draft = self.draft_for_quote({**QUOTES, DATES[1]: (None, 3)})
        before, pair = deepcopy(draft.result), agent.future_pair.snapshot()
        reply = agent.run("11/28에 5kg 상자당 13,000원")
        self.assertIn("5kg 상자당 13,000원을 **2,600원/kg**으로 환산", reply["text"])
        self.assertIn("83,950원 더 남습니다", reply["text"])
        self.assertEqual(draft.by_date[DATES[1]], (Decimal(2600), 3))
        self.assertEqual(draft.result["rows"][::2], before["rows"][::2])
        self.assertEqual(draft.common, COMMON)
        self.assertEqual(agent.future_pair.snapshot(), pair)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_box_quote_holds_until_actual_sale_weight_is_known(self, _today):
        agent, draft = self.draft_for_quote({**QUOTES, DATES[1]: (None, 3)})
        before = deepcopy(draft.result)
        for question in ("11/28에 10kg 상자당 13,000원인데 실제 상자 무게는 몰라요",
                         "11/28에 상자당 13,000원인데 무게는 몰라요", "5kg 또는 10kg"):
            with self.subTest(question=question):
                reply = agent.run(question)["text"]
                self.assertIn("실제 판매할 상자 한 개", reply)
                self.assertEqual(reply.count("?"), 1)
                self.assertEqual(draft.result, before)
        reply = agent.run("5kg이에요")["text"]
        self.assertIn("2,600원/kg**으로 환산", reply)
        self.assertEqual(draft.by_date[DATES[1]], (Decimal(2600), 3))
        self.assertIsNone(draft.pending_quote)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_different_fee_basis_asks_for_comparable_price_before_mutating(self, _today):
        agent, draft = self.draft_for_quote(QUOTES)
        before = deepcopy(draft.result)
        for question in ("11/28에 수수료 차감 후 상자당 13,000원", "수수료를 뺀 뒤 kg당 2,600원"):
            with self.subTest(question=question):
                reply = agent.run(question)["text"]
                self.assertIn("수수료를 빼기 전", reply)
                self.assertIn("11월 28일 견적도 같은 기준으로", reply)
                self.assertEqual(reply.count("?"), 1)
                self.assertEqual(draft.result, before)
        reply = agent.run("수수료 차감 전 kg당 2,600원이에요")["text"]
        self.assertIn("83,950원 더 남습니다", reply)
        self.assertEqual(draft.common, COMMON)
        self.assertEqual(draft.by_date[DATES[1]], (Decimal(2600), 3))
        self.assertIsNone(draft.pending_quote)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_cancel_or_form_edit_discards_pending_quote(self, _today):
        agent, draft = self.draft_for_quote(QUOTES)
        agent.run("11/28에 수수료 차감 후 kg당 2,600원")
        self.assertIsNotNone(draft.pending_quote)
        self.assertIn("그대로", agent.run("취소해줘")["text"])
        self.assertIsNone(draft.pending_quote)
        agent.run("11/28에 상자당 13,000원")
        self.assertIsNotNone(draft.pending_quote)
        draft.invalidate()
        self.assertIsNone(draft.pending_quote)
        self.assertEqual(draft.by_date, QUOTES)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_short_weight_reaches_original_quote_after_read_only_glossary(self, _today):
        agent, draft = self.draft_for_quote({**QUOTES, DATES[1]: (None, 3)})
        agent.client = FakeClient([response(call("explain_term", {"topic": "cost_policy"}))])
        agent.run("11/28에 상자당 13,000원")
        before = deepcopy(draft)
        term = agent.run("수수료가 뭐예요?")
        offered = {tool["name"] for tool in agent.client.requests[0]["tools"]}
        self.assertEqual(offered, {"explain_term"})
        self.assertEqual(term["visible_results"][0]["result"]["topic"], "price_basis")
        self.assertIn("수수료", term["text"])
        self.assertEqual(draft, before)
        self.assertIn("2,600원/kg", agent.run("5kg")["text"])
        self.assertEqual(draft.by_date[DATES[1]], (Decimal(2600), 3))

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_date_correction_moves_short_price_to_the_new_candidate(self, _today):
        for correction in ("11/30이요", "11/30 견적", "그럼 11/30 견적으로 볼게요", "이번엔 11/30으로"):
            with self.subTest(correction=correction):
                agent, draft = self.draft_for_quote(QUOTES)
                agent.run("11/28에 수수료 차감 후 kg당 2,700원")
                self.assertIn("11월 30일 견적", agent.run(correction)["text"])
                self.assertEqual(draft.by_date, QUOTES)
                self.assertIn("11월 30일 견적을", agent.run("2,600원")["text"])
                self.assertEqual(draft.by_date, {**QUOTES, DATES[2]: (Decimal(2600), 5)})

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_cancel_and_unlisted_date_block_an_unbound_short_price(self, _today):
        for correction in ("그 견적은 취소할게요", "견적 입력 취소", "그냥 둘게요", "11/29이요"):
            with self.subTest(correction=correction):
                agent, draft = self.draft_for_quote(QUOTES)
                before = deepcopy(draft)
                agent.run("11/28에 수수료 차감 후 kg당 2,700원")
                agent.run(correction)
                self.assertIsNone(draft.pending_quote)
                self.assertIn("날짜와 kg당 단가", agent.run("2,600원")["text"])
                self.assertEqual(draft, before)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_confirmed_quote_before_the_current_reference_keeps_the_reference(self, _today):
        agent, draft = self.draft_for_quote({**QUOTES, DATES[0]: (None, 1)})
        self.assertEqual(draft.result["reference_date"], "2026-11-28")
        reply = agent.run("2026-11-23에 2,500원/kg으로 확정했어요.")
        self.assertIn("기준 후보일인 11월 28일", reply["text"])
        self.assertIn("222,175원 덜 남습니다", reply["text"])
        self.assertEqual(draft.result["reference_date"], "2026-11-28")
        self.assertTrue(draft.result["ranking_complete"])

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_outside_excluded_or_unconfirmed_quote_keeps_existing_comparison(self, _today):
        agent, draft = self.draft_for_quote(QUOTES)
        draft.unavailable.add(DATES[-1])
        draft.result = draft.calculate()
        previous = deepcopy(draft)
        cases = (
            ("11/29에 kg당 2,600원 받기로 했어요", "현재 후보일"),
            ("11/30에 kg당 2,600원 받기로 했어요", "출하 불가일"),
            ("11/28에 kg당 2,600원 받으면 어떨까요?", "날짜와 kg당 단가"),
            ("11/28이나 11/23에 kg당 2,600원 받기로 했어요", "날짜와 kg당 단가"),
            ("11/28에 kg당 0원 받기로 했어요", "0원보다 커야"),
        )
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertIn(expected, agent.run(question)["text"])
                self.assertEqual(draft, previous)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_stores_quote_in_a_partial_draft_and_asks_only_for_needed_value(self, _today):
        agent = AgentSession(client=FakeClient([]))
        agent.run("감귤 3톤을 11/20부터 보관해서 11/23, 11/28, 11/30 출하 후보일을 비교해줘.")
        draft = agent.current_shipment_candidates()
        reply = agent.run("28일에 kg당 2,600원입니다.")
        self.assertIn("단가의 수수료 기준부터", reply["text"])
        self.assertEqual(draft.by_date, {DATES[1]: (Decimal(2600), None)})
        self.assertIsNone(draft.result)

    def test_missing_cost_is_not_zero_and_immediate_shipment_has_no_storage_cost(self):
        required = compare_shipment_candidates(DATES, excluded_dates=[], by_date=QUOTES,
                                               **{**COMMON, "daily_storage": None})
        self.assertEqual(required["status"], "NEEDS_INPUT")
        result = compare_shipment_candidates(DATES, excluded_dates=[],
                                             by_date={**QUOTES, DATES[0]: (2500, None)}, **{**COMMON, "start": DATES[0]})
        self.assertEqual(Decimal(result["rows"][0]["proceeds_krw"]), 7120000)
        self.assertEqual(result["rows"][0]["storage_cost_krw"], "0")

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_chat_exclusion_invalidates_only_candidate_report_and_preserves_inputs(self, _today):
        agent = AgentSession(client=FakeClient([]))
        before = agent.future_pair.snapshot()
        self.assertIsNone(requested_candidate_dates("11월 23일과 11월 30일 출하 비교해줘.", TODAY))
        self.assertIsNone(requested_candidate_dates(
            "보관 시작일은 11월 20일이고 A안 11월 23일, B안 11월 30일 출하 비교해줘.", TODAY))
        self.assertIsNone(requested_candidate_dates("11/23, 11/28, 11/30 출하 후보일 비교는 하지 말아 주세요.", TODAY))
        reply = agent.run("감귤 3톤을 11/20부터 보관해서 11/23, 11/28, 11/30 출하일을 비교해줘.")
        self.assertIn("11월 30일", reply["text"])
        draft = agent.current_shipment_candidates()
        draft.common.update(COMMON)
        draft.by_date.update(QUOTES)
        draft.result = compare_shipment_candidates(draft.dates, excluded_dates=[], by_date=QUOTES, **COMMON)
        reply = agent.run("11/30은 인수가 안 돼요.")
        self.assertIn("비교에서 제외", reply["text"])
        self.assertEqual(draft.eligible, DATES[:2])
        self.assertIsNone(draft.result)
        self.assertEqual(draft.common, COMMON)
        self.assertEqual(draft.by_date, QUOTES)
        self.assertEqual(agent.future_pair.snapshot(), before)

    @patch("services.data_refresh.kst_today", return_value=TODAY)
    def test_short_date_can_fill_missing_slot_after_an_exclusion(self, _today):
        agent = AgentSession(client=FakeClient([]))
        reply = agent.run("11월 23일, 30일 출하 후보일 비교해줘. 30일은 인수 불가예요.")
        self.assertIn("날짜를 하나 더", reply["text"])
        agent.run("28일이요.")
        self.assertEqual(agent.current_shipment_candidates().eligible, DATES[:2])
