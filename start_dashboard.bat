@echo off
chcp 65001 >nul
set "PORT_ARG="
if not "%~1"=="" set "PORT_ARG=-DashboardPort %~1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\tradex_control.ps1" start-dashboard %PORT_ARG%
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%
