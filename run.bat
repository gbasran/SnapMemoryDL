@echo off
setlocal EnableExtensions
title Snap Memories Downloader

REM Try python first, then py launcher
set "PY=python"
where python >nul 2>nul || set "PY=py -3"

%PY% snap_memories_dl.py
pause

