@echo off
rem Fingerprinter setup. Windows only.
rem
rem Finds Python 3.10 or newer (and offers to install it with winget if there is
rem none), then runs dependencies.py. That checks the Python packages, yt-dlp,
rem ffmpeg, Node.js and WerZatSong's audfprint, lists anything missing or not
rem working, and installs it if you say yes. Anything that already works is
rem left alone, so running this again is safe.

setlocal EnableExtensions
cd /d "%~dp0"
title Fingerprinter setup

call :find_python
if defined PY goto have_python

echo Python 3.10 or newer is needed to run the Fingerprinter and was not found.
where winget >nul 2>&1
if errorlevel 1 goto manual_python
echo.
choice /c YN /m "Install Python 3.13 for this user with winget"
if errorlevel 2 goto manual_python
winget install --exact --id Python.Python.3.13 --scope user
call :find_python
if defined PY goto have_python
echo.
echo Python is installed, but this window cannot see it yet.
echo Close this window and run setup.bat again.
goto end

:manual_python
echo.
echo Install Python from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
start "" https://www.python.org/downloads/windows/
goto end

:have_python
echo Using Python: %PY%
echo.
%PY% dependencies.py
if errorlevel 1 goto end
echo.
choice /c YN /m "Start the Fingerprinter now"
if errorlevel 2 goto end
start "" %PYW% "%~dp0yt-fingerprinter.pyw"
goto end

:find_python
set PY=
set PYW=
rem The py launcher first: it also finds installs that are not on PATH.
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set PY=py -3
    set PYW=pyw -3
    exit /b 0
)
rem The Microsoft Store "python" stub fails this check, which is what we want.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set PY=python
    set PYW=pythonw
    exit /b 0
)
rem Where a per-user install lands, before this window's PATH knows about it.
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    "%%D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && (
        set PY="%%D\python.exe"
        set PYW="%%D\pythonw.exe"
    )
)
exit /b 0

:end
echo.
pause
endlocal
