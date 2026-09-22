param([string]$TaskName = "XAUUSD-AI-Trader-Telegram-Control")
$ErrorActionPreference = "Stop"
schtasks.exe /Delete /F /TN $TaskName
Write-Host "Removed $TaskName."
