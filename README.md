# creo-mcp-iac

Servidor [MCP](https://modelcontextprotocol.io) para que un asistente de IA (Claude Desktop) trabaje
con **Creo Parametric 12** en el Departamento de Ingeniería Mecánica del IAC.

Dos vías de acceso a la sesión de Creo que ya tiene abierta el usuario:

```
Asistente ──stdio──▶ creo-mcp-iac ──HTTP localhost──▶ CREOSON 3.0.2 (J-Link) ──▶ Creo
                                  └─HTTP localhost──▶ servicio_tk ──▶ creo_iac_tk.exe (Creo Toolkit en C) ──▶ Creo
```

- **CREOSON** (creopyson): lectura de modelos, planos, BOM y masas; exportación STEP/PDF.
- **Programa propio en C con Creo Toolkit** (`native/`): lo que CREOSON no hace — interferencias
  nativas, operaciones de taladro, caras, aristas y selección, y creación de taladros, patrones,
  redondeos, chaflanes, extrusiones y cortes con croquis.

Versión 0.22.0. Instalación: [`INSTALACION.md`](INSTALACION.md). Programa nativo:
[`native/README.md`](native/README.md).

## Principios

- **Solo trabaja con lo que ya está abierto en Creo.** No abre, guarda ni cierra modelos.
- **Nunca guarda** y no tiene acceso a Windchill: el usuario revisa en Creo, guarda y registra.
- **Lectura por defecto.** Cada escritura se activa aparte en la configuración y siempre sigue el
  mismo flujo: propuesta con tabla → confirmación expresa del usuario → escritura verificada.
- Las operaciones que crea llevan nombre `IA_*` y solo se pueden borrar esas.
- **Auditoría local** (`audit.jsonl`) de cada llamada; los nombres de personas de los parámetros se
  ocultan por defecto.
- Red solo local (CREOSON y el servicio en `127.0.0.1`).

## Herramientas

| Herramienta | Qué hace | Escribe |
|---|---|---|
| `creo_estado`, `creo_capacidades` | Conexión, modelo activo, herramientas y permisos activos | No |
| `listar_modelos`, `info_modelo` | Modelos en sesión; unidades, material, tabla de familia | No |
| `leer_parametros`, `leer_cotas`, `leer_notas` | Parámetros, cotas con tolerancia normalizada, notas con símbolos | No |
| `lista_materiales`, `listar_caracteristicas` | BOM indentada y agregada; árbol de operaciones | No |
| `propiedades_masa` | Masa, CDG e inercias (unidades del modelo y SI), comprobación del parámetro `MASA` | No |
| `info_plano`, `vistas_modelo`, `config_creo`, `listar_plantillas_plano` | Hojas, vistas, símbolos, plantillas | No |
| `exportar_step`, `exportar_pdf` | STEP o PDF a la carpeta autorizada | Fichero nuevo |
| `exportar_bom_excel` | BOM a Excel con tornillería y avisos del cajetín | Fichero nuevo |
| `revisar_parametros_excel` / `aplicar_parametros_excel` | Vuelta a Creo de los parámetros editados en el Excel (con código de confirmación) | Parámetros, sin guardar |
| `presupuesto_masas` | Presupuesto de masas del conjunto a Excel | Fichero nuevo |
| `revisar_estandares` | Reglas del departamento (parámetros, códigos, materiales para vacío y criogenia…) a Excel | Fichero nuevo |
| `interferencias_creo` | Interferencias nativas del conjunto; separa tornillería y pasadores | Fichero nuevo (opcional) |
| `taladros_creo` | Taladros y roscas cosméticas: tipo, rosca, tamaño, profundidades, avellanados, eje | Fichero nuevo (opcional) |
| `caras_creo`, `seleccion_creo`, `operaciones_creo` | Caras planas y referencias; lo seleccionado en Creo; operaciones y patrones | No |
| `proponer_taladros` / `crear_taladros` | Taladros rectos y normalizados ISO (roscado, holgura, taladrado), con cajeado o avellanado | Operaciones `IA_*`, sin guardar |
| `proponer_patron` / `crear_patron` | Patrones lineales (1 o 2 direcciones) o circulares de un taladro o, desde 0.22.0, de un corte, extrusión, redondeo o chaflán | Patrón `IA_PAT_*`, sin guardar |
| `aristas_creo` | Aristas con sus caras, punto medio, dirección y si son cóncavas (esquina interior) o convexas | No |
| `proponer_redondeos` / `crear_redondeos` | Redondeo de radio constante en las aristas elegidas (p. ej. esquinas interiores que la fresa no puede dejar vivas) | Redondeo `IA_RED_*`, sin guardar |
| `proponer_chaflanes` / `crear_chaflanes` | Chaflán 45×D, D×D, D1×D2 o ángulo×D (estos dos con una cara de referencia común a las aristas) en las aristas elegidas (romper aristas vivas, entradas de taladros y ejes) | Chaflán `IA_CHF_*`, sin guardar |
| `proponer_extrusion` / `crear_extrusion` | Extrusión (añade material) o corte (quita) con un croquis de círculos, rectángulos, ranuras y perfiles de rectas y arcos, en un plano o cara normal a X, Y o Z; ciega, simétrica o pasante | Operación `IA_EXT_*` o `IA_CORTE_*`, sin guardar |
| `crear_pieza` | Pieza nueva y vacía en la sesión, copia de la plantilla del IAC, abierta en una ventana | Pieza nueva en sesión, sin guardar ni registrar |
| `proponer_revolucion` / `crear_revolucion` | Revolución (pieza torneada) o corte por revolución: media sección a un lado de un eje de giro paralelo a X, Y o Z, con cotas radiales como diámetros; 360° o un ángulo simétrico | Operación `IA_REV_*` o `IA_CORTE_REV_*`, sin guardar |
| `taladros_conjugados` | Taladros enfrentados de piezas distintas en un conjunto: alineación frente a la holgura del tornillo y tamaños compatibles (roscado M6 con holgura M6) | Solo un fichero nuevo |
| `holguras_creo` | Holguras mínimas entre piezas (distancia exacta de Creo) y estimación en frío por contracción de los materiales | Solo un fichero nuevo |
| `borrar_operaciones_ia` | Deshace operaciones, patrones, redondeos, chaflanes, extrusiones y cortes `IA_*` | Borra `IA_*`, sin guardar |
| `resultado_tarea`, `listar_tareas` | Recoger el resultado de herramientas largas | No |

Las herramientas largas (exportaciones, BOM, masas, estándares, interferencias, taladros) responden
«en_curso» si pasan de ~55 s y su resultado se recoge con `resultado_tarea`.

## Taladros y patrones

1. **Dónde:** el usuario selecciona una cara en Creo (`seleccion_creo`) o se indica por orientación
   (`+Y` = la cara más exterior hacia +Y).
2. **Propuesta** (`proponer_taladros`, `proponer_patron`): no toca Creo. Calcula las cotas desde los
   planos de referencia de la plantilla (PERFIL, ALZADO, PLANTA), toma broca, rosca, holgura,
   cajeado y avellanado de la tabla `ISO.hol` de Creo, comprueba que todo cae dentro de la cara y
   que nada se pisa, y devuelve una tabla y un código (vale 30 min, una vez).
3. **Confirmación** del usuario sobre la tabla.
4. **Creación** (`crear_taladros`, `crear_patron`): vuelve a leer la pieza (si ha cambiado, no crea
   nada), crea la operación y comprueba que cada eje queda en el punto calculado; si no, la borra y
   prueba el otro sentido de las cotas o direcciones.

Límites: piezas en mm, caras planas, colocación lineal, serie ISO. No es IFX: no taladra el resto
del conjunto ni coloca tornillería.

## Redondeos para fabricar

Solo se redondea donde lo pide la fabricación. `aristas_creo` marca cada arista como cóncava
(esquina interior) o convexa; las cóncavas paralelas al eje de la fresa (filtro `direccion`) son las
que la fresa no puede dejar vivas. El fondo de una cajera no necesita radio con fresa plana. Las
esquinas que señale una revisión de fabricabilidad se localizan por su punto medio.
`proponer_redondeos` prepara la tabla con las aristas elegidas y el radio (mayor que el de la
fresa; lo decide el usuario) y `crear_redondeos` lo crea tras la confirmación, comprobando
releyendo la pieza que esas aristas han desaparecido.

Los chaflanes siguen el mismo camino: `proponer_chaflanes` (esquema 45×D —el habitual, «Ch
D×45°»—, D×D, D1×D2 o ángulo×D; normalmente en aristas exteriores, avisa si alguna es interior) y
`crear_chaflanes`. En D1×D2 y ángulo×D hace falta `cara_referencia`, una de las dos caras de las
aristas (común a todas): D1, o D, se mide sobre ella y el ángulo se mide desde ella.

Los patrones (`proponer_patron`) también repiten, desde 0.22.0, cortes, extrusiones, redondeos y
chaflanes (`guia` = id de `operaciones_creo`). Como no tienen eje, `creo_iac_tk` 0.11.0 sitúa cada
miembro por un punto (centro de la caja de sus superficies en los lineales; punto medio de su
primera superficie, que gira con él, en los circulares) y lo compara con el de la guía desplazado o
girado; si no casa, prueba el otro sentido de las direcciones. Un patrón de redondeos o chaflanes
solo sale si en cada posición hay una arista equivalente.

## Piezas desde un boceto: extrusiones y cortes

`proponer_extrusion` prepara una extrusión (`tipo="anadir"`) o un corte (`tipo="quitar"`) con un
croquis en un plano de referencia o en una cara plana normal a X, Y o Z. Las coordenadas del
croquis son las de la pieza en ese plano, en orden X, Y, Z (plano normal a Z: `[x, y]`; a Y:
`[x, z]`; a X: `[y, z]`). Contornos: `circulo`, `rectangulo` (con radio en las esquinas),
`ranura` (coliso) y `perfil` (rectas y arcos por tres puntos o por centro y giro). Un contorno
dentro de otro es un hueco. Extensión ciega (con sentido), simétrica, pasante o pasante a los dos
lados (estas dos solo para cortes). Calcula el área neta y el volumen teórico y rechaza contornos
que se cortan.

`crear_extrusion` crea la operación en dos pasos (`ProFeatureWithoptionsCreate` incompleta, croquis
con `ProSectionEntityAdd` + acotado automático, y `ProFeatureWithoptionsRedefine`) y comprueba que
las caras de la operación quedan al lado pedido del plano y que el volumen cambia como debe (exacto
en la primera extrusión de una pieza vacía; como mucho área × profundidad después). Si no, la borra
y prueba el otro sentido.

Para levantar una pieza nueva: `crear_pieza` (0.20.0) crea en la sesión una pieza vacía con la
plantilla del IAC (`template_solidpart` de Creo, que está en Windchill: se lee a través de Creo, solo
lectura, por decisión del responsable del 2026-11-26), con el nombre que confirme el usuario, y la abre
en una ventana; no la guarda ni la registra. También vale una pieza vacía que haya abierto el usuario. Se
hace el contorno exterior y después cortes, taladros, redondeos y chaflanes, cada uno con su tabla y su confirmación. De un boceto o una foto solo se toman las cotas que el usuario
confirma.

Desde 0.18.0 (`creo_iac_tk` 0.8.0) el croquis se acota con las **cotas del boceto**, como lo haría un
delineante: diámetros y radios, posición de centros y vértices desde los planos de referencia, ancho
y alto de los rectángulos. Para no chocar con las restricciones que Creo pone solo (radios iguales,
concéntricos, alineados, tangentes), cada radio y cada coordenada se acota una vez y no se acotan
los puntos de tangencia ni lo que cae sobre una referencia; lo que falte lo completa el acotado
automático. Si aun así chocan, se quitan y el croquis queda con el acotado automático (como en
0.17), y la respuesta lo dice (`cotas_croquis`). La respuesta trae las cotas que quedaron en Creo
(las del boceto marcadas) y la desviación de la geometría resuelta frente a la pedida
(`desviacion_croquis_mm`). Los arcos por tres puntos redondean centro y radio a la milésima si
están a menos de 5·10⁻⁵ (puntos de tangencia dados con decimales).

### Caras oblicuas (0.21.0)

El croquis de una extrusión o un corte puede ir en una **cara plana oblicua** (no normal a X, Y o Z),
por su id. Hace falta un plano de referencia o una cara perpendicular a ella (en una rampa inclinada
alrededor de X, PERFIL), que orienta el croquis y mide la coordenada **u** (distancia a ese plano);
**v** es la distancia a lo largo de la cara desde su arista recta paralela a ese plano (la de abajo
según el sentido de v, que va hacia donde crece la coordenada dominante). Las aristas rectas de la cara
se proyectan como referencias del croquis, así que las cotas del boceto se ponen desde el plano y desde
la arista. La tabla da el origen y los ejes u, v en coordenadas de la pieza. El sentido es `fuera`
(hacia el exterior de la cara) o `dentro` (hacia el material) y se comprueba por el cambio de volumen
(no hay tramo a lo largo de un eje). `fuera`/`dentro` también valen en caras normales a un eje. Una
cara inclinada respecto a dos ejes necesita un plano de referencia perpendicular creado a mano. Las
revoluciones siguen en planos normales a X, Y o Z.

### Revoluciones (0.19.0)

`proponer_revolucion` prepara una pieza torneada (eje, casquillo, brida, polea) o un corte por
revolución (garganta, cajeado interior): el croquis es **media sección** a un lado del eje de giro,
en un plano que lo contenga (plano de referencia o cara), con los mismos contornos que las
extrusiones. El eje de giro es una línea de centros del croquis paralela a X, Y o Z (`eje`), que
pasa por el origen o a `eje_desplazado` de él. Las cotas perpendiculares al eje son **diámetros**
respecto a la línea de centros, como en un plano de torno, y las axiales salen del plano de
referencia. 360° (vuelta entera) o un ángulo simétrico respecto al plano del croquis. El volumen
teórico sale del teorema de Pappus y se comprueba al crear (exacto en la primera operación; en un
corte, que quite material y como mucho ese volumen). No se comprueba el tramo a lo largo del eje:
un corte por revolución parte las caras cilíndricas y Creo da los trozos nuevos a la operación. `crear_revolucion` la crea tras la confirmación (`IA_REV_*`,
`IA_CORTE_REV_*`), sin guardar.

## Holguras y criogenia

`holguras_creo` filtra los pares de piezas por sus cajas, pide a Creo la distancia mínima exacta
(`ProFitClearanceCompute`) y marca contactos, interferencias y holguras por debajo de
`holgura_minima`. Con `temperatura` (K) estima la holgura en frío: cada pieza se contrae con la
contracción integrada de su material desde 293 K (`contraccion_iac.json`, editable; otra tabla
con `CREO_MCP_CONTRACCION`) alrededor de un punto fijo, que puede ser el centro de la cara
seleccionada en Creo (`fijo_en_seleccion`). Los ajustes concéntricos (eje dentro de agujero) se
calculan con la contracción de cada radio, sin depender del punto fijo. Es una estimación de
primer orden: no sustituye a un análisis termoelástico.

## Configuración

Variables de entorno del servidor (bloque `env` en `claude_desktop_config.json`):

| Variable | Por defecto | Uso |
|---|---|---|
| `CREO_MCP_EXPORT_DIR` | *(vacía = sin exportar)* | Carpeta autorizada para Excel, STEP y PDF (Creo debe poder escribir en ella) |
| `CREO_MCP_AUDIT_LOG` | `~/.creo_mcp_iac/audit.jsonl` | Registro de auditoría |
| `CREO_MCP_ALLOW_PARAMETERS` | `0` | `1` = activa `aplicar_parametros_excel` |
| `CREO_MCP_ALLOW_OPERACIONES` | `0` | `1` = activa `crear_taladros`, `crear_patron`, `crear_redondeos`, `crear_chaflanes`, `crear_extrusion`, `crear_revolucion`, `crear_pieza` y `borrar_operaciones_ia` (además, el servicio con `--escritura`). Es el interruptor principal de la escritura |
| `CREO_MCP_ALLOW_DRAWINGS` | `0` | `1` = herramientas experimentales de planos nativos (no recomendadas) |
| `CREO_MCP_CONTRACCION` | `contraccion_iac.json` del paquete | Tabla de contracción térmica (ΔL/L desde 293 K) de `holguras_creo` |
| `CREO_MCP_ESTANDARES` | `estandares_iac.json` del paquete | Fichero de reglas de `revisar_estandares` (los formatos de código de plano de cada proyecto van aquí) |
| `CREO_MCP_CREO_COMMON` | `C:\Program Files\PTC\Creo 12.4.1.0\Common Files` | De aquí salen `pro_comm_msg.exe` y las DLL de Creo |
| `CREO_MCP_HOL_DIR` | `<Common Files>\text\hole` | Tablas de taladros de Creo (`ISO.hol`) |
| `CREO_MCP_TOOLKIT_URL` | `http://127.0.0.1:9057` | Servicio local del programa nativo (`directo` = sin servicio) |
| `CREO_MCP_TOOLKIT_EXE` | `native\build\creo_iac_tk.exe` | Programa nativo; `0` lo desactiva |
| `CREOSON_HOST` / `CREOSON_PORT` | `localhost` / `9056` | CREOSON |
| `CREO_MCP_ESPERA` | `55` | Segundos antes de responder «en_curso» |
| `CREO_MCP_TIMEOUT`, `CREO_MCP_MAX_ITEMS`, `CREO_MCP_REDACT_NAMES`, `CREO_MCP_CREO_VERSION`, `CREO_MCP_ALLOW_LAN` | | Ver `src/creo_mcp_iac/config.py` |

¿Por qué un servicio aparte? Claude Desktop de la Tienda de Microsoft lanza los programas dentro de
su paquete y desde ahí `creo_iac_tk.exe` no encuentra la sesión de Creo. `native/servicio_tk.py`
se arranca fuera de Claude (lo hace el arranque único `iniciar_creo_iac.cmd`), escucha solo en
`127.0.0.1` y solo acepta las órdenes conocidas; las de escritura, solo si se arranca con
`--escritura`. El arranque único lo hace con escritura por defecto (`$ServicioEscritura = $true` en
`iniciar_creo_iac.ps1`), porque nada se guarda y cada cambio exige confirmación; mientras
`CREO_MCP_ALLOW_OPERACIONES` no valga `1`, el asistente no puede escribir aunque el servicio lo admita.
Para arrancarlo en solo lectura, `$ServicioEscritura = $false`.

## Licencia de Creo Toolkit

El programa nativo usa Creo Toolkit en C, disponible sin desbloqueo con la licencia Educational de
Creo 12 y usado por el departamento **sin fines lucrativos**. El repositorio no incluye el
ejecutable ni ningún fichero de PTC: cada instalación lo compila (`native\compilar.cmd`) con su
propio Creo Parametric Toolkit. Object Toolkit C++ no se usa (exige una opción de licencia que el
departamento no tiene).

## Pruebas

```bash
pip install -e ".[test]"
pytest -q
```

197 pruebas sin Creo: CREOSON simulado (`tests/fake_creoson.py`) y el programa nativo simulado. Comprueban,
entre otras cosas, que ninguna sesión de lectura envía escrituras, que las funciones peligrosas se
bloquean, que la exportación nunca sobrescribe y que sin los permisos no se puede escribir en Creo.

## Ampliar

Una lectura nueva por CREOSON: función de creopyson en `ALLOWED` (`gateway.py`, solo `get_*`,
`list*`, `is_*`), herramienta en `server.py` con `annotations=READ`, respuesta en el simulado y una
prueba. Una orden nativa nueva: `native/src/main.c`, lista `ORDENES` de `nativo.py` y de
`servicio_tk.py`. Cualquier escritura en Creo necesita una decisión explícita del responsable.

Código escrito desde cero. Dependencias: `mcp` (MIT), `creopyson` (MIT), `requests` (Apache-2.0).
