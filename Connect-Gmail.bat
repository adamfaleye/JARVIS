@echo off
cd /d "%~dp0"
echo Connecting JARVIS to your Gmail. A browser window will open: sign in with the Gmail account JARVIS should send from and allow access.
echo If Google says it has not verified this app, click Advanced, then Go to JARVIS (it is your own app).
".venv\Scripts\python.exe" run.py gmail-auth
echo.
echo Done. Now run Stop-JARVIS, then Start-JARVIS.
pause
