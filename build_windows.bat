@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo Please run setup.bat first.
  exit /b 1
)

call ".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed --name LOOKBOOKBOT --paths src --icon "src\assets\lookbookbot.ico" --add-data "src\assets\lbb-logo.png;assets" --add-data "src\assets\lookbookbot.ico;assets" --add-data "automation-engine\lookbook-layout\scripts;automation-engine\core\scripts" launcher.py
if errorlevel 1 exit /b 1

copy /Y "dist\LOOKBOOKBOT.exe" "LOOKBOOKBOT.exe" >nul
if errorlevel 1 exit /b 1

echo.
echo Build ready: LOOKBOOKBOT.exe
