@echo off
rem Build IDXAlert3.exe - single file, no console.
cd /d "%~dp0"

echo Running the test suites first - a broken build is worse than no build.
python idx3.py --selftest || goto :fail
python test_keepalive.py   || goto :fail
python test_popup.py       || goto :fail
python test_options.py     || goto :fail
python test_window.py      || goto :fail
python test_tray.py        || goto :fail
python test_filters.py     || goto :fail
echo.

echo Installing build dependencies...
python -m pip install --quiet --upgrade pystray pillow pyinstaller || goto :fail

echo Building IDXAlert3.exe ...
python -m PyInstaller --noconfirm --clean ^
  --onefile ^
  --noconsole ^
  --name IDXAlert3 ^
  --hidden-import pystray._win32 ^
  --collect-submodules tkinter ^
  --hidden-import idx3 ^
  --hidden-import idx3net ^
  --hidden-import idx3sched ^
  --hidden-import idx3lat ^
  --hidden-import idx3popup ^
  --hidden-import idx3options ^
  --hidden-import idx3startup ^
  idx3tray.py || goto :fail

rem Do NOT clobber a config already tuned inside dist\. The .exe reads the
rem config next to ITSELF, not the one in this folder; keeping both in sync by
rem copying over it is how you end up with two watchers alerting differently.
if exist "dist\config.json" (
  echo Kept your existing dist\config.json
) else (
  copy /y config.json dist\config.json >nul
  echo Seeded dist\config.json from this folder
)
echo.
echo ============================================================
echo  Built: %~dp0dist\IDXAlert3.exe
echo.
echo  The .exe reads config.json, seen.json, latency.csv and
echo  logs\ from ITS OWN folder, not from this source folder.
echo  Editing Options in the .exe does NOT change the source
echo  config, and vice versa. Move the whole dist folder if you
echo  relocate it.
echo.
echo  First run from its final location:
echo    - right-click the tray icon ^> Verify feed
echo    - then Options ^> Schedule ^> Start automatically
echo      (so autostart points at the right path)
echo ============================================================
pause
exit /b 0

:fail
echo.
echo BUILD FAILED - see the error above.
pause
exit /b 1
