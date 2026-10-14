@echo off
chcp 65001 >nul
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\launch.py"
if errorlevel 1 pause
