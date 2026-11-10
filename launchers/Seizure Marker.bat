@echo off
REM Double-click launcher (Windows). Run launchers\setup_windows.bat once first.
cd /d "%~dp0.."
if not exist ".venv\Scripts\pythonw.exe" (
  echo Not set up yet: double-click launchers\setup_windows.bat first.
  pause
  exit /b 1
)
set MPLBACKEND=
start "" ".venv\Scripts\pythonw.exe" -m seizure_marker %*
