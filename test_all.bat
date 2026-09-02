@echo off
rem Every offline suite. None of these need a network or a display.
cd /d "%~dp0"
python idx3.py --selftest
echo.
python test_keepalive.py
echo.
python test_popup.py
echo.
python test_options.py
echo.
python test_window.py
echo.
python test_tray.py
echo.
python test_filters.py
pause
