@echo off
rem Stops a client's assistant and takes the public Tailscale address down. Usage: Stop-Assistant.bat [client]   (default aisidekicks)
cd /d "%~dp0"
set "TENANT=%~1"
if "%TENANT%"=="" set "TENANT=aisidekicks"
set "TS=tailscale"
where tailscale >nul 2>nul || set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
"%TS%" funnel reset >nul 2>nul && echo Public address is off.

if not exist "data\clients\%TENANT%.pid" goto done
set "OLDPID="
set /p OLDPID=<"data\clients\%TENANT%.pid"
if defined OLDPID tasklist /fi "PID eq %OLDPID%" 2>nul | findstr /i "python" | find "%OLDPID%" >nul && taskkill /pid %OLDPID% /t /f >nul 2>nul
del "data\clients\%TENANT%.pid" >nul 2>nul
:done
rem Also stop an older copy that predates the process-id file (whatever Python process holds port 8600).
for /f "tokens=5" %%p in ('netstat -ano ^| find "LISTENING" ^| find "127.0.0.1:8600 "') do tasklist /fi "PID eq %%p" 2>nul | findstr /i "python" | find "%%p" >nul && taskkill /pid %%p /t /f >nul 2>nul
echo Assistant is stopped.
timeout /t 5
