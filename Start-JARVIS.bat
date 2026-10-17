@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo JARVIS isn't set up yet. Double-click Setup.bat first.
  pause
  exit /b
)
start "JARVIS Engine" /min ".venv\Scripts\python.exe" run.py loop
start "JARVIS Voice" /min ".venv\Scripts\python.exe" run.py voice
