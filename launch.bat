@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONDONTWRITEBYTECODE=1"

set "SCRIPT=%~dp0steam_osd_tuner.pyw"
if not exist "%SCRIPT%" (
    echo [ERROR] Could not find "%SCRIPT%"
    pause
    exit /b 1
)

set "PY_CONSOLE="
set "PY_GUI="

python -B -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY_CONSOLE=python"
if not defined PY_CONSOLE (
    py -3 -B -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY_CONSOLE=py -3"
)

where pythonw >nul 2>&1 && set "PY_GUI=pythonw"
if not defined PY_GUI (
    where pyw >nul 2>&1 && set "PY_GUI=pyw -3"
)

if not defined PY_CONSOLE (
    for /d %%D in ("%LocalAppData%\Python\pythoncore-*" "%LocalAppData%\Programs\Python\Python*" "%ProgramFiles%\Python*") do (
        if not defined PY_CONSOLE if exist "%%~D\python.exe" set PY_CONSOLE="%%~D\python.exe"
        if not defined PY_GUI if exist "%%~D\pythonw.exe" set PY_GUI="%%~D\pythonw.exe"
    )
)

if not defined PY_GUI (
    for /d %%D in ("%LocalAppData%\Python\pythoncore-*" "%LocalAppData%\Programs\Python\Python*" "%ProgramFiles%\Python*") do (
        if not defined PY_GUI if exist "%%~D\pythonw.exe" set PY_GUI="%%~D\pythonw.exe"
    )
)

rem If CLI flags (e.g. --self-test) are passed, run synchronously with console Python
if not "%~1"=="" (
    if not defined PY_CONSOLE (
        echo [ERROR] Python 3.10+ not found in PATH or standard installation folders.
        exit /b 1
    )
    %PY_CONSOLE% -B "%SCRIPT%" %*
    exit /b %errorlevel%
)

if not defined PY_CONSOLE if not defined PY_GUI (
    echo [ERROR] Python 3.10+ was not found in PATH. Please install Python 3.10+ from python.org.
    pause
    exit /b 1
)

if defined PY_CONSOLE (
    %PY_CONSOLE% -B -c "import PIL, tkinter" >nul 2>&1
    if errorlevel 1 (
        echo [INFO] Installing missing dependencies from requirements.txt ...
        %PY_CONSOLE% -B -m pip install -r "%~dp0requirements.txt"
        if errorlevel 1 (
            echo [ERROR] Failed to install dependencies. Run: pip install -r requirements.txt
            pause
            exit /b 1
        )
    )
)

if defined PY_GUI (
    start "" %PY_GUI% -B "%SCRIPT%"
) else (
    start "" %PY_CONSOLE% -B "%SCRIPT%"
)
endlocal
