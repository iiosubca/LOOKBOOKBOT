@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\vdiza\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=py -3.12"

if not exist ".venv\Scripts\python.exe" (
  %PYTHON_EXE% -m venv .venv
  if errorlevel 1 exit /b 1
)

call .venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 exit /b 1
call .venv\Scripts\python.exe -m pip install -e ".[dev]"
if errorlevel 1 exit /b 1

echo.
echo LOOKBOOKBOT installed. Run run.bat
pause
