@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo Сначала запустите setup.bat
  pause
  exit /b 1
)

start "LOOKBOOKBOT" ".venv\Scripts\pythonw.exe" -m lookbookbot

