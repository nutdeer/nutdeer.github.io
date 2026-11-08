$ErrorActionPreference = 'Stop'
try {
    $scriptPath = Join-Path $PSScriptRoot 'notes_logger_v3.py'
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw 'Keep this launcher in the same folder as notes_logger_v3.py.'
    }
    $pythonPath = $null
    foreach ($name in @('py', 'python', 'python3')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $prefix = @()
        if ($name -eq 'py') { $prefix = @('-3') }
        try {
            $result = & $command.Source @prefix -c 'import sys, tkinter; assert sys.version_info >= (3, 8); print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $result) {
                $pythonPath = ([string]($result | Select-Object -Last 1)).Trim()
                break
            }
        } catch { continue }
    }
    if (-not $pythonPath) {
        throw 'Install Python 3.8 or newer with Tcl/Tk support, then double-click start_notes_v3.cmd again.'
    }
    $pythonw = Join-Path (Split-Path $pythonPath) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $pythonPath }
    $logDir = Join-Path $env:LOCALAPPDATA 'Nutdeer'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $scriptPath + '"') -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "$stamp-out.log") -RedirectStandardError (Join-Path $logDir "$stamp-error.log") | Out-Null
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'Nutdeer Notes v3') | Out-Null
    exit 1
}
