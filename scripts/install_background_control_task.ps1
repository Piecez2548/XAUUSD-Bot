param(
    [string]$TaskName = "XAUUSD Bot Background",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "$((Split-Path -Parent $PSScriptRoot))\.venv\Scripts\pythonw.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python runtime not found: $PythonPath"
}

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
$mainPath = Join-Path $resolvedRoot "main.py"

# Register the accepted task name in place.  This script deliberately does not
# invent a second Control task or start/stop the currently running runtime.
$action = New-ScheduledTaskAction `
    -Execute $resolvedPython `
    -Argument "`"$mainPath`" control" `
    -WorkingDirectory $resolvedRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Write-Host "Updated $TaskName with hidden single-Control battery-safe settings."
Write-Host "The task was not started by this script. Verify with: schtasks /Query /TN `"$TaskName`" /V /FO LIST"
