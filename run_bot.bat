@echo off
title Telegram Sales Bot 24/7
cd /d "%~dp0"
echo Starting bot in 24/7 auto-restart loop...
:loop
d:\AMTCE\venv\Scripts\python.exe run_bot.py
echo Bot crashed or stopped. Restarting in 5 seconds...
timeout /t 5
goto loop
