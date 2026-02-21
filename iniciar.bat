@echo off
chcp 65001 > nul
title OpenTralla
cd /d "%~dp0"
python transcriptor.py
if %errorlevel% neq 0 (
    echo.
    echo  ERROR al iniciar. Asegurate de haber ejecutado instalar.bat primero.
    pause
)
