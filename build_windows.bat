@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo Please run setup.bat first.
  exit /b 1
)

call ".venv\Scripts\pyinstaller.exe" --noconfirm --clean LOOKBOOKBOT.spec
if errorlevel 1 exit /b 1

copy /Y "dist\LOOKBOOKBOT.exe" "LOOKBOOKBOT.exe" >nul
if errorlevel 1 exit /b 1

echo.
echo Build ready: LOOKBOOKBOT.exe
