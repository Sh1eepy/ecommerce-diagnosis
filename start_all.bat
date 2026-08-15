@echo off
rem ============================================
rem  E-commerce Diagnosis - START ALL SERVICES
rem  双击启动 API + Worker + Scheduler（后台静默运行）
rem ============================================
cd /d "%~dp0"

echo ============================================
echo   E-commerce Diagnosis Service Starter
echo ============================================
echo.

".venv\Scripts\python.exe" scripts\manage_services.py start

echo.
echo  All services started in background.
echo  Dashboard: http://127.0.0.1:8000/api/v1/monitoring/dashboard
echo  Logs:      logs\service\*.log
echo  Stop all:  double-click stop_all.bat
echo.
pause
