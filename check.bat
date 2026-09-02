@echo off
rem First-run sanity: can this machine reach IDX, and how fast?
cd /d "%~dp0"
python idx3.py --plan
echo.
python idx3.py --probe
echo.
python idx3.py --verify-feed
echo.
python idx3.py --selftest
echo.
python test_keepalive.py
pause
