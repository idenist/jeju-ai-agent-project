@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
