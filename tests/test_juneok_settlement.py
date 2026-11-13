"""Independent monetary answers, atomic files, provenance and keyless user journey."""
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from agents.juneok.agent import AgentSession
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.settlement import ShipmentStore, empty_values, reconcile, digest, _canonical, normalize
from agents.juneok.settlement_reporting import render_settlement
from agents.juneok.reporting import render_report
from agents.juneok.tools import execute_tool
from agents.juneok.config import Settings


def sample():
    v = empty_values()
    v.update(input_kind="hypothetical", shipment_date="2026-11-09", sold_weight_kg="2940", gross_krw="9702000",
        statement_net_krw="9156900", received_krw="9156900", deductions_complete=True, external_complete=True,
        deductions=[{"id": "d1", "category": "commission", "amount_krw": "485100"},
                    {"id": "d2", "category": "transport", "amount_krw": "60000"}],
        external_costs=[{"id": "e1", "category": "storage", "amount_krw": "140000"}])
    return v


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.ws = SharedWorkspace(AgentSession())
        self.ws.load_demo()
        self.case = self.ws.export_case("기준")
        self.store = self.ws.shipment
        self.store.connect(self.case, "seven_days")

    def result(self, **patches):
        values = sample()
        values.update(patches)
        self.store.confirm(values, self.store.revision)
        return reconcile(self.store)

    def test_independent_gross_deduction_external_cash_answers(self):
        r = self.result()
        self.assertEqual(r["status"], "OK")
        expected = {"gross_krw": "9702000.00", "deductions_krw_known": "545100.00", "expected_net_krw": "9156900.00",
            "statement_delta_krw": "0.00", "after_entered_costs_krw": "9016900.00", "received_delta_krw": "0.00"}
        for k, v in expected.items(): self.assertEqual(r['amounts'][k], v)

    def test_partial_costs_do_not_become_zero(self):
        r = self.result(deductions=[], deductions_complete=False, external_costs=[], external_complete=False)
        for k in ["expected_net_krw", "deductions_krw", "external_krw", "after_entered_costs_krw"]: self.assertIsNone(r['amounts'][k])
        self.assertEqual(r['status'], 'PARTIAL')

    def test_explicit_empty_costs_mean_confirmed_zero(self):
        r = self.result(deductions=[], external_costs=[], statement_net_krw="9702000", received_krw=None)
        self.assertEqual(r['amounts']['expected_net_krw'], '9702000.00')
        self.assertEqual(r['amounts']['after_entered_costs_krw'], '9702000.00')
        self.assertIsNone(r['amounts']['received_delta_krw'])

    def test_unknown_amount_inside_confirmed_list_stays_partial(self):
        r = self.result(external_costs=[{'id':'e1','category':'storage','amount_krw':None}])
        self.assertIsNone(r['amounts']['after_entered_costs_krw'])
        self.assertIn('정산 밖 비용 전체 확인 필요', r['pending'])

    def test_discrepancy_does_not_double_deduct_n(self):
        r = self.result(statement_net_krw="9000000", received_krw="8900000")
        self.assertEqual(r['status'], 'CHECK_REQUIRED')
        self.assertEqual(r['amounts']['statement_delta_krw'], '-156900.00')
        self.assertEqual(r['amounts']['after_entered_costs_krw'], '8860000.00')
        self.assertEqual(r['amounts']['received_delta_krw'], '-100000.00')

    def test_no_cash_inference(self):
        r = self.result(received_krw=None)
        self.assertIsNone(r['amounts']['received_delta_krw'])
        self.assertIn('입금액 미확인: 정산 검산과 별개', r['pending'])

    def test_duplicate_same_id_blocks_after_cost_even_if_confirmed(self):
        r = self.result(external_costs=[{'id':'d2','category':'transport','amount_krw':'60000'}], overlap_distinct=True)
        self.assertIsNone(r['amounts']['after_entered_costs_krw'])
        self.assertTrue(any('동일 항목 ID' in x for x in r['checks']))

    def test_same_category_requires_distinct_cost_confirmation(self):
        items = [{'id':'e1','category':'transport','amount_krw':'20000'}]
        self.assertIsNone(self.result(external_costs=items)['amounts']['after_entered_costs_krw'])
        self.assertEqual(self.result(external_costs=items, overlap_distinct=True)['amounts']['after_entered_costs_krw'], '9136900.00')

    def test_more_deductions_than_gross_is_unsupported(self):
        r = self.result(gross_krw='100')
        self.assertIsNone(r['amounts']['expected_net_krw'])
        self.assertTrue(any('미지원' in x for x in r['checks']))

    def test_weight_difference_is_not_automatic_loss(self):
        r = self.result(initial_weight_kg='3000')
        self.assertTrue(any('감모로 자동 분류하지 않음' in x for x in r['checks']))
        self.assertNotIn('loss_percent', r['amounts'])

    def test_multiple_sales_and_rounding_independent(self):
        rows = [{'id':'s1','weight_kg':'1.111','price_per_kg':'100.01'}, {'id':'s2','weight_kg':'2.222','price_per_kg':'200.02'}]
        r = self.result(sales=rows, gross_krw=None, sold_weight_kg='3.333', deductions=[], external_costs=[], statement_net_krw='555.56', received_krw='555.56')
        # 111.11111 + 444.44444 = 555.55555 -> 555.56, rounded after summing.
        self.assertEqual(r['amounts']['row_gross_krw'], '555.56')
        self.assertEqual(r['status'], 'OK')

    def test_sales_mismatch_is_visible(self):
        r = self.result(sales=[{'id':'s1','weight_kg':'1','price_per_kg':'100'}])
        self.assertTrue(any('매출과 판매 행' in x for x in r['checks']))
        self.assertTrue(any('판매중량과 판매 행' in x for x in r['checks']))

    def test_incomplete_sale_does_not_create_revenue(self):
        r = self.result(gross_krw=None, sales=[{'id':'s1','weight_kg':'1','price_per_kg':None}])
        self.assertIsNone(r['amounts']['gross_krw'])

    def test_invalid_values_atomic(self):
        self.result()
        before = self.store.snapshot()
        for value in ['-1', 'NaN', 'Infinity', '1e40', '1.234', True, '9'*40, {}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError): self.result(gross_krw=value)
            self.assertEqual(before, self.store.snapshot())
        for patch in [{'shipment_date':'2026-02-30'}, {'deductions_complete':1}, {'input_kind':'verified_bank'}]:
            with self.assertRaises(ValueError): self.result(**patch)
            self.assertEqual(before, self.store.snapshot())

    def test_duplicate_ids_and_unknown_fields(self):
        for patch in [{'deductions': sample()['deductions'] * 2}, {'extra':'private'}, {'event_type':'return'}]:
            with self.assertRaises(ValueError): self.result(**patch)

    def test_original_case_immutable_and_copies_detached(self):
        base = self.store.snapshot()['baseline']
        scenario = self.ws.store.snapshot()
        self.result()
        self.result(gross_krw='9800000')
        self.assertEqual(self.store.snapshot()['baseline'], base)
        self.assertEqual(self.ws.store.snapshot(), scenario)
        detached = self.store.snapshot();detached['baseline']['selected_option'] = 'bad'
        self.assertEqual(self.store.snapshot()['baseline'], base)

    def test_option_id_current_does_not_collide_with_immediate_choice(self):
        self.ws.store.scenario['options'][0]['id'] = 'current'
        self.ws.calculate()
        self.store.clear()
        self.store.connect(self.ws.export_case('기준'), {'kind':'delayed','option_id':'current'})
        self.assertEqual(self.store.snapshot()['baseline']['selected_option'], {'kind':'delayed','option_id':'current'})
        other = ShipmentStore();other.restore(self.store.export(), 0)
        self.assertEqual(other.snapshot()['baseline'], self.store.snapshot()['baseline'])

    def test_version_conflict_and_noop(self):
        self.result()
        with self.assertRaises(ValueError): self.store.confirm(sample(), 0)
        self.assertFalse(self.store.confirm(sample(), 1))
        self.assertEqual(self.store.revision, 1)

    def test_provenance_revisions_and_fresh_citations(self):
        before = self.result()
        after = self.result(statement_net_krw='9000000')
        node = self.store.snapshot()['revisions'][-1]
        self.assertEqual(node['provenance']['gross_krw']['confirmed_revision'], 1)
        self.assertEqual(node['provenance']['statement_net_krw']['confirmed_revision'], 2)
        self.assertTrue(set(before['citations']).isdisjoint(after['citations']))
        for key, ref in after['amount_refs'].items():
            item = after['citations'][ref]
            self.assertEqual(item['value'], after['amounts'][key])
            self.assertTrue(all(after['citations'][x]['revision'] == 2 for x in item['input_refs']))

    def test_file_roundtrip_recomputes_same_citations(self):
        before = self.result()
        doc = json.loads(self.store.export())
        self.assertEqual(doc['derived_result'], before)
        other = ShipmentStore();other.restore(self.store.export(), 0)
        after = reconcile(other)
        self.assertEqual(before['citations'], after['citations'])
        self.assertTrue(after['imported_not_verified'])

    def test_bad_json_keeps_existing_record(self):
        self.result(); before = self.store.snapshot()
        doc = json.loads(self.store.export())
        variants = [b'{}', b'{"format":1,"format":2}', b'x' * (1024*1024+1), b'{"x":NaN}']
        for change in ['hash', 'version', 'derived', 'history', 'provenance', 'baseline']:
            d = deepcopy(doc)
            if change == 'hash': d['sha256'] = '0'*64
            if change == 'version': d['version'] = True
            if change == 'derived': d['derived_result']['amounts']['gross_krw'] = '123'
            if change == 'history': d['payload']['revisions'][0]['previous_sha256'] = '0'*64
            if change == 'provenance': d['payload']['revisions'][0]['provenance']['gross_krw']['confirmed_revision'] = True
            if change == 'baseline': d['payload']['baseline']['snapshot']['scenario']['quantity_kg'] = 1
            if change != 'hash': d['sha256'] = digest(d['payload'])
            variants.append(_canonical(d))
        for data in variants:
            with self.subTest(data=data[:40]), self.assertRaises(ValueError): self.store.restore(data, self.store.revision)
            self.assertEqual(self.store.snapshot(), before)

    def test_exports_do_not_contaminate_legacy_case(self):
        r = self.result()
        self.ws._freeze([{'tool':'reconcile_settlement','result':r}], 'manual')
        d = json.loads(self.ws.export_case('비교'))
        self.assertEqual(d['payload']['archived_results'], [])

    def test_no_automatic_model_transfer(self):
        self.result()
        r = execute_tool('reconcile_settlement', {'expected_record_revision':1}, self.ws.store, shipment=self.store)
        self.assertEqual(r['amounts']['after_entered_costs_krw'], '9016900.00')
        from agents.juneok.settlement import model_receipt
        receipt = model_receipt(r)
        self.assertNotIn('amounts', receipt)
        self.assertNotIn('citations', receipt)
        self.assertNotIn('9016900', json.dumps(receipt))
        r = execute_tool('reconcile_settlement', {'expected_record_revision':0}, self.ws.store, shipment=self.store)
        self.assertEqual(r['status'], 'INVALID_INPUT')

    def test_terminal_agent_citation_same_as_program(self):
        from test_juneok_harness import FakeClient, call, response
        self.result(statement_net_krw='9000000')
        self.ws.session.client = FakeClient([response(call('reconcile_settlement', {'expected_record_revision':1, 'finish_turn':True}))])
        before = self.ws.store.snapshot()
        reply = self.ws.session.run('정산 검산해줘')
        self.assertEqual(reply['status'], 'OK')
        self.assertEqual(reply['visible_results'][0]['result']['status'], 'CHECK_REQUIRED')
        self.assertIn('8,860,000.00', reply['text'])
        self.assertIn('계산-', reply['text'])
        self.assertEqual(before, self.ws.store.snapshot())

    def test_renderer_references_and_no_fake_public_evidence(self):
        r = self.result()
        text = render_report([{'tool':'reconcile_settlement','result':r}])
        self.assertIn('9,016,900.00', text)
        self.assertIn('가상 예시', text)
        self.assertNotIn('http', text)
        self.assertNotIn('C:\\', text)
        for ref in r['amount_refs'].values(): self.assertIn(f'[{ref}]', text)

    def test_reset_clears_record_and_model_context(self):
        self.result()
        self.ws.clear_conversation(preserve_conditions=False)
        self.assertIsNone(self.store.snapshot())
        self.assertEqual(self.ws.session.messages, [])

    def test_followup_model_request_gets_receipt_not_saved_money(self):
        from test_juneok_harness import FakeClient, call, response
        self.result()
        client = FakeClient([response(call('reconcile_settlement', {'expected_record_revision':1, 'finish_turn':False})),
            response(call('reconcile_settlement', {'expected_record_revision':1, 'finish_turn':True})),
            response(call('reconcile_settlement', {'expected_record_revision':2, 'finish_turn':True}))])
        self.ws.session.client = client
        reply = self.ws.session.run('확인한 정산 기록을 검산해줘')
        self.assertIn('9,016,900.00', reply['text'])
        self.result(statement_net_krw='9000000')
        reply = self.ws.session.run('정정한 정산 기록으로 다시 검산해줘')
        self.assertIn('8,860,000.00', reply['text'])
        for request in client.requests:
            raw = json.dumps(request, ensure_ascii=False, default=str)
            for private in ['9702000', '9156900', '9016900', '8860000', '485100', 'statement_net_krw', 'citations']:
                self.assertNotIn(private, raw)
        self.assertEqual(reply['visible_results'][0]['result']['record_revision'], 2)

    def test_private_export_is_rejected_by_package_guard(self):
        from agents.juneok.settlement import reject_private_record
        self.result()
        with self.assertRaises(ValueError): reject_private_record(self.store.export())
        reject_private_record(b'{"public_data": true}')

    def test_pdf_contains_amounts_same_references_and_no_private_path(self):
        from pypdf import PdfReader
        from agents.juneok.pdf_report import render_pdf_report
        r = self.result()
        text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(render_pdf_report([{'tool':'reconcile_settlement','result':r}]))).pages)
        for value in ['9,016,900.00', '9,156,900.00', r['amount_refs']['after_entered_costs_krw'], '가상 예시']:
            self.assertIn(value, text)
        self.assertNotIn('C:\\', text)

    def test_public_pdf_urls_are_clickable_and_plain_address_retained(self):
        from agents.juneok.evidence import get_historical_shipment_context
        from agents.juneok.pdf_report import render_pdf_report, _inline
        from pypdf import PdfReader
        result = get_historical_shipment_context({'shipment_variety':'노지온주','target_month':11,'start_year':2018,'end_year':2024})
        pdf = PdfReader(BytesIO(render_pdf_report([{'tool':'get_historical_shipment_context','result':result}])))
        urls = [a.get_object()['/A'].get('/URI') for page in pdf.pages for a in page.get('/Annots', []) if '/A' in a.get_object()]
        self.assertIn(result['source']['url'], urls)
        self.assertIn(result['source']['url'], '\n'.join(p.extract_text() for p in pdf.pages))
        for raw in ['[bad](javascript:alert)', '[bad](file:///etc/passwd)', '[bad](https://user:pass@example.com)']:
            self.assertNotIn('<link', _inline(raw))


class SettlementUiTests(unittest.TestCase):
    def test_keyless_connect_edit_confirm_and_hide_stale(self):
        from streamlit.testing.v1 import AppTest
        root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {'OPENAI_API_KEY':''}):
            app = AppTest.from_file(str(root/'app.py'), default_timeout=25).run()
            def button(label): return next(b for b in app.button if b.label == label)
            def input_(label): return next(b for b in app.text_input if b.label == label)
            button('가상 예시 조건 불러오기').click().run()
            button('현재 비교와 출하 기록 연결').click().run()
            self.assertEqual(list(app.exception), [])
            input_('정산서 매출(원)').set_value('1000').run()
            input_('정산서 지급액(원)').set_value('900').run()
            button('정산 입력 확인·저장').click().run()
            self.assertEqual(app.session_state.workspace.shipment.revision, 1)
            self.assertTrue(any(b.label == '개인 출하·정산 JSON 내려받기' for b in app.get('download_button')))
            ws = app.session_state.workspace
            old = reconcile(ws.shipment)
            ws._freeze([{'tool':'reconcile_settlement','result':old}], 'test')
            app.session_state.agent_messages = [{'role':'assistant','reply':{'text':render_settlement(old),'visible_results':[{'tool':'reconcile_settlement','result':old}], 'state':ws.store.snapshot()}}]
            app.session_state.pdf_cache = ('private', b'private cached report')
            input_('정산서 지급액(원)').set_value('800').run()
            self.assertFalse(any(b.label == '개인 출하·정산 JSON 내려받기' for b in app.get('download_button')))
            self.assertEqual(app.session_state.agent_messages, [])
            self.assertNotIn('pdf_cache', app.session_state)
            self.assertFalse(any(old['amount_refs']['gross_krw'] in str(x.value) for x in app.markdown))
            button('정산 입력 확인·저장').click().run()
            self.assertEqual(app.session_state.workspace.shipment.revision, 2)
            self.assertFalse(any('모델 API 전송에 동의' in x.label for x in app.checkbox))
            self.assertEqual(list(app.exception), [])


if __name__ == '__main__': unittest.main()
