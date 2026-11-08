@echo off
REM OwnMem audit console - double-click launcher
REM Starts the web UI in this window and opens the browser.
REM Keep this file ASCII-only (cmd GBK parsing breaks on Chinese comments).

setlocal
chcp 65001 >nul
set "PYTHONPATH=%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "OWNMEM_PY=C:\Users\34913\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%OWNMEM_PY%" set "OWNMEM_PY=python"

echo ============================================
echo   OwnMem Audit Console
echo   http://127.0.0.1:8787/
echo   Close this window to stop the server.
echo ============================================

"%OWNMEM_PY%" -m ownmem.cli ui --port 8787

REM If the server exits (port taken / crash), pause so the window stays open
REM and the error message is readable.
echo.
echo Server stopped. If it said "already running" above, everything is fine -
echo the console is in your browser. Otherwise screenshot the error and send it.
pause
