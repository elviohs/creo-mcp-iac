<#
  Arranque unico del entorno Creo + asistente (creo-mcp-iac), Departamento de Ingenieria Mecanica, IAC.

  Orden:
    1. Comprueba si Creo esta abierto. Si no, cierra un nmsd colgado de una sesion anterior
       y abre Creo 12.4.1.0 (igual que el acceso directo del departamento).
    2. Espera a que Creo acepte conexiones (creo_iac_tk.exe estado).
    3. Arranca CREOSON (puerto 9056) si no esta en marcha.
    4. Arranca el servicio de lecturas nativas (puerto 9057) si no esta en marcha; con
       $ServicioEscritura (o -Escritura) le permite crear taladros IA_* (nunca guarda).
    5. Comprueba las tres piezas y dice que falta, si falta algo.
  No abre ni guarda modelos. Se puede ejecutar varias veces: lo que ya esta en marcha no se toca.

  Uso:  iniciar_creo_iac.cmd            (doble clic)
        iniciar_creo_iac.cmd -CrearAcceso   (crea un acceso directo en el escritorio)
        iniciar_creo_iac.cmd -Escritura     (servicio con escritura aunque la configuracion diga que no)
#>
param([switch]$CrearAcceso, [switch]$Escritura)

$ErrorActionPreference = "Stop"
# --- configuracion -------------------------------------------------------------
$CreoExe     = "C:\Program Files\PTC\Creo 12.4.1.0\Parametric\bin\parametric.exe"
$CreoDirTrab = "E:\PTC_Trabajo"                             # carpeta de trabajo de Creo
$Common      = "C:\Program Files\PTC\Creo 12.4.1.0\Common Files"
$CreosonDir  = "E:\PTC\CreosonServerWithSetup-3.0.2-win64"
$McpDir      = "E:\PTC\creo-mcp-iac"
$PuertoCreoson = 9056
$PuertoServicio = 9057
$EsperaCreoS = 180                                         # 3 min
$ServicioEscritura = $false   # $true = el asistente puede crear taladros (ademas, CREO_MCP_ALLOW_OPERACIONES=1 en Claude)
# --------------------------------------------------------------------------------

$Tk   = Join-Path $McpDir "native\build\creo_iac_tk.exe"
$Py   = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$env:PRO_COMM_MSG_EXE = Join-Path $Common "x86e_win64\obj\pro_comm_msg.exe"
$env:PATH = (Join-Path $Common "x86e_win64\lib") + ";" + $env:PATH

function Paso($t)  { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Bien($t)  { Write-Host "   OK  $t" -ForegroundColor Green }
function Aviso($t) { Write-Host "   !!  $t" -ForegroundColor Yellow }
function Mal($t)   { Write-Host "   XX  $t" -ForegroundColor Red }

function Puerto-Abierto($p) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $p); $c.Close(); return $true }
    catch { return $false }
}
function Creo-Responde {
    if (-not (Test-Path $Tk)) { return $null }
    $salida = & $Tk estado 2>$null | Select-Object -Last 1
    try { return ($salida | ConvertFrom-Json) } catch { return $null }
}

if ($CrearAcceso) {
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Creo + asistente IAC.lnk"
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = Join-Path $McpDir "iniciar_creo_iac.cmd"
    $s.WorkingDirectory = $McpDir
    $s.IconLocation = "C:\Program Files\PTC\Creo 12.4.1.0\Parametric\install\nt\parametric.ico"
    $s.Description = "Abre Creo, CREOSON y el servicio de lecturas nativas para el asistente"
    $s.Save()
    Bien "Acceso directo creado: $lnk"
    exit 0
}

Write-Host "Arranque de Creo + asistente IAC" -ForegroundColor White

# 1. Creo ----------------------------------------------------------------------------
Paso "1/4 Creo Parametric"
$xtop = Get-Process xtop -ErrorAction SilentlyContinue
if ($xtop) {
    Bien "Creo ya esta abierto."
} else {
    $nmsd = Get-Process nmsd -ErrorAction SilentlyContinue
    if ($nmsd) {
        Aviso "Hay un nmsd de una sesion anterior sin Creo: se cierra para evitar el error -38."
        $nmsd | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    if (-not (Test-Path $CreoDirTrab)) { Aviso "No existe $CreoDirTrab; se crea."; New-Item -ItemType Directory $CreoDirTrab | Out-Null }
    Start-Process -FilePath $CreoExe -WorkingDirectory $CreoDirTrab
    Bien "Creo arrancando (puede tardar unos minutos con Windchill)..."
}

# 2. Esperar a Creo --------------------------------------------------------------------
Paso "2/4 Esperando a que Creo acepte conexiones"
if (-not (Test-Path $Tk)) {
    Aviso "No existe $Tk (compila native\compilar.cmd). Se espera solo a que aparezca Creo."
    $t0 = Get-Date
    while (-not (Get-Process xtop -ErrorAction SilentlyContinue) -and ((Get-Date) - $t0).TotalSeconds -lt $EsperaCreoS) { Start-Sleep 5 }
    Start-Sleep 30
} else {
    $t0 = Get-Date; $r = $null; $ultimo = $null
    while (((Get-Date) - $t0).TotalSeconds -lt $EsperaCreoS) {
        if (Get-Process xtop -ErrorAction SilentlyContinue) {
            $r = Creo-Responde
            if ($r -and $r.ok) { break }
            if ($r) { $ultimo = $r }
        }
        Write-Host "   ...  $([int]((Get-Date) - $t0).TotalSeconds) s" -NoNewline; Write-Host "`r" -NoNewline
        Start-Sleep -Seconds 10
    }
    Write-Host ""
    if ($r -and $r.ok) {
        $m = if ($r.modelo_activo) { "$($r.modelo_activo.nombre) ($($r.modelo_activo.tipo))" } else { "sin modelo activo" }
        Bien "Creo responde (version $($r.creo_version_mayor); $m)."
    } elseif ($ultimo -and $ultimo.codigo -eq -38) {
        Mal "Creo no responde (-38). Cierra Creo, ejecuta 'Stop-Process -Name nmsd' en PowerShell y vuelve a lanzar este arranque."
    } elseif ($ultimo -and $ultimo.codigo -eq -36) {
        Mal "Hay mas de una sesion de Creo abierta: deja solo una."
    } else {
        Mal "Creo no ha respondido en $EsperaCreoS s. Si sigue cargando, vuelve a lanzar este arranque cuando termine."
    }
}

# 3. CREOSON ----------------------------------------------------------------------------
Paso "3/4 CREOSON (puerto $PuertoCreoson)"
if (Puerto-Abierto $PuertoCreoson) {
    Bien "CREOSON ya esta en marcha."
} else {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/k title CREOSON (no cerrar) && creoson_run.bat" `
        -WorkingDirectory $CreosonDir -WindowStyle Minimized
    $t0 = Get-Date
    while (-not (Puerto-Abierto $PuertoCreoson) -and ((Get-Date) - $t0).TotalSeconds -lt 60) { Start-Sleep 2 }
    if (Puerto-Abierto $PuertoCreoson) { Bien "CREOSON arrancado (ventana minimizada 'CREOSON (no cerrar)')." }
    else { Mal "CREOSON no ha abierto el puerto en 60 s. Prueba con CreosonSetup > Start CREOSON." }
}

# 4. Servicio de lecturas nativas ------------------------------------------------------
Paso "4/4 Servicio de lecturas nativas (puerto $PuertoServicio)"
$conEscritura = $ServicioEscritura -or $Escritura
function Escritura-Servicio {
    try { return (Invoke-RestMethod -Uri "http://127.0.0.1:$PuertoServicio/estado" -TimeoutSec 5).escritura }
    catch { return $null }
}
if (Puerto-Abierto $PuertoServicio) {
    $e = Escritura-Servicio
    Bien ("El servicio ya esta en marcha (" + $(if ($e) { "con escritura" } else { "solo lectura" }) + ").")
    if ($conEscritura -and -not $e) {
        Aviso "Para permitir crear taladros, cierra la ventana del servicio y vuelve a lanzar este arranque."
    }
} else {
    $args4 = if ($conEscritura) { "servicio_tk.py --escritura" } else { "servicio_tk.py" }
    Start-Process -FilePath "cmd.exe" -ArgumentList "/k title Servicio lecturas nativas Creo (no cerrar) && `"$Py`" $args4" `
        -WorkingDirectory (Join-Path $McpDir "native") -WindowStyle Minimized
    $t0 = Get-Date
    while (-not (Puerto-Abierto $PuertoServicio) -and ((Get-Date) - $t0).TotalSeconds -lt 20) { Start-Sleep 1 }
    if (Puerto-Abierto $PuertoServicio) {
        Bien ("Servicio arrancado (ventana minimizada; " + $(if ($conEscritura) { "con escritura" } else { "solo lectura" }) + ").")
    }
    else { Mal "El servicio no ha abierto el puerto. Ejecuta native\iniciar_servicio_tk.cmd para ver el error." }
}

# Resumen --------------------------------------------------------------------------------
Paso "Resumen"
$ok = (Get-Process xtop -ErrorAction SilentlyContinue) -and (Puerto-Abierto $PuertoCreoson) -and (Puerto-Abierto $PuertoServicio)
if ($ok) { Bien "Todo listo. Ya puedes usar el asistente (reinicia Claude solo si se actualizo el servidor)." }
else     { Aviso "Falta alguna pieza: revisa los mensajes de arriba." }
Write-Host ""
Write-Host "No cierres las ventanas minimizadas de CREOSON y del servicio mientras uses el asistente."
