param(
    [string]$Python = '.test-tmp\launcher-build-env\Scripts\python.exe',
    [string]$CatalogUrl = 'https://launcher.monkeyservers.com/catalog.json',
    [string]$ClientId = '0faa7c4a-ff72-4202-8cc9-9e246c4dae89'
)
$ErrorActionPreference = 'Stop'
$launcherRepo = Split-Path -Parent $PSScriptRoot
Push-Location $launcherRepo
try {
    $launcherBuildInput = Join-Path $launcherRepo '.test-tmp\launcher-build-input'
    $launcherOutput = Join-Path $launcherRepo '.test-tmp\launcher-release'
    New-Item -ItemType Directory -Path $launcherBuildInput,$launcherOutput -Force | Out-Null
    @{catalog_source=$CatalogUrl; microsoft_client_id=$ClientId} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $launcherBuildInput 'defaults.json') -Encoding utf8NoBOM
    $launcherBuildArguments = @('-m','PyInstaller','--noconfirm','--onefile','--console','--hide-console','hide-early',
        '--name','Monkeycraft-Launcher-Windows-x64','--paths',$launcherRepo,
        '--distpath',$launcherOutput,'--workpath',(Join-Path $launcherBuildInput 'work'),
        '--specpath',$launcherBuildInput,'--icon',(Join-Path $launcherRepo 'mc_manager\launcher\assets\launcher.ico'),
        '--copy-metadata','minecraft-launcher-lib','--collect-all','minecraft_launcher_lib',
        '--exclude-module','mc_manager.controller','--exclude-module','mc_manager.agent',
        '--exclude-module','mc_manager.discord_bot','--exclude-module','mc_manager.launcher.api',
        '--exclude-module','mc_manager.launcher.store','--exclude-module','pytest',
        '--add-data',((Join-Path $launcherBuildInput 'defaults.json') + ':mc_manager/launcher'))
    foreach ($asset in @('launcher.ico','launcher-icon.png','overworld.png','civilizations.png','vanilla.png','stone.png')) {
        $launcherBuildArguments += @('--add-data',((Join-Path $launcherRepo "mc_manager\launcher\assets\$asset") + ':mc_manager/launcher/assets'))
    }
    $launcherBuildArguments += (Join-Path $PSScriptRoot 'launcher_entry.py')
    & $Python @launcherBuildArguments
    if ($LASTEXITCODE) { throw 'Launcher packaging failed' }
    $launcherBinary = Join-Path $launcherOutput 'Monkeycraft-Launcher-Windows-x64.exe'
    & $launcherBinary --self-check
    if ($LASTEXITCODE) { throw 'Packaged launcher self-check failed' }
    Get-FileHash -LiteralPath $launcherBinary -Algorithm SHA256 | Format-List
} finally { Pop-Location }
