@echo off
rem Arranque unico: Creo + CREOSON + servicio de lecturas nativas (creo-mcp-iac).
rem Doble clic. Con -CrearAcceso crea un acceso directo en el escritorio.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar_creo_iac.ps1" %*
echo.
pause
