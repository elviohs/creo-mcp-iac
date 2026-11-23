@echo off
rem Arranca el servicio local de lecturas nativas de Creo (puerto 9057).
rem Arrancalo desde aqui (fuera de Claude), como CREOSON, y deja la ventana abierta.
title Servicio de lecturas nativas de Creo (creo-mcp-iac)
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0servicio_tk.py" %*
pause
