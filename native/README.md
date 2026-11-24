# creo_iac_tk — lecturas nativas y taladros en Creo para creo-mcp-iac

Programa auxiliar en C hecho con **Creo Toolkit** (modo asíncrono simple). Lo lanza
`creo-mcp-iac` para lo que CREOSON no hace. Se conecta a la sesión de Creo ya abierta,
ejecuta **una orden**, escribe el resultado en JSON (UTF-8) por la salida estándar y se
desconecta. Las órdenes son de lectura, salvo `crear` y `borrar` (0.3.x), que crean o borran
operaciones `IA_*` en una pieza en sesión. **Nunca guarda modelos.**

| Orden | Qué hace | Herramienta MCP |
|---|---|---|
| `estado` | Versión de Creo y modelo activo | (prueba) |
| `interferencias [--rapido]` | Análisis global de interferencias de Creo (`ProFitGlobalinterferenceCompute`): pares de piezas, ruta y volumen | `interferencias_creo` |
| `taladros` | Operaciones de taladro y roscas cosméticas de la pieza o de todas las piezas del conjunto: árbol de elementos, serie de rosca, tamaño de tornillo y eje | `taladros_creo` |
| `posiciones` | Componentes del conjunto con su ruta y su matriz 4×4; en las piezas, material (`ProMaterialCurrentGet`) y caja (`ProSolidOutlineGet`, coordenadas de la pieza) desde 0.6.0 | `taladros_conjugados`, `holguras_creo` |
| `holguras --pares F` | Distancia mínima entre pares de componentes (`ProFitClearanceCompute`) con los dos puntos más cercanos en coordenadas del conjunto; una línea por par, `ruta_a ruta_b` con ids separados por `/`. Si los puntos más cercanos están en caras cilíndricas, da su radio y su eje (`cilindro_a`, `cilindro_b`, desde 0.6.1) para los ajustes concéntricos. Se corta a los 540 s (`truncado`) | `holguras_creo` |
| `caras` | Caras planas de la pieza (id, normal exterior, punto, área, caja), planos de referencia y cuerpo por defecto | `caras_creo`, `proponer_taladros` |
| `seleccion` | Lo seleccionado en Creo: tipo, id, cara, coordenadas de un punto de referencia y punto pinchado | `seleccion_creo` |
| `crear --plan F` | **Escribe.** Crea UNA operación a partir de un plan (árbol de elementos). Comprueba que el eje pasa por el punto pedido; si no, la borra y prueba los otros signos de las cotas. Con `patron <id>` crea un patrón de esa operación (`ProPatternCreate`) y comprueba cada miembro (`esperado x y z`), probando los sentidos (`alternar_i`) | `crear_taladros`, `crear_patron` |
| `aristas` | Aristas de la pieza: id, recta/arco, longitud, extremos, punto medio, tangente, caras vecinas y forma (cóncava, convexa o tangente) | `aristas_creo`, `proponer_redondeos` |
| `arbol [--ids L]` | Operaciones de la pieza (id, nombre, tipo, estado, patrón) o el árbol de elementos de las indicadas y de su patrón | `operaciones_creo` |
| `borrar --ids 12,15` | **Escribe.** Borra operaciones cuyo nombre empieza por `IA_` y sin hijas ajenas; los patrones `IA_PAT_*` con `ProPatternDelete` (queda la guía) | `borrar_operaciones_ia` |

Opción común: `--modelo NOMBRE.EXT` (modelo ya en sesión); sin ella, el modelo activo.

## Licencia

Comprobado el 2026-11-22 en el PC del responsable (Creo 12.4.1.0 Educational):
- Creo Toolkit en C: se compila y carga **sin desbloquear**. El uso está permitido sin fines lucrativos (confirmado por el responsable).
- Object Toolkit C++: **no** sirve. Exige desbloqueo con la opción TOOLKIT-for-3D_Drawings, que no está en la licencia.

## Requisitos

- Creo Parametric 12.4.1.0 con **Creo Parametric Toolkit** instalado (instalador de Creo › Personalizar › Kits de herramientas de la API).
- **Build Tools de Visual Studio 2022** con «Desarrollo de escritorio con C++» (MSVC 14.4x, v143).

## Servicio local (obligatorio con Claude Desktop de la Tienda)

Claude Desktop, en su versión de la Tienda de Microsoft, lanza los programas dentro de su paquete. Desde ahí `creo_iac_tk.exe` no encuentra la sesión de Creo: devuelve -38 (ocupado) o -4 (no encontrada). Desde una ventana de comandos normal sí la encuentra (visto el 2026-11-22, con el mismo entorno de variables).

Por eso el servidor pide las órdenes a **`servicio_tk.py`**, que tienes que arrancar tú fuera de Claude, igual que CREOSON:

```
native\iniciar_servicio_tk.cmd
```

Deja la ventana abierta.
- Escucha solo en `127.0.0.1:9057`.
- Acepta solo las órdenes de lectura. Con `iniciar_servicio_tk.cmd --escritura` acepta además
  `crear` (plan validado línea a línea) y `borrar` (lista de ids). Sin esa opción responde 403.
- Atiende una petición cada vez.
- Deja una línea de registro por petición en `build\servicio_tk.log`.

Con `CREO_MCP_TOOLKIT_URL=directo`, el servidor lanza el programa él mismo, sin el servicio.

## Si Creo deja de responder (-38) también desde `probar.cmd`

Puede haberse quedado `nmsd`, el servicio de nombres de Creo, de una sesión anterior.
1. Cierra Creo.
2. Ejecuta `Stop-Process -Name nmsd` en PowerShell.
3. Vuelve a abrir Creo.

## Compilar

Doble clic en `compilar.cmd`. El resultado es `build\creo_iac_tk.exe` y el detalle queda en `build\compilacion.log`. Enlaza con `protoolkit_NU.lib` y `pt_asynchronous.lib`, en modo `/MT`.

## Probar sin el asistente

Abre Creo con un modelo y ejecuta, desde esta carpeta:

```
probar.cmd estado
probar.cmd taladros
probar.cmd interferencias --modelo CONJUNTO.ASM
```

Cada prueba guarda el resultado completo en `build\<orden>.json`.

`probar.cmd` prepara lo mismo que el servidor:
- `PRO_COMM_MSG_EXE` apunta a `Common Files\x86e_win64\obj\pro_comm_msg.exe`;
- `Common Files\x86e_win64\lib` va al principio del `PATH`, porque ahí están `ucore68.dll` y `udata68.dll`. Sin eso, Windows dice que falta `ucore68.dll`.

## Planes de `crear`

Texto, una instrucción por línea (lo genera `operaciones.py`; no se escribe a mano):

```
modelo PRUEBA_CMD.PRT
serie ISO
tamano M6x1
e 386.387 i 911                  # ruta de elementos (ProElemId.h) + tipo + valor
e 386.1340.1341 r 5 962          # referencia: tipo (5 superficie, 98 cuerpo) e id
e 386.1964 w IA_M6X1_143022_1    # nombre de la operación
verificar 10 20 5 0 1 0          # punto y dirección que debe tener el eje
alternar 386.1340.706 386.1340.708   # cotas cuyo signo se prueba si el eje no cae en su sitio
```

Tipos de valor: `i` entero, `d` real, `b` booleano, `w` texto, `r` referencia, `k` colección de
aristas (`k 1037 1038 …`, para redondeos). Las plantillas
salen de taladros hechos a mano en Creo 12.4.1.0 (PRUEBA_CMD.PRT, 2026-11-23).

## Notas

- Si hay más de una sesión de Creo abierta, el programa no elige ninguna y devuelve un error.
- `interferencias` usa el cálculo exacto por defecto. `--rapido` solo da los pares, sin volumen, y puede incluir contactos.
- `taladros` solo ve operaciones de taladro y roscas cosméticas. Los taladros hechos como extrusión o revolución no aparecen; para esos está la herramienta de tornillería con el STEP.
- Primera prueba real (2026-11-22): un conjunto de prueba dio 1 interferencia de 319,38 mm³.
