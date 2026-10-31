param([string]$Catalog)
$ErrorActionPreference = 'Stop'
$launcherRepo = Split-Path -Parent $PSScriptRoot
$launcherPython = Join-Path $launcherRepo '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $launcherPython)) {
    throw 'Install the project in .venv first (python -m venv .venv, then .venv\Scripts\python -m pip install -e ".[launcher]").'
}
Push-Location $launcherRepo
try {
    if ($Catalog) { & $launcherPython -m mc_manager.launcher.desktop --catalog $Catalog }
    else { & $launcherPython -m mc_manager.launcher.desktop }
} finally { Pop-Location }
