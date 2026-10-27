@echo off
cd /d "%~dp0"
rem open instantly with last data, rebuild in the background (page reloads itself when done)
set "CH=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CH%" set "CH=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist data.js (
  echo First run: pulling MT5 data...
  python build.py
)
if exist "%CH%" (
  start "" "%CH%" --app="file:///%~dp0trade_chart.html" --window-size=1600,950
) else (
  start "" "%~dp0trade_chart.html"
)
if exist data.js start "" pythonw build.py
