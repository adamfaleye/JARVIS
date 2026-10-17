@echo off
title Connect JARVIS to Claude
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='%~f0'; iex ((Get-Content -LiteralPath $f -Raw) -split ('#=='+'PS==#'),2)[1]"
echo.
pause
exit /b
#==PS==#
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $f
$cands = @($Root, "C:\JARVIS\jarvis-engine", (Join-Path $env:USERPROFILE "Desktop\jarvis-engine"), (Join-Path $env:USERPROFILE "Downloads\jarvis-engine"), (Join-Path $env:USERPROFILE "Documents\jarvis-engine"))
$Root = $cands | Where-Object { Test-Path (Join-Path $_ "mcp_server.py") } | Select-Object -First 1
if (-not $Root) { Write-Host "Put this file inside your jarvis-engine folder (next to Setup.bat) and run it again." -ForegroundColor Red; return }
$vpy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { Write-Host "JARVIS isn't installed in $Root yet. Run Setup.bat first." -ForegroundColor Red; return }
Write-Host "JARVIS Engine folder: $Root"

function Test-Server {
    $eo = Join-Path $env:TEMP "jarvis_mcp_out.txt"; $ee = Join-Path $env:TEMP "jarvis_mcp_err.txt"
    Remove-Item $eo, $ee -ErrorAction SilentlyContinue
    Start-Process -FilePath $vpy -ArgumentList "-c `"import mcp_server; print('JARVIS_OK')`"" -WorkingDirectory $Root -NoNewWindow -Wait -RedirectStandardOutput $eo -RedirectStandardError $ee | Out-Null
    $t = ((Get-Content $eo -Raw -ErrorAction SilentlyContinue) + "`n" + (Get-Content $ee -Raw -ErrorAction SilentlyContinue))
    return @{ ok = ($t -match "JARVIS_OK"); text = $t }
}
function Show-Tail($t) { ($t -split "`r?`n" | Where-Object { $_.Trim() }) | Select-Object -Last 12 | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow } }

Write-Host "Checking the engine connection server..."
$r = Test-Server
if (-not $r.ok) {
    Write-Host "  It didn't load. Error:" -ForegroundColor Yellow; Show-Tail $r.text
    Write-Host "Repairing: reinstalling the engine's packages (a minute or two)..."
    $reqs = @("-r", (Join-Path $Root "requirements.txt"))
    if (Test-Path (Join-Path $Root "requirements-voice.txt")) { $reqs += @("-r", (Join-Path $Root "requirements-voice.txt")) }
    & $vpy -m pip install --disable-pip-version-check @reqs 2>&1 | ForEach-Object { "$_" } | Select-String -Pattern "Successfully|ERROR|error:" | ForEach-Object { Write-Host "  $_" }
    $r = Test-Server
}
if (-not $r.ok) {
    Write-Host ""
    Write-Host "The connection server still won't load. Send Claude a screenshot of this window." -ForegroundColor Red
    Show-Tail $r.text
    return
}
Write-Host "  Server loads fine." -ForegroundColor Green

# Claude Desktop keeps its config in one of these, depending on how it was installed.
$dirs = @(Join-Path $env:APPDATA "Claude")
$pk = Join-Path $env:LOCALAPPDATA "Packages"
if (Test-Path $pk) { Get-ChildItem $pk -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue | ForEach-Object { $dirs += (Join-Path $_.FullName "LocalCache\Roaming\Claude") } }

$server = [pscustomobject]@{ command = $vpy; args = @((Join-Path $Root "mcp_server.py")) }
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Force $d | Out-Null
    $p = Join-Path $d "claude_desktop_config.json"
    $cfg = $null
    if (Test-Path $p) {
        Copy-Item $p "$p.backup" -Force
        try { $cfg = Get-Content $p -Raw | ConvertFrom-Json } catch { Write-Host "  Skipped $p (couldn't read it; your original is untouched)." -ForegroundColor Yellow; continue }
    }
    if (-not $cfg) { $cfg = [pscustomobject]@{} }
    if (-not $cfg.PSObject.Properties["mcpServers"]) { $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) }
    if ($cfg.mcpServers.PSObject.Properties["jarvis-engine"]) { $cfg.mcpServers."jarvis-engine" = $server }
    else { $cfg.mcpServers | Add-Member -NotePropertyName "jarvis-engine" -NotePropertyValue $server }
    [IO.File]::WriteAllText($p, ($cfg | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding $false))
    Write-Host "  Added jarvis-engine to $p" -ForegroundColor Green
}

Write-Host ""
Write-Host "Done. Now:" -ForegroundColor Cyan
Write-Host "  1. Quit Claude completely: right-click the Claude icon by the clock > Quit."
Write-Host "  2. Open Claude again, open JARVIS from the sidebar, and click Allow for jarvis-engine."
Write-Host "  3. In JARVIS: Settings > Integrations > PC link > Test."
