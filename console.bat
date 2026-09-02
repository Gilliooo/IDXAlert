@echo off
rem Headless engine - same polling, alerts to the console and today's log.
rem Useful for watching the timing live. Ctrl+C to stop.
cd /d "%~dp0"
python idx3.py
pause
