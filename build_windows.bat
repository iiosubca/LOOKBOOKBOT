@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo Сначала запустите setup.bat
  pause
  exit /b 1
)

call ".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name LOOKBOOKBOT --paths src launcher.py
if errorlevel 1 exit /b 1

echo.
echo Сборка готова: dist\LOOKBOOKBOT\LOOKBOOKBOT.exe
pause
