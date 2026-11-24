@echo off
chcp 65001 >nul
title 汽水音乐自动化本地部署
cd /d "%~dp0"
if exist "%~dp0platform-tools\adb.exe" set "PATH=%~dp0platform-tools;%PATH%"
set PYTHONIOENCODING=utf-8
set PY=python
where python >nul 2>nul || set PY=py -3
%PY% qishui_gui.py
pause