@echo off
rem Aniimo -> Palworld Studio launcher (Windows). Double-click to start.
rem Everything is installed and created inside this folder (studio-data\).
rem Note: PY holds either a launcher command (e.g. py -3.12) or a quoted path, so it is used unquoted below.
setlocal
set "STUDIO=%~dp0"
cd /d "%STUDIO%"
set "PORTABLE=%STUDIO%studio-data\python\python.exe"

if exist "%PORTABLE%" (
  set PY="%PORTABLE%"
  goto run
)

rem Use an installed Python 3.11+ that includes tkinter, if there is one.
for %%C in ("py -3.13" "py -3.12" "py -3.11" "python") do (
  %%~C -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && (
    set "PY=%%~C"
    goto run
  )
)

echo No suitable Python (3.11+ with tkinter) was found.
echo Downloading a portable Python into this folder (about 40 MB) ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%STUDIO%launcher\get-python.ps1" -Dest "%STUDIO%studio-data"
if exist "%PORTABLE%" (
  set PY="%PORTABLE%"
  goto run
)
echo.
echo Python could not be downloaded automatically.
echo Please install Python 3.12 from https://www.python.org/downloads/ (keep "tcl/tk and IDLE" ticked),
echo then double-click Start-Studio.bat again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:run
%PY% "%STUDIO%start_studio.py" %*
if errorlevel 1 pause
