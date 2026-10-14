@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 (
  echo 安装失败，请查看上方错误，解决后重新运行。
  pause
  exit /b 1
)
pause
