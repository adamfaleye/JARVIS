@echo off
cd /d "%~dp0"
title MolaBucks Capital HQ - web and VR
if not exist ".venv\Scripts\python.exe" (
  echo JARVIS isn't set up yet. Double-click Setup.bat first.
  pause
  exit /b
)
".venv\Scripts\python.exe" -c "import cryptography" 2>nul || ".venv\Scripts\python.exe" -m pip install -q "cryptography>=41"
".venv\Scripts\python.exe" run.py web
pause
