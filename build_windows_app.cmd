@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows_app.ps1" %*
endlocal
