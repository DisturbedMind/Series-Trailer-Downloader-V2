param(
    [string]$Python = "python",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$scriptPath = Join-Path $projectRoot "series_trailer_downloader.py"
$iconPath = Join-Path $projectRoot "assets\wolf.ico"
$bannerPath = Join-Path $projectRoot "assets\wolf-banner.png"
$versionPath = Join-Path $projectRoot "packaging\version_info.txt"
$buildPath = Join-Path $projectRoot "build"
$distPath = Join-Path $projectRoot "dist"
$releasePath = Join-Path $distPath "Series-Trailer-Downloader-V2.0"
$zipPath = Join-Path $distPath "Series-Trailer-Downloader-V2.0-Windows-x64.zip"
$exeName = "Series Trailer Downloader.exe"
$builtExe = Join-Path $distPath $exeName
$releaseExe = Join-Path $releasePath $exeName

function Invoke-Checked {
    param([string]$Label, [string[]]$Arguments)
    Write-Host "`n== $Label ==" -ForegroundColor Cyan
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Assert-ProjectChild {
    param([string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $resolved"
    }
}

foreach ($required in @($scriptPath, $iconPath, $bannerPath, $versionPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required build input is missing: $required"
    }
}

Invoke-Checked "Checking PyInstaller" @("-m", "PyInstaller", "--version")

if (-not $SkipTests) {
    Invoke-Checked "Compiling Python source" @("-m", "py_compile", $scriptPath)
    Invoke-Checked "Running tests" @("-m", "unittest", "discover", "-s", (Join-Path $projectRoot "tests"), "-v")
}

Assert-ProjectChild $buildPath
Assert-ProjectChild $releasePath
Assert-ProjectChild $zipPath

if (Test-Path -LiteralPath $releasePath) {
    Remove-Item -LiteralPath $releasePath -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

$addBanner = "$bannerPath;assets"
$addIcon = "$iconPath;assets"
$pyInstallerArguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "Series Trailer Downloader",
    "--icon", $iconPath,
    "--version-file", $versionPath,
    "--add-data", $addBanner,
    "--add-data", $addIcon,
    "--collect-all", "yt_dlp",
    "--collect-all", "curl_cffi",
    "--hidden-import", "tkinter",
    "--distpath", $distPath,
    "--workpath", $buildPath,
    "--specpath", $buildPath,
    $scriptPath
)
Invoke-Checked "Building Windows executable" $pyInstallerArguments

if (-not (Test-Path -LiteralPath $builtExe -PathType Leaf)) {
    throw "PyInstaller completed but the executable was not found: $builtExe"
}

New-Item -ItemType Directory -Path $releasePath -Force | Out-Null
Move-Item -LiteralPath $builtExe -Destination $releaseExe -Force

$supportFiles = @(
    ".gitignore",
    "install.ps1",
    "LICENSE",
    "README.md",
    "requirements.txt",
    "series_trailer_downloader.py",
    "series_trailer_downloader.settings.example.json"
)
foreach ($relative in $supportFiles) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $relative) -Destination $releasePath -Force
}

Compress-Archive -LiteralPath $releasePath -DestinationPath $zipPath -CompressionLevel Optimal
$hash = Get-FileHash -LiteralPath $releaseExe -Algorithm SHA256

Write-Host "`nBuild complete." -ForegroundColor Green
Write-Host "EXE: $releaseExe"
Write-Host "ZIP: $zipPath"
Write-Host "SHA256: $($hash.Hash)"
