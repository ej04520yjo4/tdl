param(
    [string]$Python = "$PSScriptRoot\..\.build\venv\Scripts\python.exe",
    [string]$Go = "$PSScriptRoot\..\.build\go\bin\go.exe",
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
$tdlRoot = (Resolve-Path "$PSScriptRoot\..").Path
Push-Location $tdlRoot
try {
    $env:GOPATH = Join-Path $tdlRoot '.build\gopath'
    $env:GOCACHE = Join-Path $tdlRoot '.build\gocache'
    $env:CGO_ENABLED = '0'
    New-Item -ItemType Directory -Force bin, dist | Out-Null
    # desktop/version.py is the single source of the release version.
    $version = (& $Python desktop/version.py).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $version) { throw 'Cannot read desktop/version.py' }
    & $Go build -trimpath -ldflags "-s -w -X github.com/iyear/tdl/pkg/consts.Version=desktop-$version" -o bin/tdl-core.exe .
    if ($LASTEXITCODE -ne 0) { throw 'Go build failed' }
    if (-not $SkipTests) {
        & $Go test ./pkg/desktopio ./pkg/key ./app/chat ./app/dl ./app/up ./app/migrate ./pkg/tmessage ./cmd ./core/downloader ./core/uploader ./core/util/tutil
        if ($LASTEXITCODE -ne 0) { throw 'Go tests failed' }
        & $Python -m pytest desktop/tests -q
        if ($LASTEXITCODE -ne 0) { throw 'Desktop tests failed' }
    }
    & $Python desktop/package_release.py
    if ($LASTEXITCODE -ne 0) { throw 'License collection failed' }
    # Qt uses Windows' ICU DLLs. Unrelated developer tools (for example
    # Poppler/Conda) may place incompatible ICU DLLs on PATH; never bundle those.
    $tdlBuildPath = $env:PATH
    try {
        $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
        & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name 'TDL下載助手' --icon "$tdlRoot/desktop/assets/app.ico" --version-file "$tdlRoot/.build/version_info.txt" --paths $tdlRoot --hidden-import desktop.backend --hidden-import desktop.channel_page --hidden-import desktop.transfer_page --hidden-import desktop.version --hidden-import desktop.channel_probe --hidden-import desktop.channel_ui_probe --hidden-import desktop.qr_probe --hidden-import desktop.restart_probe --add-binary "$tdlRoot/bin/tdl-core.exe;bin" --add-data "$tdlRoot/desktop/assets;assets" --add-data "$tdlRoot/dist/licenses;licenses" --workpath .build/pyinstaller --specpath .build "$tdlRoot/desktop/app.py"
        if ($LASTEXITCODE -ne 0) { throw 'Desktop packaging failed' }
    } finally {
        $env:PATH = $tdlBuildPath
    }
    & $Python desktop/verify_release.py --source 'dist/TDL下載助手.exe'
    if ($LASTEXITCODE -ne 0) { throw 'Standalone executable verification failed' }
    & $Python desktop/verify_restart_release.py --source 'dist/TDL下載助手.exe'
    if ($LASTEXITCODE -ne 0) { throw 'Executable restart verification failed' }
    Copy-Item -LiteralPath 'dist/TDL下載助手.exe' -Destination 'TDL下載助手.exe' -Force
    & $Python desktop/package_release.py
    if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed' }
    Get-FileHash -LiteralPath 'TDL下載助手.exe' -Algorithm SHA256
} finally {
    Pop-Location
}
