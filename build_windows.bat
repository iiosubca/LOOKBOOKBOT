@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo Please run setup.bat first.
  exit /b 1
)

call ".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name LOOKBOOKBOT --paths src launcher.py
if errorlevel 1 exit /b 1

echo.
echo Build ready: dist\LOOKBOOKBOT\LOOKBOOKBOT.exe
