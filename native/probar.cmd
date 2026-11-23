@echo off
rem Ejecuta una orden de creo_iac_tk.exe contra la sesion de Creo abierta.
rem Uso: probar.cmd <orden> [--modelo NOMBRE.EXT] [--rapido]
rem      (sin argumentos: estado). La salida queda en native\build\<orden>.json
setlocal
set "CREO=C:\Program Files\PTC\Creo 12.4.1.0\Common Files"
set "ORDEN=%~1"
if "%ORDEN%"=="" set "ORDEN=estado"
if not defined PRO_COMM_MSG_EXE (
  for /f "delims=" %%p in ('where /r "%CREO%\x86e_win64" pro_comm_msg.exe 2^>nul') do if not defined PRO_COMM_MSG_EXE set "PRO_COMM_MSG_EXE=%%p"
)
set "DIR_ICU="
for /f "delims=" %%f in ('where /r "%CREO%\x86e_win64" ucore*.dll 2^>nul') do if not defined DIR_ICU set "DIR_ICU=%%~dpf"
if not defined DIR_ICU (echo No se encuentra ucore*.dll en "%CREO%\x86e_win64". & goto :fin)
set "PATH=%DIR_ICU%;%PATH%"
echo Ejecutando: creo_iac_tk %*
"%~dp0build\creo_iac_tk.exe" %ORDEN% %2 %3 %4 > "%~dp0build\%ORDEN%.json" 2> "%~dp0build\%ORDEN%_err.txt"
echo Codigo de salida: %ERRORLEVEL%
for %%A in ("%~dp0build\%ORDEN%.json") do echo Resultado: %%~fA (%%~zA bytes)
powershell -NoProfile -Command "$t = Get-Content -Raw '%~dp0build\%ORDEN%.json'; if ($t.Length -gt 1500) { $t.Substring(0,1500) + ' ...' } else { $t }"
type "%~dp0build\%ORDEN%_err.txt"
:fin
endlocal
pause
