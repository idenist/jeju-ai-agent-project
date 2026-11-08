@echo off
cd /d "%~dp0"
py -3.13 -m venv .venv
if errorlevel 1 (
  echo Python 3.13 and the Python launcher are required. See README.md.
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-juneok.lock
if errorlevel 1 exit /b 1
if not exist .env copy .env.example .env >nul
echo Setup complete. Configure OPENAI_API_KEY in .env for natural-language mode.
echo Local CSV tools do not require DATA_GO_KR_KEY.
