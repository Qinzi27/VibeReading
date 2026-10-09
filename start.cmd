@echo off
rem Launch only the project-local Python. The console provides Ctrl+C to stop.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.ps1 before starting VibeReading.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 run.py --open %*
if errorlevel 1 pause
