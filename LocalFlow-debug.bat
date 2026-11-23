@echo off
rem Runs LocalFlow with a console window so you can watch the log live. Close this window to quit.
"%LOCALAPPDATA%\LocalFlow\venv\Scripts\python.exe" "%~dp0localflow.py"
pause
