# cryptdrive deinstallieren: Autostart entfernen, Dienst beenden.
# Archiv, Konfiguration und Schluessel bleiben erhalten, sofern nicht
# -RemoveState oder -RemoveArchive angegeben wird.

[CmdletBinding()]
param(
    [switch]$RemoveState,     # Konfiguration, Schluessel, Index und Log loeschen
    [switch]$RemoveArchive    # zusaetzlich das Archiv im Cloud-Ordner loeschen
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$stateDir = Join-Path $env:LOCALAPPDATA "cryptdrive"

Write-Host "Entferne Autostart ..."
if (Test-Path $venvPython) {
    & $venvPython -m cryptdrive autostart --disable
} else {
    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
        -Name "cryptdrive" -ErrorAction SilentlyContinue
}

Write-Host "Beende laufende Prozesse ..."
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*cryptdrive*daemon*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\cryptdrive Wiederherstellung.lnk"
if (Test-Path $shortcut) { Remove-Item $shortcut -Force }

if ($RemoveArchive) {
    $configFile = Join-Path $stateDir "config.toml"
    if (Test-Path $configFile) {
        $archive = (Select-String -Path $configFile -Pattern '^archive\s*=\s*"(.*)"').Matches.Groups[1].Value
        $archive = $archive -replace '\\\\', '\'
        Write-Host "Archiv loeschen: $archive" -ForegroundColor Yellow
        $answer = Read-Host "Wirklich alle Sicherungen unwiderruflich loeschen? (ja/nein)"
        if ($answer -eq "ja" -and (Test-Path $archive)) {
            Remove-Item -Recurse -Force $archive
            Write-Host "Archiv geloescht."
        } else {
            Write-Host "Archiv bleibt erhalten."
        }
    }
}

if ($RemoveState) {
    Write-Host "Loesche lokalen Zustand: $stateDir" -ForegroundColor Yellow
    $answer = Read-Host "Auch die Schluesseldatei loeschen? Ohne Passwort ist das Archiv dann verloren. (ja/nein)"
    if ($answer -eq "ja" -and (Test-Path $stateDir)) {
        Remove-Item -Recurse -Force $stateDir
        Write-Host "Lokaler Zustand geloescht."
    } else {
        Write-Host "Lokaler Zustand bleibt erhalten."
    }
}

Write-Host "Fertig." -ForegroundColor Green
