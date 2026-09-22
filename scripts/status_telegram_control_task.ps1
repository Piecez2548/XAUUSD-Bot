param([string]$TaskName = "XAUUSD-AI-Trader-Telegram-Control")
schtasks.exe /Query /TN $TaskName /V /FO LIST
