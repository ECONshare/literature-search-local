@echo off
rem LitReview Local - double-click this file to start the app.
rem
rem It finds a Python 3.8 or newer to run launcher.py, which does the rest: it
rem sets up the app's own Python 3.10+ environment, starts the app and opens it
rem in your browser. If this computer has no Python at all, it offers to install
rem uv, a small tool that fetches Python - and asks first.
rem
rem This file must keep Windows (CRLF) line endings; see .gitattributes.

setlocal EnableExtensions DisableDelayedExpansion
title LitReview
pushd "%~dp0" || (echo Could not open the folder "%~dp0". & goto pause_exit)

rem Each candidate is run to check its version: a command can exist and not be
rem Python (the Microsoft Store's "python" and "python3" only print a message).
set "LR_PY="
call :try_python ".venv\Scripts\python.exe"
if not defined LR_PY call :try_python py -3
if not defined LR_PY call :try_python python
if not defined LR_PY call :try_python python3
if defined LR_PY goto run_python
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined LR_PY call :try_python "%%~fD\python.exe"
)
if defined LR_PY goto run_python
for /d %%D in ("%ProgramFiles%\Python3*") do (
    if not defined LR_PY call :try_python "%%~fD\python.exe"
)
if defined LR_PY goto run_python
goto no_python


:try_python
rem Is %* a Python 3.8 or newer? Sets LR_PY if so.
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "LR_PY=%*"
exit /b 0


:run_python
%LR_PY% launcher.py %*
set "LR_EXIT=%ERRORLEVEL%"
if not "%LR_EXIT%"=="0" goto failed
exit /b 0


:no_python
echo.
echo LitReview needs Python, and none was found on this computer.
echo.
set "LR_UV="
for %%U in (uv.exe) do set "LR_UV=%%~$PATH:U"
if not defined LR_UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "LR_UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined LR_UV if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "LR_UV=%USERPROFILE%\.cargo\bin\uv.exe"
if defined LR_UV goto run_uv

echo It can set Python up for you with uv, a small free tool from Astral
echo (https://docs.astral.sh/uv/) that downloads a private copy of Python just
echo for LitReview. uv installs into your user folder and needs no
echo administrator rights.
echo.
set "LR_ANSWER="
set /p "LR_ANSWER=Install uv now? Type y and press Enter (or just Enter to cancel): "
setlocal EnableDelayedExpansion
if /i "!LR_ANSWER!"=="y" (endlocal & goto install_uv)
if /i "!LR_ANSWER!"=="yes" (endlocal & goto install_uv)
endlocal
echo.
echo Nothing was installed.
goto python_help


:install_uv
echo.
echo Installing uv...
powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 goto uv_failed
if exist "%USERPROFILE%\.local\bin\uv.exe" set "LR_UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined LR_UV if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "LR_UV=%USERPROFILE%\.cargo\bin\uv.exe"
if not defined LR_UV for %%U in (uv.exe) do set "LR_UV=%%~$PATH:U"
if not defined LR_UV goto uv_failed


:run_uv
echo Starting LitReview with Python 3.12 from uv. The first time, uv downloads
echo it, which takes a minute.
echo.
"%LR_UV%" run --no-project --python 3.12 python launcher.py %*
set "LR_EXIT=%ERRORLEVEL%"
if not "%LR_EXIT%"=="0" goto failed
exit /b 0


:uv_failed
echo.
echo Installing uv did not work. It needs an internet connection.
goto python_help


:python_help
echo.
echo To install Python yourself:
echo   1. Go to https://www.python.org/downloads/windows/ and download the
echo      Windows installer for Python 3.12, 64-bit.
echo   2. Run it, and tick "Add python.exe to PATH" on its first screen.
echo   3. Double-click start-windows.bat again.
goto pause_exit


:failed
echo.
echo LitReview could not start. The messages above say why and what to do.

:pause_exit
echo.
pause
exit /b 1
