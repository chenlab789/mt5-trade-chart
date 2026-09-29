@echo off
cd /d "%~dp0"
echo Pulling MT5 data...
python build.py
start "" "%~dp0trade_chart.html"
