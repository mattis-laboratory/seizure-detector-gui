@echo off
REM One-time setup on Windows: creates a private Python environment (.venv) next to
REM this folder and installs the app into it. Double-click this file.
setlocal
cd /d "%~dp0.."
echo Seizure Marker setup in: %cd%

set PY=
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set PY=py -3
if "%PY%"=="" python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set PY=python
if "%PY%"=="" (
  echo.
  echo Python 3.10 or newer was not found.
  echo Install it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)
echo Using: %PY%

%PY% -m venv .venv || goto :fail
.venv\Scripts\python.exe -m pip install --upgrade pip || goto :fail
.venv\Scripts\python.exe -m pip install -e . || goto :fail

echo.
echo Done. Start the app with launchers\"Seizure Marker.bat".
pause
exit /b 0

:fail
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
