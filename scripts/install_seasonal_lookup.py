"""One-time safe injection of the multi-season panel into the team's Streamlit app.

Run from project root: python -m scripts.install_seasonal_lookup
A backup of app.py is kept on the first successful installation.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = '                _render_price_history(ws, dirty)'
INSERT = ('                from agents.juneok.seasonal_prices_ui import render_seasonal_prices\n'
          '                render_seasonal_prices()\n')


def install(root: Path = ROOT):
    root = Path(root)
    app = root / 'app.py'
    if not app.is_file():
        raise FileNotFoundError('프로젝트 루트(app.py가 있는 폴더)에 압축을 풀어주세요.')
    for relative in ('agents/common/seasonal_prices.py', 'agents/juneok/seasonal_prices_ui.py'):
        if not (root / relative).is_file():
            raise FileNotFoundError(f'압축 파일이 올바르게 풀리지 않았습니다: {relative}')
    raw = app.read_bytes()
    text = raw.decode('utf-8-sig')
    if '                render_seasonal_prices()' in text:
        print('[OK] 다년도 가격 조회 화면이 이미 연결되어 있습니다.')
        return False
    if text.count(ANCHOR) != 1:
        raise ValueError('현재 app.py의 화면 구성과 패치 위치가 달라 자동 수정을 중단했습니다. app.py는 변경되지 않았습니다.')
    backup = root / 'artifacts' / 'local-backups' / 'app.py.before-seasonal-lookup.bak'
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_bytes(raw)
    newline = '\r\n' if b'\r\n' in raw else '\n'
    amended = text.replace(ANCHOR, INSERT.replace('\n', newline) + ANCHOR, 1)
    temp = app.with_name('app.py.seasonal-tmp')
    temp.write_bytes((b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + amended.encode('utf-8'))
    temp.replace(app)
    print('[OK] 자료·도구 > 자료 조회에 다년도 경락가격 비교 화면을 추가했습니다.')
    print(f'     기존 app.py 백업: {backup.relative_to(root)}')
    return True


if __name__ == '__main__':
    install()
