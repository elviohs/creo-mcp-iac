@echo off
rem Sube creo-mcp-iac a GitHub (solo la primera vez). Sin el .exe (lo compila cada PC).
rem Antes: crea en github.com un repositorio VACIO llamado creo-mcp-iac
rem (sin README ni .gitignore) y pon tu usuario en la linea siguiente.
set "USUARIO=elviohs"
cd /d "%~dp0"
if "%USUARIO%"=="TU_USUARIO_DE_GITHUB" (echo Edita este fichero y pon tu usuario de GitHub. & pause & exit /b 1)
git --version >nul 2>&1 || (echo Falta Git: instalalo desde https://git-scm.com & pause & exit /b 1)
git config user.name >nul 2>&1 || (echo Configura antes tu nombre y correo de Git: & echo   git config --global user.name "Nombre" & echo   git config --global user.email "correo" & pause & exit /b 1)
if not exist .git git init -b main
git add -A
echo.
echo === Ficheros que se van a subir (revisa que no haya datos personales ni de proyectos) ===
git status --short
echo.
set /p OK=Escribe S para continuar: 
if /i not "%OK%"=="S" (echo Cancelado. & pause & exit /b 1)
git commit -m "creo-mcp-iac 0.11.0: lectura, BOM, masas, estandares, Creo Toolkit, taladros y patrones"
git remote remove origin >nul 2>&1
git remote add origin https://github.com/%USUARIO%/creo-mcp-iac.git
git push -u origin main
pause
