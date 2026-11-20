@echo off
setlocal
cd /d "%~dp0"
py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.13 -m venv .venv
  goto install
)
python -c "import sys; sys.exit(sys.version_info[:2] != (3, 13))" >nul 2>&1
if errorlevel 1 (
  echo Python 3.13 is required. Register it with the Python launcher or add it to PATH.
  exit /b 1
)
python -m venv .venv

:install
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-juneok.lock
if errorlevel 1 exit /b 1
if not exist .env copy .env.example .env >nul
echo Setup complete. Configure OPENAI_API_KEY in .env for natural-language mode.
echo Local CSV tools do not require DATA_GO_KR_KEY.
