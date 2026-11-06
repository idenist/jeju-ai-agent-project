# 감귤 Tool Calling Agent — `.env` 설정 및 실행 안내

## 1. 최초 설치

프로젝트 루트(`C:\SKN projects\jeju`)에서 실행합니다.

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

`.env` 파일의 `OPENAI_API_KEY=` 뒤에 **OpenAI 플랫폼에서 발급받은 실제 API 키**를 입력하세요.

```dotenv
OPENAI_API_KEY=여기에_실제_키_입력
OPENAI_MODEL=gpt-5-mini
```

- `.env`는 `jeju/`에 놓습니다. `agents/` 내부에 놓지 않습니다.
- `.env.example`만 Git에 공유합니다. 실제 `.env`와 API 키는 공유하지 않습니다.
- `DATA_GO_KR_KEY`는 경매원천정보 수집용 키이며 **OpenAI API 키가 아닙니다.**
- ChatGPT 구독과 API 사용량은 별개입니다.

## 2. Agent 실행

```powershell
python agents/agent.py --show-tools
```

테스트 질문:

> 조생귤 특등급 소 크기 5kg 상자 3톤을 가지고 있어. 7일간 보관하면 하루 보관비 2만원, 전체 감량률 2%야. 지금 출하하는 것과 비교해 줘.

자동 테스트:

```powershell
python -m unittest discover -s tests -v
```

## 3. 인증 오류 해결

- `AuthenticationError`(401): 전달된 인증키를 서버가 거부한 것입니다. `.env`에 **진짜 OpenAI API 키**를 적었는지 확인하세요.
- 이 코드에서는 **기존 OS 환경변수가 `.env`보다 우선**합니다. 이전에 PowerShell에서 잘못된 키를 설정했다면 다음 명령으로 지우고 다시 실행하세요.

```powershell
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
python agents/agent.py --show-tools
```

- `.env`를 교체한 뒤 실행 중이던 Agent가 있다면 프로세스를 종료하고 새로 실행하세요.
- `PermissionDeniedError`(403): API 프로젝트 또는 사용 모델의 접근 권한을 점검하세요.
- `RateLimitError`(429): 계정의 사용 가능량·결제 설정·호출량 제한을 점검하세요.
- API 키가 정상이어도 실제 API 사용에 별도 결제/크레딧이 필요할 수 있습니다.

## 4. 구성

- `agents/baseline.py`: 가격 CSV 조회 및 손익분기 계산 (LLM 없음)
- `agents/agent.py`: 자연어 이해·함수 호출 및 결과 설명
- `data/processed/daily_prices.csv`: 기존 경락가격 파일 재사용
- `requirements.txt`: `requests`, `urllib3`, `openai`, `python-dotenv`

현재 가격 데이터는 서울가락시장 2026년 10~11월의 관측 거래입니다. **미래 가격 예측이나 확정적인 출하 권고를 제공하지 않습니다.**

## 5. Git에 올릴 파일

```powershell
git add agents/agent.py agents/baseline.py tests/ requirements.txt .env.example docs/감귤_Tool_Calling_Agent_실행안내.md
git commit -m "감귤 Tool Calling Agent에 환경설정 파일 지원 추가"
```

기존 `.gitignore`에 `.env`, `.env.*`, `!.env.example`이 있는지 확인하세요. `git status --short`에 `.env`가 나타나면 커밋을 진행하지 마세요.
