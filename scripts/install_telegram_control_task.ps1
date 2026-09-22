param(
    [string]$TaskName = "XAUUSD-AI-Trader-Telegram-Control",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "$((Split-Path -Parent $PSScriptRoot))\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python runtime not found: $PythonPath"
}
$taskRun = "`"$PythonPath`" `"$ProjectRoot\main.py`" control"
schtasks.exe /Create /F /SC ONLOGON /TN $TaskName /TR $taskRun /RL LIMITED
Write-Host "Installed $TaskName. It starts only the Telegram Control Service."
Write-Host "Monitoring remains stopped until an authorized /start command is received."
