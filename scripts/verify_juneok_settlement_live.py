"""Paid model selection checks on fictional records only; not farmer validation."""
from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from agents.juneok.agent import AgentSession
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.settlement import empty_values, reconcile
from agents.juneok.quality import assess_turn

CASES = {
    'S01': '매출9702000원이고 정산서 지급액9156900원이야. 아직 출하 기록 카드는 연결하지 않았어. 이걸 실제 기록에 넣고 정산 검산해줘.',
    'S02': '앱에 확인한 내 출하 정산 기록이 있어. 정산서 지급액과 공제, 외부 비용을 검산해줘.',
    'S03': '카드에서 확인한 정산 내역을 검산하고, 별도 보관비를 반영하면 얼마인지 근거와 함께 보여줘. 원래 비교 조건은 바꾸지 마.',
    'S04': '정산 기록에서 아직 모르는 외부 비용을 0으로 만들지 말고 확인된 범위로만 검산해줘. 입금은 아직 확인 못 했어.',
    'S05': '정산 기록의 운송비를 두 번 빼는 건 아닌지 확인해줘. 확인 전에는 실제 순이익이라고 하지 마.',
    'S06': '확인한 출하 기록의 정산서 지급액이 매출-공제와 맞는지, 입력한 입금액도 일치하는지 검산해줘.',
}


def fixture():
    v = empty_values()
    v.update(input_kind='hypothetical', shipment_date='2026-11-09', sold_weight_kg='2940', gross_krw='9702000',
        statement_net_krw='9156900', received_krw='9156900', deductions_complete=True, external_complete=True,
        deductions=[{'id':'d1','category':'commission','amount_krw':'485100'}, {'id':'d2','category':'transport','amount_krw':'60000'}],
        external_costs=[{'id':'e1','category':'storage','amount_krw':'140000'}])
    return v


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--repeat', type=int, choices=[1,2,3], default=3)
    parser.add_argument('--case', choices=list(CASES))
    args = parser.parse_args()
    folder = PROJECT_ROOT/'artifacts/settlement-live'/datetime.now().strftime('%Y%m%dT%H%M%S')
    folder.mkdir(parents=True)
    sources = [*sorted((PROJECT_ROOT/'agents/juneok').glob('*.py')), Path(__file__)]
    report = {'scope':'FICTIONAL_KOREAN_TASKS_NOT_FARMER_VALIDATION', 'model':Settings.load().model,
        'source_sha256':{p.relative_to(PROJECT_ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}, 'trials':[]}
    for key, question in CASES.items():
        if args.case and key != args.case: continue
        for iteration in range(args.repeat):
            ws = SharedWorkspace(AgentSession(Settings.load()))
            ws.load_demo()
            if key != 'S01':
                ws.shipment.connect(ws.export_case('가상 비교'), 'seven_days')
                v = fixture()
                if key == 'S04': v.update(external_complete=False, received_krw=None)
                if key == 'S05': v['external_costs'] = [{'id':'e1','category':'transport','amount_krw':'60000'}]
                if key == 'S06': v.update(statement_net_krw='9000000', received_krw='8900000')
                ws.shipment.confirm(v, 0)
            before = ws.store.snapshot()
            record = ws.shipment.snapshot()
            turn = ws.session.run(question)
            outputs = [x['result'] for x in turn['tool_results'] if x['tool'] == 'reconcile_settlement']
            r = outputs[-1] if outputs else {}
            checks = {'completed':turn['status']=='OK', 'used_expected_tool':bool(outputs),
                'planning_state_unchanged':ws.store.snapshot()==before, 'record_unchanged':ws.shipment.snapshot()==record,
                'canonical_rendering':assess_turn(turn)['passed']}
            receipts = [json.loads(x['output']) for x in ws.session.messages if isinstance(x, dict) and x.get('type') == 'function_call_output' and 'local_result_displayed' in x.get('output', '')]
            checks['no_saved_financial_payload_in_model_history'] = bool(receipts) and all(not any(k in x for k in ['amounts','citations','baseline_ref','input_digest']) for x in receipts)
            if key == 'S01': checks['no_financial_result_without_record'] = r.get('status')=='NEEDS_INPUT' and 'amounts' not in r
            else:
                checks['same_program_citations'] = r == reconcile(ws.shipment)
                amounts = r.get('amounts', {})
                expected = {'S02':'9016900.00', 'S03':'9016900.00', 'S04':None, 'S05':None, 'S06':'8860000.00'}[key]
                checks['independent_after_cost_amount'] = 'after_entered_costs_krw' in amounts and amounts['after_entered_costs_krw']==expected
                if key=='S06': checks['independent_delta'] = amounts.get('statement_delta_krw')=='-156900.00' and amounts.get('received_delta_krw')=='-100000.00'
            trial = {'case':key, 'iteration':iteration+1, 'checks':checks, 'passed':all(checks.values()), 'turn':turn}
            report['trials'].append(trial)
            report.update(completed=len(report['trials']), passed_trials=sum(x['passed'] for x in report['trials']), passed=all(x['passed'] for x in report['trials']))
            (folder/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding='utf-8')
            (folder.parent/'latest.json').write_text(json.dumps({'evidence':str(folder/'result.json'), 'completed':report['completed'], 'passed_trials':report['passed_trials'], 'passed':report['passed']},ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'case':key,'iteration':iteration+1,'checks':checks,'passed':trial['passed']},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
