# Hooky Main All-in-One Runner & Setup (Windows)
# Usage:
#   .\main.ps1          -> Auto-setup if needed, launch Web Studio, open browser
#   .\main.ps1 web      -> Launch Web Studio (http://127.0.0.1:8000)
#   .\main.ps1 make     -> Run autonomous batch pipeline
#   .\main.ps1 doctor   -> Run system health checks
#   .\main.ps1 <cmd>    -> Pass any command to Hooky CLI

param(
    [switch] $SkipFfmpeg,
    [switch] $SkipSetup,
    [switch] $ConfigureEnv,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CommandArgs = @()
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location -LiteralPath $Root

function Invoke-Checked {
    param([string] $Command, [string[]] $Arguments = @())
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Command $($Arguments -join ' ')"
    }
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($machine -or $user) {
        $parts = @($machine, $user) | Where-Object { $_ }
        $env:Path = $parts -join ";"
    }
}

# 1. Verify FFmpeg & FFprobe
if (-not $SkipFfmpeg -and (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
        -not (Get-Command ffprobe -ErrorAction SilentlyContinue))) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "[hooky] Installing FFmpeg via winget..." -ForegroundColor Cyan
        Invoke-Checked "winget" @("install", "--id", "Gyan.FFmpeg.Shared", "-e", "--accept-source-agreements", "--accept-package-agreements")
        Refresh-Path
    } else {
        throw "FFmpeg/FFprobe are missing. Install FFmpeg or install winget, then rerun main.ps1."
    }
}

# 2. Bootstrap uv and dependencies only when the virtual environment is missing
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $uvCommand = $null
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $uv) {
        $uvCommand = $uv.Source
    } else {
        $candidate = Join-Path $HOME ".local\bin\uv.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $uvCommand = $candidate
        } else {
            Write-Host "[hooky] Installing uv package manager..." -ForegroundColor Cyan
            irm https://astral.sh/uv/install.ps1 | iex
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $uvCommand = $candidate
            } else {
                $uv = Get-Command uv -ErrorAction SilentlyContinue
                if ($null -ne $uv) { $uvCommand = $uv.Source }
            }
        }
    }
    Write-Host "[hooky] Setting up Python virtual environment and dependencies..." -ForegroundColor Cyan
    Invoke-Checked $uvCommand @("sync", "--extra", "all")
}

# 3. Check .env and prompt for environment configuration choices
$EnvFile = Join-Path $Root ".env"
$EnvExample = Join-Path $Root ".env.example"
$isInteractive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

if ($ConfigureEnv) {
    Write-Host "`n[hooky] Launching interactive setup wizard..." -ForegroundColor Cyan
    & $Python -m hooky setup --redo
} elseif (-not (Test-Path -LiteralPath $EnvFile)) {
    if (Test-Path -LiteralPath $EnvExample) {
        Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
        Write-Host "[hooky] Created .env configuration from template." -ForegroundColor Green
    }
    if ($isInteractive -and -not $SkipSetup) {
        Write-Host "`n[hooky] First-time setup detected." -ForegroundColor Cyan
        $answer = Read-Host "[hooky] Would you like to configure your API keys and environment settings now? (Y/n)"
        if ($answer -ne 'n' -and $answer -ne 'N') {
            & $Python -m hooky setup
        }
    }
} elseif ($isInteractive -and -not $SkipSetup) {
    $isLaunching = ($CommandArgs.Count -eq 0) -or ($CommandArgs[0] -in @("web", "make", "parts", "post", "ui", "portal", "serve"))
    if ($isLaunching) {
        $answer = Read-Host "[hooky] Would you like to review or update your .env settings before launch? (y/N)"
        if ($answer -eq 'y' -or $answer -eq 'Y' -or $answer -eq 'yes') {
            & $Python -m hooky setup --redo
        }
    }
}

$env:HOOKY_LAUNCHER_PROMPTED = "1"

# 4. Determine command: if no arguments provided, default to launching Web Studio ('web')
[string[]]$RunArgs = if ($CommandArgs.Count -eq 0) { @("web") } else { $CommandArgs }

Write-Host "`n[hooky] Launching Hooky ($($RunArgs -join ' '))...`n" -ForegroundColor Green
& $Python -m hooky @RunArgs
