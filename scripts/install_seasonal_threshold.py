"""Safely append a collapsible market reference to the existing A/B result UI.

Never replaces app.py or the team's full future_pair_ui.py. An exact anchor is
required; otherwise it does not modify any file. Can be run repeatedly.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = '\n\ndef _render_future_pair_pdf(entries, location):'
INSERT = ('\n    from .seasonal_threshold_ui import render_threshold_evidence\n'
          '    render_threshold_evidence(result, location)\n')


def install(root: Path = ROOT):
    root = Path(root)
    target = root / 'agents/juneok/future_pair_ui.py'
    for path in ('agents/common/seasonal_prices.py', 'agents/common/seasonal_threshold.py',
                 'agents/juneok/seasonal_threshold_ui.py'):
        if not (root / path).is_file():
            raise FileNotFoundError(f'필요한 파일이 없습니다: {path}')
    if not target.is_file():
        raise FileNotFoundError('agents/juneok/future_pair_ui.py를 찾지 못했습니다.')
    raw = target.read_bytes()
    text = raw.decode('utf-8-sig').replace('\r\n','\n')
    if '    render_threshold_evidence(result, location)' in text:
        print('[OK] 이미 시장 근거 기능이 연결되어 있습니다.')
        return False
    if text.count(ANCHOR) != 1 or 'def render_future_pair_result(result, location' not in text:
        raise ValueError('팀원이 변경한 UI와 삽입 위치가 달라 수정을 중단했습니다.')
    backup = root / 'artifacts/local-backups/future_pair_ui.py.before-seasonal-threshold.bak'
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_bytes(raw)
    updated = text.replace(ANCHOR, INSERT + ANCHOR, 1)
    newline = '\r\n' if b'\r\n' in raw else '\n'
    updated = updated.replace('\n', newline)
    temporary = target.with_name('future_pair_ui.py.threshold-tmp')
    temporary.write_bytes((b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + updated.encode('utf-8'))
    temporary.replace(target)
    print('[OK] 기존 A/B 손익 결과에 다년도 시장 근거 패널을 연결했습니다.')
    return True


if __name__ == '__main__':
    install()
