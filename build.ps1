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

# Use the project virtual environment for every Python step (tests, icon
# generation, PyInstaller) so the packaged dependency set matches what the
# test suite just exercised. Fall back to PATH only when .venv is absent.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "== EFOS build ==" -ForegroundColor Cyan
if (-not $SkipTests) {
    Write-Host "Running test suite..."
    $env:EFOS_APP_DATA = Join-Path $root ".efos-data"
    $env:QT_QPA_PLATFORM = "offscreen"
    $env:QT_QPA_FONTDIR = "C:\Windows\Fonts"
    & $py -m pytest -q --disable-warnings -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; packaging stopped." }
}

$release = Join-Path $root "release"
$staging = Join-Path $root "build\release-staging"
$work = Join-Path $root "build\pyinstaller"
$specDir = Join-Path $root "build"
New-Item -ItemType Directory -Force $release | Out-Null
New-Item -ItemType Directory -Force $staging | Out-Null
Get-ChildItem $staging -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$icon = Join-Path $root "assets\efos.ico"
if (-not (Test-Path $icon)) {
    $icon = Join-Path $root "build\efos.ico"
    if (-not (Test-Path $icon)) {
        Write-Host "Generating the EFOS icon from brand artwork..."
        & $py (Join-Path $root "scripts\make_icon.py") $icon
        if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }
    }
    if (-not (Test-Path $icon)) { throw "Expected icon was not produced: $icon" }
}

$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "EFOS",
    "--icon", $icon,
    "--version-file", (Join-Path $root "assets\version_info.txt"),
    "--add-data", "$root\efos_app\sql;efos_app\sql",
    # Bundle certifi's CA store so HTTPS/OAuth token exchange works inside the
    # one-file executable regardless of the host certificate store.
    "--collect-data", "certifi",
    "--distpath", $staging,
    "--workpath", $work,
    "--specpath", $specDir,
    "--paths", $root,
    (Join-Path $root "main.py")
)
Write-Host "Packaging one-file Windows executable..."
& $py @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$exe = Join-Path $staging "EFOS.exe"
if (-not (Test-Path $exe)) { throw "Expected executable was not produced: $exe" }

# Verify the staged binary against the live database before touching release\.
Write-Host "Verifying staged binary self-test..."
$selftest = Join-Path $tmp "selftest_binary.json"
if (Test-Path $selftest) { Remove-Item $selftest -Force }
$env:EFOS_SELFTEST_OUT = $selftest
$process = Start-Process -FilePath $exe -ArgumentList "--self-test" -Wait -PassThru
$selftestExit = $process.ExitCode
if ($selftestExit -ne 0) {
    throw "Staged binary self-test failed with exit code $selftestExit. Existing release\EFOS.exe was not modified."
}
if (-not (Test-Path $selftest)) {
    throw "Staged binary did not write its self-test report. Existing release\EFOS.exe was not modified."
}
$report = Get-Content $selftest -Raw | ConvertFrom-Json
if (-not $report.ok) {
    throw "Staged binary self-test reported failure: $($report.error). Existing release\EFOS.exe was not modified."
}
Write-Host "Staged binary connected to $($report.connection) with $($report.counts.tasks) tasks."

# Preserve the previous verified binary outside release\, then publish the new one.
$targetExe = Join-Path $release "EFOS.exe"
if (Test-Path $targetExe) {
    Copy-Item $targetExe (Join-Path $tmp "EFOS.previous.exe") -Force
}
Copy-Item $exe $targetExe -Force
$exe = $targetExe

$readme = @"
EFOS - Executive Function Optimization System
=============================================
Version 2.1.0

Run: double-click EFOS.exe

Status and availability
-----------------------
Task Status is Ready, Blocked, Completed, or Deferred. It is derived from
conjunctive preconditions and prerequisites.

Temporal availability is separate: every new task defaults to 24/7, and an
optional availability dialog can restrict a task to selected weekdays and time
windows (including multiple windows per day). Availability never changes task
status, completion, project resolution, scheduling, or recurrence.

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
The canonical EFOS 2.1.0 schema is created automatically the first time the
selected database is empty. An existing EFOS 2.0.0 database is upgraded
additively on startup: public.task_availability_windows is created and
schema_meta is bumped to 2.1.0 without dropping or recreating existing tables.
A partially-created or mismatched EFOS schema is refused rather than modified.
First launch may require one-time administrative credentials; daily runtime can
use a separate low-privilege application role.

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
