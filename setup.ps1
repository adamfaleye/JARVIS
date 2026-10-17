# JARVIS one-time setup for Windows. Run via Setup.bat (double-click).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Say($msg, $color = "Cyan") { Write-Host "`n>> $msg" -ForegroundColor $color }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE). Scroll up for the error." } }

function Find-Python {
    foreach ($c in @("py -3.12", "py -3.11", "py -3", "python")) {
        try {
            $v = cmd /c "$c --version 2>&1"
            if ($v -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 11) { return $c }
        } catch {}
    }
    return $null
}

try {
    Write-Host "J.A.R.V.I.S  ·  MolaBucks Capital  ·  setup" -ForegroundColor Cyan

    # 1. Python
    Say "Checking for Python 3.11+"
    $py = Find-Python
    if (-not $py) {
        Say "Python not found. Installing Python 3.12 (this can take a few minutes)..."
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $py = Find-Python
        if (-not $py) { throw "Python didn't install automatically. Install Python 3.12 from python.org (check 'Add Python to PATH'), then double-click Setup.bat again." }
    }
    Write-Host "   Using $py"

    # 2. Virtual environment + packages
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Say "Creating JARVIS's private Python environment"
        cmd /c "$py -m venv .venv"; Check "Creating the environment"
    }
    $vpy = Join-Path $Root ".venv\Scripts\python.exe"
    Say "Installing JARVIS components (5-10 minutes the first time)"
    & $vpy -m pip install --upgrade pip --quiet; Check "pip upgrade"
    & $vpy -m pip install -r requirements.txt -r requirements-voice.txt; Check "Installing packages"
    Say "Installing the browser JARVIS uses to read county sites"
    $env:PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT = "300000"
    $browserOk = $false
    foreach ($i in 1..2) {
        & $vpy -m playwright install chromium
        if ($LASTEXITCODE -eq 0) { $browserOk = $true; break }
        Write-Host "   Download was slow or timed out. Trying again..." -ForegroundColor Yellow
    }
    if (-not $browserOk) { Write-Host "   Skipping that download. JARVIS will use Microsoft Edge for county sites instead." -ForegroundColor Yellow }

    # 3. API key
    if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
    $envText = Get-Content ".env" -Raw
    if ($envText -notmatch "ANTHROPIC_API_KEY=\S+") {
        Say "JARVIS needs your Anthropic API key (console.anthropic.com > API Keys)" "Yellow"
        $key = (Read-Host "Paste it here and press Enter").Trim()
        if ($key) {
            $envText = [regex]::Replace($envText, "ANTHROPIC_API_KEY=.*", { param($m) "ANTHROPIC_API_KEY=$key" })
            [IO.File]::WriteAllText((Join-Path $Root ".env"), $envText, (New-Object Text.UTF8Encoding $false))
        } else { Write-Host "   Skipped. Add it later in the .env file." -ForegroundColor Yellow }
    }

    $envText = Get-Content ".env" -Raw
    if ($envText -notmatch "ELEVENLABS_API_KEY") { $envText = $envText.TrimEnd() + "`r`nELEVENLABS_API_KEY=`r`n" }
    if ($envText -notmatch "ELEVENLABS_API_KEY=\S+") {
        Say "Optional: your ElevenLabs API key for JARVIS's voice (elevenlabs.io > Developers > API Keys). Press Enter to skip." "Yellow"
        $ek = (Read-Host "ElevenLabs key").Trim()
        if ($ek) { $envText = [regex]::Replace($envText, "ELEVENLABS_API_KEY=.*", { param($m) "ELEVENLABS_API_KEY=$ek" }) }
    }
    [IO.File]::WriteAllText((Join-Path $Root ".env"), $envText, (New-Object Text.UTF8Encoding $false))

    # 4. Desktop + Start menu shortcuts
    Say "Creating the JARVIS app shortcuts"
    $ws = New-Object -ComObject WScript.Shell
    $icon = Join-Path $Root "jarvis.ico"
    $places = @(
        @{ Path = [Environment]::GetFolderPath("Desktop"); Name = "JARVIS"; Target = "Start-JARVIS.bat" },
        @{ Path = [Environment]::GetFolderPath("Programs"); Name = "JARVIS"; Target = "Start-JARVIS.bat" },
        @{ Path = [Environment]::GetFolderPath("Programs"); Name = "Stop JARVIS"; Target = "Stop-JARVIS.bat" }
    )
    foreach ($p in $places) {
        $lnk = $ws.CreateShortcut((Join-Path $p.Path ($p.Name + ".lnk")))
        $lnk.TargetPath = Join-Path $Root $p.Target
        $lnk.WorkingDirectory = $Root
        $lnk.IconLocation = $icon
        $lnk.WindowStyle = 7
        $lnk.Description = "JARVIS for MolaBucks Capital"
        $lnk.Save()
    }

    $ans = Read-Host "`nStart JARVIS automatically when you log in to Windows? (Y/N)"
    $startupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "JARVIS.lnk"
    if ($ans -match "^[Yy]") {
        $lnk = $ws.CreateShortcut($startupLnk)
        $lnk.TargetPath = Join-Path $Root "Start-JARVIS.bat"; $lnk.WorkingDirectory = $Root
        $lnk.IconLocation = $icon; $lnk.WindowStyle = 7; $lnk.Save()
        Write-Host "   JARVIS will start with Windows."
    } elseif (Test-Path $startupLnk) { Remove-Item $startupLnk }

    # 5. Connect to Claude Desktop (PC link + chat control)
    Say "Connecting JARVIS to Claude Desktop"
    $cfgDir = Join-Path $env:APPDATA "Claude"
    $cfgPath = Join-Path $cfgDir "claude_desktop_config.json"
    New-Item -ItemType Directory -Force $cfgDir | Out-Null
    $cfg = $null
    if (Test-Path $cfgPath) {
        Copy-Item $cfgPath "$cfgPath.backup" -Force
        try { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json } catch { $cfg = $null }
    }
    if (-not $cfg) { $cfg = [pscustomobject]@{} }
    if (-not $cfg.PSObject.Properties["mcpServers"]) { $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) }
    $server = [pscustomobject]@{ command = $vpy; args = @((Join-Path $Root "mcp_server.py")) }
    if ($cfg.mcpServers.PSObject.Properties["jarvis-engine"]) { $cfg.mcpServers."jarvis-engine" = $server }
    else { $cfg.mcpServers | Add-Member -NotePropertyName "jarvis-engine" -NotePropertyValue $server }
    $json = $cfg | ConvertTo-Json -Depth 20
    [IO.File]::WriteAllText($cfgPath, $json, (New-Object Text.UTF8Encoding $false))
    Write-Host "   Added. Restart Claude Desktop to load it."

    Say "JARVIS is installed." "Green"
    Write-Host "   - Double-click JARVIS on your desktop to start it (or find it in the Start menu)."
    Write-Host "   - Say 'Hey Jarvis' to talk to it. The first start downloads voice models (about a minute)."
    Write-Host "   - Restart Claude Desktop, open your JARVIS artifact there, and allow jarvis-engine to turn on the PC link."
    $go = Read-Host "`nStart JARVIS now? (Y/N)"
    if ($go -match "^[Yy]") { Start-Process (Join-Path $Root "Start-JARVIS.bat") -WorkingDirectory $Root }
}
catch {
    Write-Host "`nSetup stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Fix that, then double-click Setup.bat again. It picks up where it left off."
}
