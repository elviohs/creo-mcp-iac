# Instalación de creo-mcp-iac en otro PC del departamento

Servidor MCP del Departamento de Ingeniería Mecánica del IAC para que Claude Desktop trabaje con
Creo Parametric 12.4.1.0. Uso interno y sin fines lucrativos: la licencia Educational de Creo
Toolkit no permite otro uso. El detalle de cada herramienta está en `README.md`.

## 1. Requisitos

- Windows con **Creo Parametric 12.4.1.0** instalado en `C:\Program Files\PTC\Creo 12.4.1.0`.
  Si la ruta es otra, cámbiala en el paso 5 y en `CREO_MCP_CREO_COMMON`.
- **Python 3.13** (`%LOCALAPPDATA%\Programs\Python\Python313`), instalado como usuario normal.
- **CREOSON 3.0.2** (MIT): <https://github.com/SimplifiedLogic/creoson/releases>.
- **Claude Desktop** con acceso al proyecto del departamento.
- **Git** para descargar el repositorio, y el repositorio `elviohs/creo-mcp-iac`.
- **El programa nativo `creo_iac_tk.exe` no está en el repositorio** (enlaza librerías de PTC con
  licencia Educational, que no se redistribuyen). Dos opciones:
  - compilarlo (paso 3 bis): hacen falta **Creo Parametric Toolkit** (instalador de Creo ›
    Personalizar › Kits de herramientas de la API) y **Build Tools de Visual Studio 2022** con
    «Desarrollo de escritorio con C++»;
  - o pedírselo al responsable y copiarlo en `native\build\creo_iac_tk.exe`.
  Sin él funcionan las herramientas de CREOSON, pero no las nativas (interferencias, taladros,
  patrones, redondeos, chaflanes, taladros conjugados y holguras).
  Versión mínima: **`creo_iac_tk` 0.6.1** (la necesitan `holguras_creo` y `taladros_conjugados`).
  Un `.exe` anterior sigue sirviendo para el resto, pero hay que recompilarlo o pedir uno nuevo.

## 2. Carpetas

```
E:\PTC\
├── CreosonServerWithSetup-3.0.2-win64\   CREOSON
├── creo-mcp-iac\                         este repositorio
├── export\                               Excel, STEP y PDF que genera el asistente
└── logs\                                 audit.jsonl
E:\PTC_Trabajo\                           carpeta de trabajo de Creo
```

Si el PC no tiene disco `E:`, usa otra unidad y cambia las rutas en los pasos 4, 5 y 6.
Creo debe poder escribir en `export` (en un PC del departamento no podía en `E:\PTC_Trabajo`).

## 3. Descargar e instalar

En una PowerShell **normal** (no de administrador):

```powershell
cd E:\PTC
git clone https://github.com/elviohs/creo-mcp-iac.git
cd creo-mcp-iac
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
mkdir E:\PTC\export, E:\PTC\logs
```

Para actualizar más adelante: `git pull` en esa carpeta y reiniciar Claude.

## 3 bis. Programa nativo

Doble clic en `native\compilar.cmd`. Debe terminar con «COMPILACION CORRECTA» y dejar
`native\build\creo_iac_tk.exe`. La versión del programa sale en `creo_capacidades` y en la
`fuente` de las herramientas nativas (`creo_iac_tk 0.6.1`). Si el enlazador dice «no se puede escribir el archivo», cierra la
ventana del servicio de lecturas nativas y repite. Vuelve a compilar cada vez que `git pull` traiga
cambios en `native\src`.

## 4. CREOSON

Descomprime CREOSON en `E:\PTC`. En `CreosonSetup` indica la ruta de Creo 12 y el puerto 9056.

## 5. Arranque único

Abre `iniciar_creo_iac.ps1` y revisa el bloque de configuración del principio (rutas de Creo,
CREOSON, este repositorio y carpeta de trabajo). Después crea el acceso directo del escritorio:

```
iniciar_creo_iac.cmd -CrearAcceso
```

Ese acceso directo abre Creo, espera a que responda (hasta 3 min), arranca CREOSON y el servicio
de lecturas nativas, y dice qué falta si algo falla. Deja abiertas las ventanas minimizadas de
CREOSON y del servicio mientras uses el asistente.

## 6. Claude Desktop

En `claude_desktop_config.json`, dentro de `"mcpServers"`, añade (cuidado con las comas):

```json
"creo-iac": {
  "command": "E:\\PTC\\creo-mcp-iac\\.venv\\Scripts\\creo-mcp-iac.exe",
  "env": {
    "CREO_MCP_EXPORT_DIR": "E:\\PTC\\export",
    "CREO_MCP_AUDIT_LOG": "E:\\PTC\\logs\\audit.jsonl"
  }
}
```

Reinicia Claude. Prueba: pide al asistente «comprueba la conexión con Creo» (`creo_estado`) y
«¿qué puedes hacer en Creo?» (`creo_capacidades`).

## 7. Escritura en Creo (opcional, desactivada por defecto)

Por defecto el asistente solo lee y exporta ficheros nuevos. Cada escritura se activa aparte en
`claude_desktop_config.json`, se confirma con una tabla y **nunca guarda** (guardas tú en Creo y
registras en Windchill). El arranque único ya abre el servicio con escritura
(`$ServicioEscritura = $true`); el interruptor que decide es la variable de Claude:

| Qué | Cómo se activa |
|---|---|
| Parámetros del cajetín desde el Excel del BOM | `"CREO_MCP_ALLOW_PARAMETERS": "1"` en `env` |
| Taladros, cajeados, avellanados, patrones, redondeos y chaflanes | `"CREO_MCP_ALLOW_OPERACIONES": "1"` en `env` (el servicio ya arranca con escritura; para solo lectura, `$ServicioEscritura = $false` en `iniciar_creo_iac.ps1`) |

Prueba primero las escrituras con una pieza de prueba que no esté en Windchill. Lo creado queda en
la sesión de Creo con nombre `IA_*`: si guardas la pieza, se guarda con ella.

## 7 bis. Holguras en frío

`holguras_creo` usa la tabla de contracción `src\creo_mcp_iac\contraccion_iac.json` (datos del
departamento: NIST de la calculadora y librería ANSYS v4). Para usar otra tabla, añade
`"CREO_MCP_CONTRACCION": "<ruta del .json>"` en `env`. Los materiales que no están en la tabla se
quedan sin cálculo en frío; se pueden indicar pieza a pieza al pedir el cálculo.

## 8. Problemas frecuentes

- **Creo no responde (-38)**: cierra Creo, ejecuta `Stop-Process -Name nmsd` en PowerShell y vuelve
  a abrir con el acceso directo (el arranque único ya lo hace si Creo está cerrado).
- **«El servicio de lecturas nativas no está arrancado»**: falta la ventana del servicio; vuelve a
  lanzar el arranque único.
- **Falta `ucore68.dll`**: la ruta de Creo no es la estándar; ajusta `CREO_MCP_CREO_COMMON`.
- **No instales paquetes desde una PowerShell de administrador** en `.venv`: deja `site-packages`
  ilegible. Arreglo: borra `.venv` y repite el paso 3.
