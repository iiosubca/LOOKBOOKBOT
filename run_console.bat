@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите setup.bat
  pause
  exit /b 1
)

call ".venv\Scripts\python.exe" -m lookbookbot

