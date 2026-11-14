@echo off
title Stop Codexen MCP Engine
cd /d "%~dp0"

set "PY_EXE="
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import starlette" >nul 2>nul
    if not errorlevel 1 set "PY_EXE=%~dp0.venv\Scripts\python.exe"
)
if "%PY_EXE%"=="" if exist "C:\Users\%USERNAME%\Miniconda3\python.exe" (
    set "PY_EXE=C:\Users\%USERNAME%\Miniconda3\python.exe"
    set "PATH=C:\Users\%USERNAME%\Miniconda3;C:\Users\%USERNAME%\Miniconda3\Scripts;C:\Users\%USERNAME%\Miniconda3\Library\bin;%PATH%"
) else if "%PY_EXE%"=="" (
    where python >nul 2>nul
    if %errorlevel% equ 0 set "PY_EXE=python"
)

if "%PY_EXE%"=="" (
    echo [ERROR] Python environment not found!
    exit /b 1
)

"%PY_EXE%" -m codexen.launcher --stop
ping 127.0.0.1 -n 2 >nul
