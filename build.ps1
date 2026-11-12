param(
    [switch]$SkipTests,
    [switch]$DesktopShortcut
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$tmp = Join-Path $root ".tmp"
New-Item -ItemType Directory -Force $tmp | Out-Null
$env:TEMP = $tmp
$env:TMP = $tmp

Write-Host "== EFOS build ==" -ForegroundColor Cyan
if (-not $SkipTests) {
    Write-Host "Running test suite..."
    $env:EFOS_APP_DATA = Join-Path $root ".efos-data"
    $env:QT_QPA_PLATFORM = "offscreen"
    $env:QT_QPA_FONTDIR = "C:\Windows\Fonts"
    python -m pytest -q --disable-warnings -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; packaging stopped." }
}

$release = Join-Path $root "release"
$work = Join-Path $root "build\pyinstaller"
$specDir = Join-Path $root "build"
New-Item -ItemType Directory -Force $release | Out-Null
Get-ChildItem $release -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "EFOS",
    "--icon", (Join-Path $root "assets\efos.ico"),
    "--version-file", (Join-Path $root "assets\version_info.txt"),
    "--add-data", "$root\efos_app\sql;efos_app\sql",
    "--distpath", $release,
    "--workpath", $work,
    "--specpath", $specDir,
    "--paths", $root,
    (Join-Path $root "main.py")
)
Write-Host "Packaging one-file Windows executable..."
python @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$exe = Join-Path $release "EFOS.exe"
if (-not (Test-Path $exe)) { throw "Expected executable was not produced: $exe" }

$readme = @"
EFOS - Executive Function Optimization System
=============================================

Run: double-click EFOS.exe

Configuration
-------------
EFOS loads its .env profile from:
  1. %EFOS_ENV_FILE%
  2. the executable folder
  3. any parent of the executable (up to five levels)
  4. the current working directory and its parents
  5. %APPDATA%\EFOS\.env

The workspace .env is therefore found when this executable stays in the
project's release\ folder. Keep the .env file out of shared distributions.

Database
--------
The canonical EFOS schema is created automatically the first time the selected
database is empty. A partially-created EFOS schema is refused rather than
modified. First launch may require one-time administrative credentials; daily
runtime can use a separate low-privilege application role.

Google Calendar
---------------
Google Calendar synchronization uses read-only OAuth 2.0. Set
EFOS_GOOGLE_CLIENT_ID, EFOS_GOOGLE_CLIENT_SECRET, and EFOS_GOOGLE_CALENDAR_ID.
Access and refresh tokens are stored in the Windows Credential Manager, never in
plaintext files or PostgreSQL. API-key access has been removed.

"@
Set-Content -Path (Join-Path $release "README.txt") -Value $readme -Encoding UTF8

$shell = New-Object -ComObject WScript.Shell
function New-EfosShortcut([string]$Path) {
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $exe
    $link.WorkingDirectory = $root
    $link.IconLocation = "$exe,0"
    $link.Description = "EFOS - Executive Function Optimization System"
    $link.Save()
    Write-Host "Shortcut: $Path"
}
New-EfosShortcut (Join-Path $root "EFOS.lnk")
if ($DesktopShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if ($desktop) { New-EfosShortcut (Join-Path $desktop "EFOS.lnk") }
}

Write-Host "Built: $exe" -ForegroundColor Green
Write-Host "Size:  $([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB"
