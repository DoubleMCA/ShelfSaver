<#
.SYNOPSIS
  Installs, upgrades or removes ShelfSaver on this PC. Run as administrator.

.DESCRIPTION
  Install / upgrade (same command): stops ShelfSaver if it is running, replaces the program
  folder, starts it again. Your settings, history and logs in the data folder are not touched.

  -Uninstall   Stops ShelfSaver and removes its scheduled task, firewall rule and program
               folder. The data folder is kept unless -RemoveData is also given.
  -RemoveData  With -Uninstall only: also delete the data folder (settings, history, login).

  Your video libraries, holding folders and scratch disks are never touched.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$RemoveData
)

$ErrorActionPreference = 'Stop'

# --- Administrator check comes first: nothing else runs without it. ---------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ''
    Write-Host 'ShelfSaver setup needs to run as administrator.' -ForegroundColor Red
    Write-Host 'Close this window, right-click PowerShell, choose "Run as administrator",'
    Write-Host 'then run the same command again.'
    exit 1
}

$TaskName = 'ShelfSaver'
$RuleName = 'ShelfSaver'
$Port = 8642
$AppDir = Join-Path $env:ProgramFiles 'ShelfSaver'
$DataDir = Join-Path $env:ProgramData 'ShelfSaver'
$VenvPython = Join-Path $AppDir 'venv\Scripts\python.exe'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Say($text) { Write-Host $text }
function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Warn($text) { Write-Host "NOTE: $text" -ForegroundColor Yellow }

function Fail($text) { throw $text }

function Invoke-Native {
    # Runs a program and fails with a plain message when it returns an error.
    param([string]$What, [string]$Exe, [string[]]$ArgList)
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) { Fail "$What did not finish (exit code $LASTEXITCODE)." }
}

# --- Stop / remove helpers ---------------------------------------------------------------------
function Stop-ShelfSaver {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
    # The app runs as "python -m shelfsaver"; end any copy that is still going.
    $procs = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match '-m\s+shelfsaver' }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 30; $i++) {
        $left = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match '-m\s+shelfsaver' }
        if (-not $left) { return }
        Start-Sleep -Seconds 1
    }
    Fail 'ShelfSaver would not stop. Restart the PC and run this again.'
}

function Remove-Directory($path) {
    # Retry briefly: a just-stopped program can hold files for a moment.
    for ($i = 0; $i -lt 10; $i++) {
        if (-not (Test-Path -LiteralPath $path)) { return }
        try { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop; return }
        catch { Start-Sleep -Seconds 1 }
    }
    if (Test-Path -LiteralPath $path) { Fail "Could not remove the folder $path. Close anything using it and try again." }
}

# --- Uninstall ---------------------------------------------------------------------------------
if ($Uninstall) {
    Step 'Removing ShelfSaver'
    Stop-ShelfSaver
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Say 'Removed the startup task.'
    }
    if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $RuleName
        Say 'Removed the firewall rule.'
    }
    Remove-Directory $AppDir
    Say "Removed the program folder ($AppDir)."
    if ($RemoveData) {
        Remove-Directory $DataDir
        Say "Removed the data folder ($DataDir), including settings and history."
    } else {
        Say ''
        Say "Your settings, history and login were KEPT in $DataDir."
        Say 'To delete them as well, run the uninstall again with -RemoveData.'
    }
    Say 'Your video libraries, holding folders and scratch disks were not touched.'
    exit 0
}

if ($RemoveData) { Fail '-RemoveData only works together with -Uninstall.' }

# --- Install / upgrade -------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'shelfsaver\__main__.py'))) {
    Fail "Can't find the ShelfSaver files next to this script. Run it from the downloaded ShelfSaver folder."
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Test-SystemRuns {
    # Really runs the program as SYSTEM (what the startup task will use) and checks it worked.
    param([string]$Exe, [string]$ExeArgs)
    $checkDir = Join-Path $DataDir 'check'
    New-Item -ItemType Directory -Force -Path $checkDir | Out-Null
    $out = Join-Path $checkDir 'out.txt'
    Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
    $name = 'ShelfSaver-Check'
    $cmdArgs = '/c ""{0}" {1} > "{2}" 2>&1 && echo SHELFSAVER_OK>> "{2}""' -f $Exe, $ExeArgs, $out
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmdArgs
    $who = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    try {
        Register-ScheduledTask -TaskName $name -Action $action -Principal $who -Force | Out-Null
        Start-ScheduledTask -TaskName $name
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            if ((Get-ScheduledTask -TaskName $name).State -ne 'Running' -and (Test-Path -LiteralPath $out)) { break }
        }
        $text = if (Test-Path -LiteralPath $out) { Get-Content -LiteralPath $out -Raw } else { '' }
        return [bool]($text -match 'SHELFSAVER_OK')
    } finally {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $checkDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-RealPath([string]$path) {
    # A WinGet "link" is a shortcut SYSTEM can't see; follow it to the real file.
    $item = Get-Item -LiteralPath $path
    if ($item.LinkType -and $item.Target) {
        $target = @($item.Target)[0]
        if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path $item.DirectoryName $target }
        return [IO.Path]::GetFullPath($target)
    }
    return $item.FullName
}

# Python ---------------------------------------------------------------------------------------
function Find-Python {
    $found = @()
    Get-ChildItem -Path (Join-Path $env:ProgramFiles 'Python3*') -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | ForEach-Object { $found += (Join-Path $_.FullName 'python.exe') }
    Get-Command python.exe -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -and $_.Source -notmatch '\\WindowsApps\\' } |
        ForEach-Object { $found += $_.Source }
    foreach ($candidate in $found) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $ok = & $candidate -c 'import sys; print(sys.version_info >= (3, 10))' 2>$null
        if ($ok -eq 'True') { return (Resolve-RealPath $candidate) }
    }
    return $null
}

function Install-WithWinget([string]$id, [string]$label) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        Fail "$label is needed but Windows' app installer (winget) isn't available. Install $label yourself, then run this again."
    }
    Say "Installing $label for all users..."
    $common = @('install', '--id', $id, '-e', '--silent',
                '--accept-package-agreements', '--accept-source-agreements')
    & winget.exe @common '--scope' 'machine'
    if ($LASTEXITCODE -ne 0) {
        Warn "$label could not be installed for all users. Trying again for this account only."
        & winget.exe @common
    }
    Update-SessionPath
}

Step 'Checking Python'
$Python = Find-Python
if (-not $Python) {
    Install-WithWinget 'Python.Python.3.12' 'Python'
    $Python = Find-Python
}
if (-not $Python) { Fail 'Python could not be found or installed. Install Python 3.10 or newer from python.org, then run this again.' }
Say "Using Python at $Python"
if (-not (Test-SystemRuns $Python '--version')) {
    Fail "Python at $Python can't be used by the background service (it lives in a personal folder). Install Python for all users from python.org and run this again."
}

# Data folder, locked down before anything private is written to it ---------------------------
Step 'Preparing the data folder'
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
# SYSTEM and Administrators only: the folder holds the login and access tokens.
Invoke-Native 'Locking the data folder' icacls.exe @($DataDir, '/inheritance:r', '/grant:r',
    '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
$ErrorActionPreference = 'Continue'
& icacls.exe "$DataDir\*" /inheritance:e /T /C /Q 2>&1 | Out-Null
$ErrorActionPreference = 'Stop'
Say "Only administrators can open $DataDir"

# Replace the program folder ------------------------------------------------------------------
Step 'Stopping ShelfSaver (if running) and replacing the program'
Stop-ShelfSaver
Remove-Directory $AppDir
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null

$staging = Join-Path ([IO.Path]::GetTempPath()) ('shelfsaver-setup-' + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'shelfsaver') -Destination $staging -Recurse
    foreach ($file in 'pyproject.toml', 'LICENSE', 'README.md') {
        $src = Join-Path $RepoRoot $file
        if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $staging }
    }
    Get-ChildItem -LiteralPath $staging -Recurse -Directory -Filter '__pycache__' |
        Remove-Item -Recurse -Force
    Say 'Setting up (this downloads a few packages and can take a minute)...'
    Invoke-Native 'Creating the Python environment' $Python @('-m', 'venv', (Join-Path $AppDir 'venv'))
    Invoke-Native 'Installing ShelfSaver' $VenvPython @('-m', 'pip', 'install', '--quiet',
        '--disable-pip-version-check', '--no-warn-script-location', $staging)
} finally {
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
}
if (-not (Test-SystemRuns $VenvPython '--version')) {
    Fail 'The background service cannot run the installed program. Install Python for all users and run this again.'
}

# FFmpeg ----------------------------------------------------------------------------------------
$ConfigPath = Join-Path $DataDir 'config.json'
$config = New-Object PSObject
if (Test-Path -LiteralPath $ConfigPath) {
    try {
        $existing = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
        if ($existing -is [PSCustomObject]) { $config = $existing }
    } catch { Warn 'The old config.json could not be read, so it is being replaced.' }
}

function Find-Tool([string]$name) {
    # Order: what config.json already says, then PATH, then WinGet's own folders.
    $candidates = @()
    $configured = $config.PSObject.Properties[$name]
    if ($configured -and $configured.Value) { $candidates += [string]$configured.Value }
    Get-Command "$name.exe" -All -ErrorAction SilentlyContinue | ForEach-Object { $candidates += $_.Source }
    $roots = @((Join-Path $env:ProgramFiles 'WinGet\Packages'),
               (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'))
    foreach ($pkgs in $roots) {
        if (Test-Path -LiteralPath $pkgs) {
            Get-ChildItem -Path $pkgs -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue |
                ForEach-Object { $candidates += $_.FullName }
        }
    }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-RealPath $c) }
    }
    return $null
}

function Get-UsableTool([string]$name) {
    $path = Find-Tool $name
    if (-not $path) { return $null }
    if (Test-SystemRuns $path '-version') { return $path }
    # Installed for one user only. These are single-file programs, so give ShelfSaver its own copy.
    $bin = Join-Path $AppDir 'bin'
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $copy = Join-Path $bin "$name.exe"
    Copy-Item -LiteralPath $path -Destination $copy -Force
    if (Test-SystemRuns $copy '-version') {
        Warn "$name was only installed for one user, so a copy was placed in $bin."
        return $copy
    }
    return $null
}

Step 'Checking FFmpeg'
$ffmpeg = $null
if (-not (Find-Tool 'ffmpeg') -or -not (Find-Tool 'ffprobe')) {
    Install-WithWinget 'Gyan.FFmpeg' 'FFmpeg'
}
$ffmpeg = Get-UsableTool 'ffmpeg'
$ffprobe = Get-UsableTool 'ffprobe'
if (-not $ffmpeg -or -not $ffprobe) {
    Fail "FFmpeg could not be found in a place the background service can use. Install FFmpeg for all users, then run this again."
}
Say "Using $ffmpeg"
Say "Using $ffprobe"

$config | Add-Member -NotePropertyName ffmpeg -NotePropertyValue $ffmpeg -Force
$config | Add-Member -NotePropertyName ffprobe -NotePropertyValue $ffprobe -Force
[IO.File]::WriteAllText($ConfigPath, ($config | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
Say "Saved settings to $ConfigPath"

# Startup task ----------------------------------------------------------------------------------
Step 'Setting ShelfSaver to start with the PC'
# Quotes keep "C:\Program Files" in one piece.
$action = New-ScheduledTaskAction -Execute ('"{0}"' -f $VenvPython) -Argument '-m shelfsaver' -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$who = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
# If it stops unexpectedly (or is refused while a previous copy is still closing), try again in a minute.
$settings = New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $who `
    -Settings $settings -Description 'ShelfSaver: converts video libraries to save space.' -Force | Out-Null
Say 'Done.'

# Firewall --------------------------------------------------------------------------------------
Step 'Allowing access from your home network'
if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -DisplayName $RuleName
}
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $Port -Profile Private | Out-Null
Say "Port $Port is open on Private networks only."

# Start -----------------------------------------------------------------------------------------
Step 'Starting ShelfSaver'
Start-ScheduledTask -TaskName $TaskName
$up = $false
for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    Start-Sleep -Seconds 1
    $client = New-Object Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', $Port); $up = $true } catch { } finally { $client.Close() }
}

$ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -in 'Dhcp', 'Manual' } |
    Select-Object -First 1 -ExpandProperty IPAddress

Write-Host ''
if ($up) {
    Write-Host 'ShelfSaver is installed and running.' -ForegroundColor Green
} else {
    Warn "ShelfSaver was installed but hasn't answered yet. Give it a minute and try the address below."
    Warn 'If it still does not open, restart the PC. It starts by itself at startup.'
}
Say 'Open ShelfSaver in a web browser:'
Say "  On this PC:        http://localhost:$Port"
if ($ip) { Say "  From another device: http://${ip}:$Port" }
Say ''
Say "If the other device can't connect, make sure this PC's network is set to 'Private' in Windows settings."
Say "Your settings and history live in $DataDir and are kept when you update or uninstall."
