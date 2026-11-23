@echo off
rem Starts LocalFlow in the background (look for the microphone icon in the system tray).
set "PYW=%LOCALAPPDATA%\LocalFlow\venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
  echo LocalFlow is not set up yet - running setup first...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" || (pause & exit /b 1)
)
start "" "%PYW%" "%~dp0localflow.py"
