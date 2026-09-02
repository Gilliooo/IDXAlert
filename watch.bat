@echo off
rem IDXAlert 3.0 - the tray app. This is the one to run.
rem Look for the green icon in the system tray; right-click it for the menu.
cd /d "%~dp0"
start "" pythonw idx3tray.py
