@echo off
rem Fingerprinter uninstall. Windows only.
rem
rem Removes the Fingerprinter: its program folder, with audfprint, ffmpeg and
rem Node.js (tools), its settings and scratch files, and its shortcuts. Your
rem fingerprints (pklz-files), kept audio (audio) and anything in the working
rem folder (downloads) are asked about one by one and kept unless you say so,
rem and files that did not come with the Fingerprinter are never touched.
rem Setup notes in installed-by-setup.txt which Python packages, and whether
rem Python itself, it installed; only those are offered for removal, and only
rem when you say yes, as other programs may use them too.
rem
rem dependencies.py --uninstall does the work. This file finds Python for it,
rem removes Python afterwards if asked (it cannot while it is running), and
rem last deletes itself and the folder, if nothing else is left in it.

setlocal EnableExtensions
rem pushd, not cd: it also works for a folder on a network share (\\server\...),
rem where cd cannot go, and anything below would then run in the wrong place.
pushd "%~dp0" || (
    echo Cannot open the program folder %~dp0
    pause
    exit /b 1
)
title Uninstall the Fingerprinter

call :find_python
if not defined PY goto no_python
set "THEN=%TEMP%\fingerprinter-uninstall-%RANDOM%%RANDOM%.cmd"
if exist "%THEN%" del "%THEN%"
%PY% dependencies.py --uninstall "%THEN%"
rem 0 done, 1 nothing removed, 2 some of it could not be removed. Anything
rem else (Ctrl+C gives a large negative code) counts as stopped.
set "RC=%errorlevel%"
if "%RC%"=="2" goto partly
if not "%RC%"=="0" goto stopped
if exist "%THEN%" (
    call "%THEN%"
    del "%THEN%" >nul 2>&1
)
goto finish

:no_python
rem Python is gone, so there are no packages left to remove either; only the
rem program's own files are. The same lists as in dependencies.py.
echo Uninstall the Fingerprinter
echo.
echo Program folder: %~dp0
echo.
echo Python is not installed any more, so only the program's own files can be
echo removed. Your fingerprints (pklz-files), kept audio (audio) and the working
echo folder (downloads) are kept. If you made a desktop shortcut, delete it too.
echo.
choice /c YN /m "Remove the Fingerprinter"
rem Y is 1, N is 2, and Ctrl+C gives 0.
if errorlevel 2 goto stopped
if not errorlevel 1 goto stopped
if exist "work\pklz\*.pklz" (
    if not exist "pklz-files\" mkdir "pklz-files"
    for %%P in ("work\pklz\*.pklz") do call :keep_pklz "%%~fP" "%%~nP"
)
for %%F in (yt-fingerprinter.pyw dependencies.py audfprint_quiet.py requirements.txt config.example.json README.md CHANGELOG.md LICENSE fingerprinter.ico setup.bat Fingerprinter.lnk config.json recent_urls.json fingerprinted-items.txt unfinished-list.json unfinished-list.tmp installed-by-setup.txt) do (
    if exist "%%F" del /f /q "%%F"
)
for %%D in (audfprint audfprint.download texts __pycache__ tools\ffmpeg tools\node work\texts) do (
    if exist "%%D\" rmdir /s /q "%%D"
)
for /d %%D in (audfprint.old audfprint.old? audfprint.old?? tools\tmp*) do (
    if exist "%%D\" rmdir /s /q "%%D"
)
rem work\pklz goes only once nothing unfinished is left in it.
if exist "work\pklz\*.pklz" (
    echo Some unfinished fingerprints could not be moved out of work\pklz, so work stays.
) else (
    if exist "work\pklz\" rmdir /s /q "work\pklz"
)
rem tools and work go only once empty.
rmdir "tools" 2>nul
rmdir "work" 2>nul
goto finish

:keep_pklz
rem Moves one unfinished work\pklz\<name>.pklz into pklz-files as
rem recovered-<name>.pklz, or recovered-<name>_2.pklz and so on: never over
rem a file already there, as MOVE would do without asking.
set "TARGET=pklz-files\recovered-%~2.pklz"
set N=2
:keep_next
if exist "%TARGET%" (
    set "TARGET=pklz-files\recovered-%~2_%N%.pklz"
    set /a N+=1
    goto keep_next
)
move "%~1" "%TARGET%" >nul || echo Could not move %~nx1 out of work\pklz.
exit /b 0

:partly
echo.
echo Some parts could not be removed; see above. Close whatever uses them and
echo run uninstall.bat again, which is still here for that.
pause
goto :eof

:stopped
echo.
pause
goto :eof

:finish
echo.
echo The Fingerprinter has been removed. Press any key to close this window.
pause >nul
rem This file goes last, and the folder with it if nothing else is left.
rem "(goto)" ends the batch file while the rest of the line still runs; the
rem folder change comes after it, as ending the batch file restores the
rem folder it started in, which could not be removed while it is in use.
(goto) 2>nul & cd /d "%TEMP%" & del "%~f0" & rmdir "%~dp0." 2>nul
goto :eof

:find_python
rem As in setup.bat.
set PY=
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set PY=py -3
    exit /b 0
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set PY=python
    exit /b 0
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
    "%%D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && (
        set PY="%%D\python.exe"
    )
)
exit /b 0
