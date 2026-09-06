@echo off
setlocal EnableExtensions

if defined PASTEBERTH_HOME goto configured_root
set "DEPLOYMENT_ROOT=%~dp0"
goto root_ready

:configured_root
set "DEPLOYMENT_ROOT=%PASTEBERTH_HOME%"

:root_ready
if not exist "%DEPLOYMENT_ROOT%\runtime\__init__.py" goto invalid_root
if not exist "%DEPLOYMENT_ROOT%\runtime\__main__.py" goto invalid_root
if not exist "%DEPLOYMENT_ROOT%\runtime\static" goto invalid_root
if not exist "%DEPLOYMENT_ROOT%\runtime\templates" goto invalid_root

set "PYTHONPATH=%DEPLOYMENT_ROOT%"
set "PYTHONDONTWRITEBYTECODE=1"

where py >nul 2>&1
if not errorlevel 1 goto use_py_launcher

python -P -m runtime %*
set "EXIT_CODE=%ERRORLEVEL%"
goto finish

:use_py_launcher
py -3 -P -m runtime %*
set "EXIT_CODE=%ERRORLEVEL%"
goto finish

:invalid_root
>&2 echo pasteberth: invalid deployment root: %DEPLOYMENT_ROOT%
set "EXIT_CODE=2"

:finish
exit /b %EXIT_CODE%
