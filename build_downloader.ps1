[CmdletBinding()]
param([string]$Python = "python")

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Push-Location $root
try {
    & $Python -m PyInstaller --clean --noconfirm --onefile --windowed `
        --name DanceVideoDownloader `
        --icon "$root\assets\app-icon.ico" `
        --add-data "$root\assets\app-icon.png;assets" `
        --distpath "$root\dist-downloader" `
        --workpath "$root\build\downloader" `
        --specpath "$root\build\downloader" `
        "$root\dance_downloader_gui.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Downloader build failed with exit code $LASTEXITCODE"
    }
    Write-Host "Downloader created: $root\dist-downloader\DanceVideoDownloader.exe"
}
finally {
    Pop-Location
}
