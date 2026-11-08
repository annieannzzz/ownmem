@echo off
REM OwnMem launcher (local, no-install mode)
REM Usage: ownmem.cmd recall --hook claude  |  ownmem.cmd ui  |  ownmem.cmd status
REM NOTE: keep this file ASCII-only. Chinese comments break cmd's GBK parsing.

setlocal
chcp 65001 >nul
set "PYTHONPATH=%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "OWNMEM_PY=C:\Users\34913\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%OWNMEM_PY%" set "OWNMEM_PY=python"
"%OWNMEM_PY%" -m ownmem.cli %*
