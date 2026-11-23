@echo off
rem Ejecuta "creo_iac_tk.exe estado" contra la sesion de Creo abierta.
rem Prepara el entorno: pro_comm_msg.exe y las DLL de Creo (ucore/udata) en el PATH.
setlocal
set "CREO=C:\Program Files\PTC\Creo 12.4.1.0\Common Files"
if not defined PRO_COMM_MSG_EXE (
  for /f "delims=" %%p in ('where /r "%CREO%\x86e_win64" pro_comm_msg.exe 2^>nul') do if not defined PRO_COMM_MSG_EXE set "PRO_COMM_MSG_EXE=%%p"
)
set "DIR_ICU="
for /f "delims=" %%f in ('where /r "%CREO%\x86e_win64" ucore*.dll 2^>nul') do if not defined DIR_ICU set "DIR_ICU=%%~dpf"
echo PRO_COMM_MSG_EXE=%PRO_COMM_MSG_EXE%
echo DLL de Creo en: %DIR_ICU%
if not defined DIR_ICU (echo No se encuentra ucore*.dll en "%CREO%\x86e_win64". & goto :fin)
set "PATH=%DIR_ICU%;%PATH%"
"%~dp0build\creo_iac_tk.exe" estado > "%~dp0build\estado.json" 2> "%~dp0build\estado_err.txt"
echo Codigo de salida: %ERRORLEVEL%
type "%~dp0build\estado.json"
type "%~dp0build\estado_err.txt"
:fin
endlocal
pause
