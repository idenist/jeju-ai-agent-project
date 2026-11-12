"""Generate two clearly fictional offline settlement reports; never load user records."""
import json
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.settlement import reconcile
from agents.juneok.reporting import render_report
from agents.juneok.pdf_report import render_pdf_report
from scripts.verify_juneok_settlement_live import fixture


def main():
    ws = SharedWorkspace(AgentSession(Settings(api_key='')))
    ws.load_demo()
    ws.shipment.connect(ws.export_case('가상 비교'), 'seven_days')
    values = fixture()
    folder = PROJECT_ROOT/'artifacts/settlement-review/implementation'
    folder.mkdir(parents=True, exist_ok=True)
    outputs = []
    for name in ['normal', 'partial']:
        if name == 'partial': values.update(external_complete=False, received_krw=None)
        ws.shipment.confirm(values, ws.shipment.revision)
        result = reconcile(ws.shipment)
        entries = [{'tool':'reconcile_settlement','result':result}]
        (folder/(name+'.pdf')).write_bytes(render_pdf_report(entries))
        (folder/(name+'.md')).write_text(render_report(entries),encoding='utf-8')
        (folder/(name+'.json')).write_bytes(ws.shipment.export())
        outputs.append({'name':name, 'record_revision':ws.shipment.revision, 'amounts':result['amounts']})
    (folder/'manifest.json').write_text(json.dumps({'scope':'FICTIONAL_ONLY_NOT_FARMER_RECORDS','outputs':outputs},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output_dir':str(folder),'fictional_reports':len(outputs),'model_called':False},ensure_ascii=False))


if __name__ == '__main__': main()
