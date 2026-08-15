@echo off
rem ============================================
rem  E-commerce Diagnosis - STOP ALL SERVICES
rem  双击停止 API + Worker + Scheduler
rem ============================================
cd /d "%~dp0"

echo ============================================
echo   E-commerce Diagnosis Service Stopper
echo ============================================
echo.

".venv\Scripts\python.exe" scripts\manage_services.py stop

echo.
echo  All services stopped.
echo.
pause
