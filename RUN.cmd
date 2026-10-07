@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First follow the setup commands in README.md to create .venv.
    pause
    exit /b 1
)
echo Open http://127.0.0.1:8000/docs in your browser.
echo Press Ctrl+C to stop the server.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
