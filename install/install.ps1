# cryptdrive Installation (Windows)
#
#   .\install\install.ps1 -Source "C:\Users\me\Documents" -Archive "G:\Meine Ablage\cryptdrive"
#
# Legt eine virtuelle Umgebung an, installiert die Abhaengigkeiten, richtet
# Konfiguration und Archiv ein, traegt den Hintergrunddienst in den Autostart
# ein und startet ihn samt Taskleistensymbol.

[CmdletBinding()]
param(
    [string]$Source,
    [string]$Archive,
    [string]$MaxArchiveSize = "100 GiB",
    [string]$MinChangeSize = "100 MiB",
    [string]$DailyTime = "03:00",
    [int]$CompressionLevel = 19,
    [switch]$NoAutostart,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "cryptdrive Installation" -ForegroundColor Cyan
Write-Host "Projektordner: $root"

# --- 1. Python finden ---
$python = $null
foreach ($cand in @("py", "python")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) { throw "Python 3.11 oder neuer wird benoetigt, aber nicht gefunden." }

# --- 2. Virtuelle Umgebung ---
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$venvPythonw = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Erzeuge virtuelle Umgebung in .venv ..."
    if ($python -like "*py.exe") { & $python -3 -m venv $venv } else { & $python -m venv $venv }
}
Write-Host "Installiere Abhaengigkeiten ..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install fehlgeschlagen." }

# --- 3. Konfiguration und Archiv ---
$configDir = Join-Path $env:LOCALAPPDATA "cryptdrive"
$configFile = Join-Path $configDir "config.toml"
if (Test-Path $configFile) {
    Write-Host "Konfiguration vorhanden: $configFile (bleibt unveraendert)"
} else {
    if (-not $Source) { $Source = Read-Host "Quellordner (wird gesichert)" }
    if (-not $Archive) { $Archive = Read-Host "Archivordner (z. B. im gemounteten Google Drive)" }
    Write-Host ""
    Write-Host "Jetzt wird das Passwort gesetzt. Aus ihm laesst sich der Schluessel" -ForegroundColor Yellow
    Write-Host "jederzeit neu ableiten, also bitte physisch sicher aufbewahren." -ForegroundColor Yellow
    & $venvPython -m cryptdrive init --source "$Source" --archive "$Archive" `
        --max-archive-size "$MaxArchiveSize" --min-change-size "$MinChangeSize" `
        --daily-time "$DailyTime" --compression-level $CompressionLevel
    if ($LASTEXITCODE -ne 0) { throw "cryptdrive init fehlgeschlagen." }
}

# --- 4. Startskript pruefen ---
$cmdFile = Join-Path $root "cryptdrive.cmd"
if (Test-Path $cmdFile) {
    Write-Host "Kommandozeile: $cmdFile"
} else {
    @"
@echo off
rem Aufruf der cryptdrive-Kommandozeile in der virtuellen Umgebung
"%~dp0.venv\Scripts\python.exe" -m cryptdrive %*
"@ | Set-Content -Path $cmdFile -Encoding ascii
    Write-Host "Kommandozeile angelegt: $cmdFile"
}

# --- 5. Autostart ---
if (-not $NoAutostart) {
    & $venvPython -m cryptdrive autostart
    if ($LASTEXITCODE -ne 0) { throw "Autostart konnte nicht eingerichtet werden." }
}

# --- 6. Verknuepfung fuer die Wiederherstellungs-GUI im Startmenue ---
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$shortcut = Join-Path $startMenu "cryptdrive Wiederherstellung.lnk"
try {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($shortcut)
    $lnk.TargetPath = $venvPythonw
    $lnk.Arguments = "-m cryptdrive restore-gui"
    $lnk.WorkingDirectory = $root
    $lnk.Description = "cryptdrive: Stand wiederherstellen"
    $lnk.Save()
    Write-Host "Startmenue-Eintrag: $shortcut"
} catch {
    Write-Warning "Verknuepfung konnte nicht erstellt werden: $($_.Exception.Message)"
}

# --- 7. Dienst starten ---
if (-not $NoStart) {
    $running = Get-Process -Name "pythonw" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $venvPythonw }
    if ($running) {
        Write-Host "Hintergrunddienst laeuft bereits."
    } else {
        Write-Host "Starte Hintergrunddienst mit Taskleistensymbol ..."
        Start-Process -FilePath $venvPythonw -ArgumentList "-m", "cryptdrive", "daemon" `
            -WorkingDirectory $root
    }
}

Write-Host ""
Write-Host "Fertig." -ForegroundColor Green
Write-Host "Status:            .\cryptdrive.cmd status"
Write-Host "Sofort sichern:    .\cryptdrive.cmd sync"
Write-Host "Wiederherstellen:  .\cryptdrive.cmd restore-gui"
Write-Host "Der taegliche Lauf ist auf $DailyTime gesetzt, verpasste Laeufe werden nachgeholt."
