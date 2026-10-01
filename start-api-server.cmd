@echo off
REM ============================================================
REM  Double-click launcher for the DeepSeek Web -> OpenAI API server.
REM
REM  ASCII ONLY in this file. cmd.exe reads .cmd in the OEM codepage
REM  (GBK on this machine), so UTF-8 Chinese here would get mangled and
REM  eat the commands that follow. Same trap that broke ask.cmd once.
REM
REM  The script's own folder is used via %~dp0 instead of hardcoding the
REM  path, because that path contains Chinese characters.
REM ============================================================

title DeepSeek Web API Server
chcp 65001 > nul

set "PY=C:\Users\MOONFISH\AppData\Local\Programs\Python\Python311\python.exe"
set "SERVER=%~dp0api_server.py"
set "PORT=8899"

if not exist "%PY%" (
    echo.
    echo  [ERROR] Python not found:
    echo          %PY%
    echo.
    pause
    exit /b 1
)

if not exist "%SERVER%" (
    echo.
    echo  [ERROR] api_server.py not found next to this file:
    echo          %SERVER%
    echo.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   DeepSeek Web  -^>  OpenAI-compatible API
echo  ============================================================
echo.
echo   Base URL : http://127.0.0.1:%PORT%/v1
echo   API Key  : anything (local server, not checked)
echo   Model    : deepseek-web  /  deepseek-web-think
echo.
echo   One request takes 10-60s. One request at a time.
echo.
echo   Close this window or press Ctrl+C to stop.
echo  ============================================================
echo.

"%PY%" -X utf8 "%SERVER%" --port %PORT% %*

echo.
echo  Server stopped.
pause
