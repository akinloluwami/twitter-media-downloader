$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appName = "X Media Downloader"
$buildVenvDir = Join-Path $scriptDir ".build-venv-windows"
$buildWorkDir = Join-Path $scriptDir "build-windows"
$distDir = Join-Path $scriptDir "dist"
$distAppDir = Join-Path $distDir $appName
$iconIcoPath = Join-Path $scriptDir "logo.ico"
$iconPngPath = Join-Path $scriptDir "logo.png"

function Test-TkPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Exe,
        [string[]]$Args = @()
    )

    $probe = 'import tkinter as tk; root = tk.Tk(); root.withdraw(); print(root.tk.eval("info patchlevel")); root.destroy()'
    & $Exe @Args -c $probe *> $null
    return $LASTEXITCODE -eq 0
}

function Resolve-BuildPython {
    if ($env:PYTHON_BIN) {
        if (-not (Test-Path $env:PYTHON_BIN)) {
            throw "PYTHON_BIN is set but not executable: $($env:PYTHON_BIN)"
        }
        return @{
            Exe = $env:PYTHON_BIN
            Args = @()
            Display = $env:PYTHON_BIN
        }
    }

    $candidates = @(
        @{ Exe = "py"; Args = @("-3.12"); Display = "py -3.12" }
        @{ Exe = "py"; Args = @("-3"); Display = "py -3" }
        @{ Exe = "python"; Args = @(); Display = "python" }
        @{ Exe = "python3"; Args = @(); Display = "python3" }
    )

    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) {
            continue
        }
        if (Test-TkPython -Exe $candidate.Exe -Args $candidate.Args) {
            return $candidate
        }
    }

    throw "No usable Python with tkinter support was found. Install Python plus tkinter, or set PYTHON_BIN."
}

function Remove-IfExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PathValue
    )

    if (Test-Path $PathValue) {
        Remove-Item -Recurse -Force $PathValue
    }
}

$buildPython = Resolve-BuildPython
Write-Host "Using build Python: $($buildPython.Display)"

Remove-IfExists -PathValue $buildVenvDir
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

& $buildPython.Exe @($buildPython.Args + @("-m", "venv", $buildVenvDir))

$buildVenvPython = Join-Path $buildVenvDir "Scripts\python.exe"
& $buildVenvPython -m pip install --upgrade pip
& $buildVenvPython -m pip install --upgrade pyinstaller pillow

$iconPath = $null
if (Test-Path $iconIcoPath) {
    $iconPath = $iconIcoPath
} elseif (Test-Path $iconPngPath) {
    $iconPath = $iconPngPath
}

$pyinstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", $appName,
    "--distpath", $distDir,
    "--workpath", $buildWorkDir,
    "--specpath", $scriptDir
)

if ($iconPath) {
    $pyinstallerArgs += @("--icon", $iconPath)
}

$pyinstallerArgs += (Join-Path $scriptDir "download_x_media_gui.py")

& $buildVenvPython @pyinstallerArgs

$toolchainDir = Join-Path $distAppDir "_toolchain"
Remove-IfExists -PathValue $toolchainDir
& $buildVenvPython -m venv $toolchainDir

$toolchainPython = Join-Path $toolchainDir "Scripts\python.exe"
& $toolchainPython -m pip install --upgrade pip
& $toolchainPython -m pip install --upgrade gallery-dl yt-dlp

Write-Host ""
Write-Host "Built Windows app folder:"
Write-Host "  $distAppDir"
Write-Host ""
Write-Host "Main executable:"
Write-Host "  $(Join-Path $distAppDir 'X Media Downloader.exe')"
Write-Host ""
Write-Host "Bundled downloader toolchain:"
Write-Host "  $toolchainDir"
