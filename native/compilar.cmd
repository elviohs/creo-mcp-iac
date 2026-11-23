@echo off
rem Compila creo_iac_tk.exe (Creo Toolkit en C, modo asincrono simple) con Build Tools 2022.
rem Resultado en native\build. No escribe nada en Program Files.
setlocal
set "CREO=C:\Program Files\PTC\Creo 12.4.1.0\Common Files"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set "PTK=%CREO%\protoolkit"
set "RAIZ=%~dp0"
set "OUT=%RAIZ%build"

if not exist "%OUT%" mkdir "%OUT%"
cd /d "%OUT%"
del /q creo_iac_tk.exe compilacion.log *.obj 2>nul
call "%VCVARS%" >nul || (echo No se pudo cargar el entorno de Visual Studio. & goto :fin)

echo Compilando... (detalle en native\build\compilacion.log)
cl /nologo /c /W3 /GS /fp:precise /MT /D_WSTDIO_DEFINED /DPRO_USE_VAR_ARGS /DPRO_MACHINE=36 /DPRO_OS=4 ^
   /I"%PTK%\includes" /I"%RAIZ%src" ^
   "%RAIZ%src\main.c" "%RAIZ%src\salida.c" "%RAIZ%src\win_utf8.c" > compilacion.log 2>&1 || goto :error

link /nologo /subsystem:console /machine:amd64 /debug:none /out:creo_iac_tk.exe ^
   main.obj salida.obj win_utf8.obj ^
   "%PTK%\x86e_win64\obj\protoolkit_NU.lib" "%PTK%\x86e_win64\obj\pt_asynchronous.lib" ^
   "%PTK%\x86e_win64\obj\ucore.lib" "%PTK%\x86e_win64\obj\udata.lib" ^
   libcmt.lib kernel32.lib user32.lib wsock32.lib advapi32.lib mpr.lib winspool.lib netapi32.lib ^
   psapi.lib gdi32.lib shell32.lib comdlg32.lib ole32.lib ws2_32.lib >> compilacion.log 2>&1 || goto :error

del /q *.obj 2>nul
echo COMPILACION CORRECTA: %OUT%\creo_iac_tk.exe
goto :fin

:error
echo ERROR en la compilacion. Ultimas lineas de compilacion.log:
powershell -NoProfile -Command "Get-Content '%OUT%\compilacion.log' -Tail 25"

:fin
endlocal
if "%1"=="" pause
