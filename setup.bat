@echo off
rem Fingerprinter setup. Windows only.
rem
rem Finds Python 3.10 or newer (and offers to install it with winget if there is
rem none), then runs dependencies.py. That first brings yt-dlp to its newest
rem release, every time and whatever version is installed, as an old yt-dlp is
rem the commonest reason downloads fail. Then it checks the Python packages,
rem yt-dlp, ffmpeg, Node.js and WerZatSong's audfprint, lists anything missing
rem or not working, and installs it if you say yes. Anything else that already
rem works is left alone, so running this again is safe.
rem It also makes Fingerprinter.lnk, the program's shortcut with its fingerprint
rem icon, and asks once whether to put one on the desktop. What it installs
rem outside the program folder is noted in installed-by-setup.txt, so that
rem uninstall.bat can offer to remove exactly that.
rem
rem If winget cannot install Python, this says why in plain words and offers
rem the other ways to get it: for all users (with an administrator's
rem permission), from the Microsoft Store, or from python.org. The commonest
rem failure is a Windows policy that forbids the installation (winget error
rem 0x8A15010F, Windows Installer code 1625); see :explain_failure.

setlocal EnableExtensions
rem pushd, not cd: it also works for a folder on a network share (\\server\...),
rem where cd cannot go and dependencies.py would not be found.
pushd "%~dp0" || (
    echo Cannot open the program folder %~dp0
    pause
    exit /b 1
)
title Fingerprinter setup

call :find_python
if defined PY goto have_python

echo Python 3.10 or newer is needed to run the Fingerprinter and was not found.
where winget >nul 2>&1
if errorlevel 1 goto manual_python
echo.
choice /c YN /m "Install Python 3.13 for this user with winget"
if errorlevel 2 goto manual_python
set "SCOPE=user"

:install_python
echo.
winget install --exact --id Python.Python.3.13 --scope %SCOPE%
set "WG=%errorlevel%"
rem Noted for uninstall.bat, which offers to remove what setup installed.
if "%WG%"=="0" >>"%~dp0installed-by-setup.txt" echo python Python.Python.3.13 %SCOPE%
call :find_python
if defined PY goto have_python
if "%WG%"=="0" goto installed_unseen
call :explain_failure
if "%NEXT%"=="end" goto end
goto python_options

:installed_unseen
echo.
echo Python was installed, but this window cannot see it yet.
echo Close this window and run setup.bat again.
goto end

:python_options
echo.
echo What would you like to do?
echo   1  Try again, installing Python for all users of this PC. Windows asks for
echo      an administrator's permission. This works where only installing for a
echo      single user is blocked.
echo   2  Get Python 3.13 from the Microsoft Store. Store apps are not installed by
echo      Windows Installer, so its policies do not apply, unless the Store itself
echo      is blocked too.
echo   3  Open python.org to install Python by hand.
echo   4  Quit.
echo.
choice /c 1234 /n /m "Choose 1, 2, 3 or 4: "
if errorlevel 4 goto end
if errorlevel 3 goto manual_python
if errorlevel 2 goto store_python
set "SCOPE=machine"
goto install_python

:store_python
start "" "ms-windows-store://pdp/?ProductId=9PNRBTZXMB4Z"
echo.
echo The Microsoft Store opens at Python 3.13. Press Get or Install, wait until it
echo has finished, then run setup.bat again.
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
%PY% dependencies.py --update-ytdlp --shortcut
if errorlevel 1 goto end
echo.
choice /c YN /m "Start the Fingerprinter now"
if errorlevel 2 goto end
rem pythonw runs the program without a console window. The Microsoft Store's
rem Python may not provide it, so fall back to the plain one.
%PYW% -c "import sys" >nul 2>&1 || set PYW=%PY%
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
rem Where installs for one user and for all users land, before this window's
rem PATH knows about them.
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
    "%%D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && (
        set PY="%%D\python.exe"
        set PYW="%%D\pythonw.exe"
    )
)
exit /b 0

:explain_failure
rem Says why winget could not install Python, going by its exit code (listed at
rem github.com/microsoft/winget-cli, doc/windows/package-manager/winget/returnCodes.md),
rem and sets NEXT to "options" (offer the other ways) or "end" (nothing to try
rem from here). %=ExitCode% shows the code in the hex form winget's docs use.
cmd /c exit %WG%
set "WGHEX=0x%=ExitCode%"
set "NEXT=options"
echo.
echo Installing Python did not finish.
echo.
if "%WG%"=="-1978334961" goto why_policy
if "%WG%"=="-1978335174" goto why_winget_policy
if "%WG%"=="-1978334964" goto why_cancelled
if "%WG%"=="-1978334969" goto why_network
if "%WG%"=="-1978334971" goto why_disk
if "%WG%"=="-1978334967" goto why_restart_after
if "%WG%"=="-1978334966" goto why_restart_before
if "%WG%"=="-1978334963" goto why_already
if "%WG%"=="-1978335135" goto why_already
rem Offline, winget fails before any installer runs, with the network error
rem itself: name not resolved, cannot connect, timed out, or download failed.
if "%WG%"=="-2147012889" goto why_network
if "%WG%"=="-2147012867" goto why_network
if "%WG%"=="-2147012894" goto why_network
if "%WG%"=="-1978335224" goto why_network
rem Any other code: an offline PC can still be the reason (winget's package
rem list can fail to open first), so look before blaming anything else.
where curl.exe >nul 2>&1 && (
    curl.exe -s -o nul -I --max-time 15 https://www.python.org >nul 2>&1 || goto why_network
)
echo winget reported error %WGHEX%. The lines above say more, including where
echo the installer's log is if the installer ran.
exit /b 0

:why_policy
echo A Windows policy on this PC does not allow the installation (winget error
echo %WGHEX%, "Organization policies are preventing installation"). The Python
echo installer asked Windows Installer to install it, and Windows Installer
echo refused with its code 1625, "This installation is forbidden by system
echo policy". Such policies come from whoever manages the PC, like a workplace
echo or a school, and on a home PC sometimes from security, parental control or
echo tweaking tools.
call :show_policies
exit /b 0

:why_winget_policy
echo A Windows policy on this PC does not allow winget to install programs
echo (winget error %WGHEX%). The Microsoft Store or python.org may still work.
exit /b 0

:why_cancelled
echo The installation was cancelled, possibly at the administrator prompt.
exit /b 0

:why_network
echo The download needs an internet connection, and this PC does not seem to have
echo one (winget error %WGHEX%). Connect, then try again. The other ways below
echo need a connection too.
exit /b 0

:why_disk
echo There is not enough free disk space. Free some space, then try again.
exit /b 0

:why_restart_after
echo Windows needs a restart to finish installing Python. Restart the PC, then
echo run setup.bat again.
set "NEXT=end"
exit /b 0

:why_restart_before
echo Windows needs a restart before Python can be installed. Restart the PC,
echo then run setup.bat again.
set "NEXT=end"
exit /b 0

:why_already
echo winget says Python 3.13 is already installed, but it does not work from
echo here. Repairing it with the installer from python.org usually fixes that.
exit /b 0

:show_policies
rem The two Windows Installer policies that block installs like this one,
rem set for the whole PC (HKLM) or for this user (HKCU).
echo.
set "FOUND="
for %%K in (HKLM HKCU) do for %%V in (DisableUserInstalls DisableMSI) do (
    for /f "tokens=3" %%A in ('reg query "%%K\SOFTWARE\Policies\Microsoft\Windows\Installer" /v %%V 2^>nul ^| find /i "%%V"') do call :policy_line %%K %%V %%A
)
if defined FOUND exit /b 0
echo No Windows Installer policy is set in this PC's registry, so the block
echo probably comes from device management, security software or AppLocker.
echo Whoever manages this PC can install Python for you, or allow it.
exit /b 0

:policy_line
rem 0 is the policy set to allow installs, so it is not the cause.
if "%3"=="0x0" exit /b 0
set "FOUND=1"
set "MEANS=%3"
if /i "%2"=="DisableUserInstalls" if "%3"=="0x1" set "MEANS=1, installing for a single user is not allowed. Option 1 below can work."
if /i "%2"=="DisableMSI" if "%3"=="0x1" set "MEANS=1, only installations an administrator approves are allowed. Option 1 below can work."
if /i "%2"=="DisableMSI" if "%3"=="0x2" set "MEANS=2, Windows Installer is switched off completely. Try option 2 below."
echo Found the policy %2 for %1: %MEANS%
exit /b 0

:end
echo.
pause
endlocal
