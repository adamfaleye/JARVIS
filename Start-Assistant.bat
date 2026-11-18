@echo off
rem AI Sidekicks client assistant: (re)starts one client's assistant and publishes it on Tailscale Funnel.
rem Usage: Start-Assistant.bat [client]   (default aisidekicks). Start-Sidekicks.bat calls this.
rem First run: a browser page opens to create your login. No typing in this window.
rem Put a shortcut to this file in your Startup folder (Win+R, shell:startup) so it starts after every reboot.
cd /d "%~dp0"
set "TENANT=%~1"
if "%TENANT%"=="" set "TENANT=aisidekicks"
title Assistant: %TENANT%
rem One assistant runs at a time on port 8600.
if not exist ".venv\Scripts\python.exe" (
  echo JARVIS isn't set up yet. Double-click Setup.bat first.
  pause
  exit /b
)
if not exist "data\clients" mkdir "data\clients"
if not exist "data\clients\%TENANT%.db" ".venv\Scripts\python.exe" run.py assistant init --tenant %TENANT%

rem 1. Stop a copy that's already running (found by its process id, only if that id is still a Python process).
if not exist "data\clients\%TENANT%.pid" goto start_server
set "OLDPID="
set /p OLDPID=<"data\clients\%TENANT%.pid"
if not defined OLDPID goto clear_pid
tasklist /fi "PID eq %OLDPID%" 2>nul | findstr /i "python" | find "%OLDPID%" >nul && taskkill /pid %OLDPID% /t /f >nul 2>nul
timeout /t 2 >nul
:clear_pid
del "data\clients\%TENANT%.pid" >nul 2>nul

:start_server
rem Also stop an older copy that predates the process-id file (whatever Python process holds port 8600).
for /f "tokens=5" %%p in ('netstat -ano ^| find "LISTENING" ^| find "127.0.0.1:8600 "') do tasklist /fi "PID eq %%p" 2>nul | findstr /i "python" | find "%%p" >nul && taskkill /pid %%p /t /f >nul 2>nul
timeout /t 1 >nul
rem 2. The assistant (dashboard + Instagram webhook) on this PC, port 8600. It runs with no window, so closing this window doesn't stop it.
rem    Its log: data\clients\%TENANT%-server.log
start "" "%~dp0.venv\Scripts\pythonw.exe" -u run.py assistant serve --tenant %TENANT% --https-proxy
timeout /t 8 >nul
if not exist "data\clients\%TENANT%.pid" (
  echo.
  echo The assistant didn't start. The reason is at the end of data\clients\%TENANT%-server.log
  pause
  exit /b
)
echo Assistant running. If your login doesn't exist yet, a browser page opened to create it (link works for 1 hour).

rem 3. Public HTTPS address via Tailscale Funnel.
set "TS=tailscale"
where tailscale >nul 2>nul || set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not "%TS%"=="tailscale" if not exist "%TS%" (
  echo.
  echo Tailscale isn't installed yet. The dashboard works on this PC at http://127.0.0.1:8600
  pause
  exit /b
)
echo.
echo Turning on the public address. If a login.tailscale.com link appears, Ctrl+click it and approve Funnel; this window continues by itself.
"%TS%" funnel --bg 8600
if errorlevel 1 (
  echo.
  echo Tailscale couldn't publish it yet. If it says you're logged out, open Tailscale from the system tray and sign in, then run this again.
  echo The dashboard still works on this PC at http://127.0.0.1:8600
  pause
  exit /b
)
echo.
"%TS%" funnel status
echo.
echo The %TENANT% dashboard and the Instagram webhook are at the https address above. You can close this window; the assistant keeps running.
echo Webhook URL for the Meta app: that address + /webhooks/instagram
pause
