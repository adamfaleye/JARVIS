@echo off
taskkill /FI "WINDOWTITLE eq JARVIS Engine*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq JARVIS Voice*" /T /F >nul 2>&1
echo JARVIS stopped.
timeout /t 2 >nul
