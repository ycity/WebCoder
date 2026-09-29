# WebCoder - agent installer for Windows (Windows PowerShell 5.1 or PowerShell 7).
# Creates a Python virtual environment in %USERPROFILE%\.webcoder-agent, installs the agent from the hub,
# pairs this computer, sets the folders the hub may access and (unless -NoAutostart) registers a task
# that starts the agent in the background at logon. No administrator rights are needed.
param(
    [Parameter(Mandatory = $true)][string]$Hub,
    [Parameter(Mandatory = $true)][string]$Code,
    [string[]]$Root = @(),
    [switch]$ReadOnly,
    [switch]$NoAutostart,
    [switch]$Mobile  # let the hub control phones attached here (adb / hdc)
)

# Native tools print progress and warnings on stderr, so check exit codes instead of stopping on stderr.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$Hub = $Hub.TrimEnd('/')
$AgentHome = Join-Path $env:USERPROFILE '.webcoder-agent'
$Venv = Join-Path $AgentHome 'venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$PythonW = Join-Path $Venv 'Scripts\pythonw.exe'
$Config = Join-Path $AgentHome 'config.json'  # passed explicitly: the logon task must not depend on the environment
$LogFile = Join-Path $AgentHome 'agent.log'
$TaskName = 'WebCoder Agent'

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }

# This runs inside the caller's PowerShell session: whatever is changed process-wide is restored at the end.
$savedEnv = @{}
foreach ($name in 'PYTHONHOME', 'PYTHONPATH', 'PYTHONIOENCODING') {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$savedEncoding = [Console]::OutputEncoding
$restartPrevious = $false
$restartManual = $false
try {
    # A PYTHONHOME/PYTHONPATH left by another tool makes Python load a foreign standard library, and
    # Python's output must be decoded the way it is encoded (non-ASCII folder names).
    Remove-Item Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
    $env:PYTHONIOENCODING = 'utf-8'
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

    Write-Step '查找 Python 3.10+'
    $BaseExe = $null
    $BaseArgs = @()
    foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
        if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
        $prefix = @()
        if ($candidate.Count -gt 1) { $prefix = @($candidate[1..($candidate.Count - 1)]) }
        $ok = & $candidate[0] @prefix -c 'import sys; print(int(sys.version_info >= (3, 10)))' 2>$null
        if ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq '1') { $BaseExe = $candidate[0]; $BaseArgs = $prefix; break }
    }
    if (-not $BaseExe) {
        throw '需要 Python 3.10 或更高版本：请从 https://www.python.org/downloads/ 安装（勾选 Add python.exe to PATH）后重新运行本命令。'
    }

    New-Item -ItemType Directory -Force -Path $AgentHome -ErrorAction Stop | Out-Null
    # Stop the running agent before upgrading (it holds files and the single-instance lock); if this
    # installation fails, the finally block starts it again.
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        $restartPrevious = $existing.State -eq 'Running'
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    # An agent started by hand from this folder would otherwise keep running next to the new one. If this
    # installation fails, the finally block starts one again in the background.
    $manual = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*-m webcoder.agent*' -and
                       ($_.CommandLine.Contains($Config) -or $_.CommandLine -notlike '*--config*') })
    $manual | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $restartManual = $manual.Count -gt 0 -and -not $restartPrevious

    if (-not (Test-Path $Python)) {
        Write-Step "创建独立的 Python 虚拟环境：$Venv"
        & $BaseExe @BaseArgs -m venv $Venv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Python)) { throw '创建 Python 虚拟环境失败。' }
    }

    Write-Step '安装 WebCoder Agent'
    & $Python -m pip install --disable-pip-version-check --quiet --upgrade "$Hub/downloads/webcoder-agent.zip"
    if ($LASTEXITCODE -ne 0) { throw 'pip 安装失败（见上方输出）。国内网络可先设置 PIP_INDEX_URL 使用镜像源。' }

    # `webcoder` (tasks for ChatGPT on the web, from a project folder) on the user's PATH: a copy of the launcher
    # pip made (it names the venv's Python by full path) in a folder of its own, so the venv's python.exe stays
    # off PATH. A running webcoder keeps the old copy, which works the same.
    $Launcher = Join-Path $Venv 'Scripts\webc.exe'
    if (Test-Path $Launcher) {
        $Bin = Join-Path $AgentHome 'bin'
        New-Item -ItemType Directory -Force -Path $Bin | Out-Null
        Copy-Item -Force $Launcher (Join-Path $Bin 'webc.exe') -ErrorAction SilentlyContinue
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not (@("$userPath" -split ';') -contains $Bin)) {
            Write-Step '把 webc 命令加入 PATH（新开的终端里生效）'
            [Environment]::SetEnvironmentVariable('Path', ((@("$userPath".TrimEnd(';'), $Bin) | Where-Object { $_ }) -join ';'), 'User')
        }
    }

    Write-Step '配对这台电脑'
    $paired = & $Python -m webcoder.agent --config $Config pair --hub $Hub --code $Code
    if ($LASTEXITCODE -ne 0) { throw '配对失败（见上方输出）。配对码 15 分钟内有效，可在面板重新生成。' }
    Write-Host ($paired | Select-Object -First 1)  # the rest are hints for manual setups; this script does those steps

    # Always applied, even when empty: reinstalling with fewer folders must take the others away.
    if ($Root.Count -gt 0) { Write-Step ('设置允许访问的文件夹：' + ($Root -join '、')) }
    else { Write-Step '不开放任何文件夹（之后可在面板「修改设置」里添加）' }
    & $Python -m webcoder.agent --config $Config roots set --create @Root | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '设置文件夹失败（见上方输出）。' }
    $readOnlyState = 'off'
    if ($ReadOnly) { $readOnlyState = 'on' }
    & $Python -m webcoder.agent --config $Config read-only $readOnlyState | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '设置只读失败（见上方输出）。' }
    $mobileState = 'off'
    if ($Mobile) { $mobileState = 'on'; Write-Step '允许控制连接到这台电脑的手机/模拟器（adb / hdc）' }
    & $Python -m webcoder.agent --config $Config mobile $mobileState | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '设置手机控制失败（见上方输出）。' }

    if ($NoAutostart) {
        $restartPrevious = $false
        $restartManual = $false
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue  # from an earlier install
        Write-Host ''
        Write-Host "安装完成。需要时用这条命令启动 Agent：& '$Python' -m webcoder.agent --config '$Config' run" -ForegroundColor Green
        return
    }

    Write-Step "设置开机自动运行（计划任务：$TaskName）"
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $PythonW `
        -Argument "-m webcoder.agent --config `"$Config`" run --log-file `"$LogFile`"" -WorkingDirectory $AgentHome
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'WebCoder agent' -Force -ErrorAction Stop | Out-Null
    $logStart = 0
    if (Test-Path $LogFile) { $logStart = (Get-Item $LogFile).Length }  # only look at what the new run writes
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $restartPrevious = $false
    $restartManual = $false

    $connected = $false
    for ($i = 0; $i -lt 20 -and -not $connected; $i++) {
        Start-Sleep -Seconds 1
        if (-not (Test-Path $LogFile)) { continue }
        try {
            # The running agent keeps the log open, so share read/write access while reading it.
            $stream = [System.IO.File]::Open($LogFile, 'Open', 'Read', 'ReadWrite')
            try {
                [void]$stream.Seek([Math]::Min($logStart, $stream.Length), 'Begin')
                $connected = (New-Object System.IO.StreamReader($stream)).ReadToEnd() -match 'connected to'
            } finally { $stream.Dispose() }
        } catch { }
    }
    Write-Host ''
    if ($connected) {
        Write-Host "完成：Agent 已在后台运行并连上 Hub，以后登录 Windows 时会自动启动。日志：$LogFile" -ForegroundColor Green
    } else {
        Write-Host "Agent 已在后台启动，但暂未确认连接成功，请查看日志：$LogFile" -ForegroundColor Yellow
    }
} finally {
    if ($restartPrevious) {  # the installation failed: bring the previous agent back
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    } elseif ($restartManual -and (Test-Path $PythonW)) {  # it was started by hand: start it the standard way
        Start-Process -FilePath $PythonW -WorkingDirectory $AgentHome -WindowStyle Hidden -ErrorAction SilentlyContinue `
            -ArgumentList "-m webcoder.agent --config `"$Config`" run --log-file `"$LogFile`""
    }
    foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process') }
    try { [Console]::OutputEncoding = $savedEncoding } catch { }
}
