@echo off
cd /d "%~dp0"
python idx3.py --stats
echo.
python idx3.py --status
pause
