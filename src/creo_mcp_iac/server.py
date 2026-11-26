"""Servidor MCP de solo lectura para Creo Parametric (vía creopyson + CREOSON)."""

from __future__ import annotations

import base64
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from . import bom_excel
from . import estandares as estandares_mod
from . import masas as masas_mod
from . import xlsx as xlsx_mod
from .bom import build_bom
from .conjugados import revisar_conjugados
from .extrusiones import planificar_extrusion, planificar_revolucion
from .holguras import (buscar_material, candidatos, cargar_tabla, contraccion, piezas, punto_de_seleccion,
                       revisar_holguras)
from .config import Settings
from .formato import add_dim_text, blank, check_mass_param, clean_params, massprops_si, note_has_person, note_text
from .gateway import CAJETIN_PARAMS, CONFIG_OPTIONS, CreoGateway, CreoUnavailableError, NotAllowedError, ScopeError
from .nativo import (Nativo, NativoError, NativoSinRespuesta, resumir_interferencias, resumir_operacion,
                     resumir_taladros)
from .nativo import nombre_fichero as nombre_fichero_nat
from .operaciones import (PlanError, huella, huella_aristas, leer_hol, nombre_operacion, planificar_chaflan,
                          planificar_patron, planificar_redondeo, planificar_taladro, resumir_aristas, resumir_caras, resumir_seleccion)
from .tareas import GestorTareas

INSTRUCTIONS = """\
Servidor de SOLO LECTURA sobre Creo Parametric (IAC, Departamento de Ingeniería Mecánica).
- Solo lee modelos que ya están abiertos en la sesión de Creo; no abre, guarda,
  modifica, regenera ni cierra nada, y no tiene acceso a Windchill.
- La única escritura posible es exportar un STEP nuevo a la carpeta autorizada
  (si está configurada). Pide confirmación al usuario antes de exportar.
- Todo lo que generes a partir de estos datos es un BORRADOR que debe revisar
  una persona. Indica siempre de qué modelo y herramienta sale cada dato.
- Unidades: consulta `info_modelo` o `propiedades_masa`; no las supongas.
- Al empezar una tarea, `creo_capacidades` dice qué herramientas y permisos hay en esta
  instalación. Si una escritura devuelve «resultado_desconocido», no repitas la escritura:
  pide al usuario que compruebe Creo y vuelve a revisar antes de nada.
- `interferencias_creo` y `taladros_creo` leen de Creo de forma nativa (Creo Toolkit, programa
  creo_iac_tk.exe): interferencias del conjunto sin exportar STEP y operaciones de taladro con su
  rosca normalizada. Son de lectura; en conjuntos grandes tardan minutos.
- `taladros_conjugados` comprueba en un conjunto que los taladros enfrentados de piezas distintas
  están alineados (descentrado frente a la holgura del tornillo) y casan de tamaño (roscado M6 con
  holgura M6). Útil antes de fabricar o al revisar un conjunto.
- `holguras_creo` da las holguras mínimas entre piezas de un conjunto (distancia exacta de Creo)
  y, con `temperatura`, una estimación en frío por contracción diferencial de los materiales
  (tabla contraccion_iac.json). La estimación en frío depende del punto fijo: dilo siempre.
- Las herramientas largas (exportar_step, exportar_bom_excel, presupuesto_masas,
  revisar_estandares, interferencias_creo, taladros_creo, taladros_conjugados, holguras_creo, crear_taladros)
  responden
  «en_curso» con un
  `tarea_id` si tardan más de ~55 s: recoge el resultado con `resultado_tarea`; no las repitas.
- `exportar_bom_excel` escribe un Excel nuevo en la carpeta de exportación (no toca
  Creo). Si `aplicar_parametros_excel` está activa (CREO_MCP_ALLOW_PARAMETERS=1), solo
  la uses tras enseñar al usuario la tabla de `revisar_parametros_excel` y recibir su
  confirmación expresa; cambia parámetros del cajetín en sesión y no guarda.
- Redondeos: `aristas_creo` (aristas cóncavas = esquinas interiores) → `proponer_redondeos` →
  confirmación → `crear_redondeos`. Útil para corregir esquinas vivas que señale la revisión de
  fabricabilidad; el radio lo decide el usuario (p. ej. mayor que el de la fresa). Chaflanes igual:
  `proponer_chaflanes` (45×D o D×D, normalmente en aristas exteriores) → `crear_chaflanes`.
- Extrusiones y cortes con croquis: `proponer_extrusion` (contornos en coordenadas de la pieza
  sobre un plano o cara normal a X, Y o Z) → confirmación → `crear_extrusion`. Para levantar una
  pieza desde un boceto: el usuario crea una pieza vacía con la plantilla del IAC; primero el
  contorno exterior (extrusión), luego cortes, taladros, redondeos y chaflanes, de uno en uno y con
  confirmación de cada tabla. Pide las cotas que falten; no las deduzcas de una foto sin confirmarlas.
- Piezas torneadas: `proponer_revolucion` (media sección a un lado del eje de giro, en un plano que lo
  contenga; cotas radiales como diámetros) → confirmación → `crear_revolucion`.
- Taladros en piezas: `seleccion_creo` (lo que el usuario seleccionó en Creo) o `caras_creo`
  para saber la cara y el punto; `proponer_taladros` prepara la tabla sin tocar Creo y da un
  `codigo`. Enseña la tabla y, solo con la confirmación expresa del usuario, `crear_taladros`
  (si está activa: CREO_MCP_ALLOW_OPERACIONES=1). Crea operaciones IA_* sin guardar;
  `borrar_operaciones_ia` las deshace. No inventes cotas ni tamaños: pregunta lo que falte.
- Si las herramientas de planos están activas: solo crean y modifican planos NUEVOS
  creados por este servidor y, en su modelo, los parámetros del cajetín que ya
  existen. Nunca guardan: el usuario revisa el plano en Creo, lo guarda y lo
  registra en Windchill. Pide los valores del cajetín al usuario; no los inventes.
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                       openWorldHint=False)
EXPORT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                         openWorldHint=False)
DRAW = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                       openWorldHint=False)
DRAW_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False,
                                   openWorldHint=False)

_CREO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,30}$")  # Creo: máx. 31 caracteres

# Formatos del IAC en Windchill (carpeta de formatos del departamento), confirmados por
# el responsable el 2026-11-20. Dan el marco y el cajetín; la plantilla da las vistas.
FORMATOS_IAC = {"A0": "form_iac_a-0", "A1": "form_iac_a-1", "A2": "form_iac_a-2", "A3": "form_iac_a-3"}

Point = Annotated[float, Field(description="Coordenada en unidades del plano (normalmente mm)")]

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,100}$")
# Creo rechaza nombres de fichero de 32 caracteres o más antes de la extensión.
CREO_STEM_MAX = 31

_EXPORT_HINT = (
    "Causa más probable (vista en el PC del departamento el 2026-11-20): Creo (xtop.exe) "
    "no puede escribir en la carpeta de exportación; falla al instante con 'General Error' "
    "o 'Abort'. Prueba con otra carpeta en CREO_MCP_EXPORT_DIR (en ese PC funcionó "
    "E:\\PTC\\export y no E:\\PTC_Trabajo) o revisa los permisos de la carpeta; si lo "
    "bloquea el antivirus corporativo, pide a informática que lo permita. "
    "Si no es eso: 1) el modelo regenera sin errores; 2) exporta a mano (Archivo > Guardar "
    "una copia > STEP) para ver el mensaje de Creo."
)


def _step_filename(model: str, requested: str | None, exts: tuple[str, ...] = (".stp", ".step")) -> str:
    """Nombre del fichero exportado: sin rutas, con extensión y dentro del límite de Creo."""
    if requested:
        fname = requested.strip()
        if not _SAFE_FILENAME.match(fname) or ".." in fname:
            raise ValueError("Nombre de fichero no válido: solo letras, números, '_', '-' y '.', sin rutas.")
        if not fname.lower().endswith(exts):
            fname += exts[0]
        stem = fname.rsplit(".", 1)[0]
        if len(stem) > CREO_STEM_MAX:
            raise ValueError(
                f"Creo solo admite nombres de hasta {CREO_STEM_MAX} caracteres antes de la "
                f"extensión; '{stem}' tiene {len(stem)}."
            )
        return fname
    stamp = f"{datetime.now():%y%m%d%H%M}"  # 10 caracteres
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", model.rsplit(".", 1)[0])
    base = base[: CREO_STEM_MAX - len(stamp) - 1].rstrip("_-") or "modelo"
    return f"{base}_{stamp}{exts[0]}"

class Taladro(BaseModel):
    """Un taladro para proponer_taladros."""

    tipo: Literal["recto", "roscado", "holgura", "taladrado"] = Field(
        description="recto (diámetro libre); roscado, holgura o taladrado (normalizados ISO de Creo)")
    cara: int | str = Field(description="Id de la cara plana (caras_creo o seleccion_creo) o +X, -X, +Y, -Y, +Z, -Z "
                                        "(la cara más exterior hacia ese lado)")
    punto: list[float] = Field(min_length=3, max_length=3,
                               description="Centro [x, y, z] en coordenadas de la pieza (mm); se proyecta sobre la cara")
    metrica: str | None = Field(None, description="Roscado, holgura o taladrado: M6, M6x1, M2.5…")
    diametro: float | None = Field(None, gt=0, le=500, description="Solo recto: diámetro (mm)")
    profundidad: float | Literal["pasante"] = Field("pasante", description="'pasante' o profundidad en mm")
    rosca: float | Literal["pasante"] | None = Field(
        None, description="Roscado: longitud de rosca (mm) o 'pasante'. Vacío = 1,5·d (o pasante si el taladro lo es)")
    ajuste: Literal["fino", "medio", "libre"] = Field("medio", description="Holgura: ajuste de la tabla ISO")
    cajeado: bool = Field(False, description="Cajeado (rebaje cilíndrico para cabeza de tornillo, p. ej. DIN 912). "
                                             "Solo normalizados. Por defecto, Ø y profundidad de la tabla ISO de Creo")
    cajeado_diametro: float | None = Field(None, gt=0, description="Cajeado: Ø (mm), si no vale el de la tabla")
    cajeado_profundidad: float | None = Field(None, gt=0, description="Cajeado: profundidad (mm), si no vale la de la tabla")
    avellanado: bool = Field(False, description="Avellanado cónico (cabeza avellanada, p. ej. DIN 7991). Solo "
                                                "normalizados. Por defecto, Ø y ángulo de la tabla ISO de Creo")
    avellanado_diametro: float | None = Field(None, gt=0, description="Avellanado: Ø exterior (mm), si no vale el de la tabla")
    avellanado_angulo: float | None = Field(None, description="Avellanado: ángulo (°), 90 por defecto")
    referencias: list[str] | None = Field(
        None, min_length=2, max_length=2,
        description="Dos planos de referencia para acotar. Vacío = PERFIL, ALZADO o PLANTA perpendiculares a la cara")


ModelArg = Annotated[
    str | None,
    Field(description="Nombre del modelo en sesión, p. ej. 'SOPORTE.PRT'. Vacío = modelo activo."),
]


def _decode(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decodifica valores Base64 que CREOSON marca con encoded=True."""
    out = []
    for it in items or []:
        it = dict(it)
        if it.get("encoded") and isinstance(it.get("value"), str):
            try:
                it["value"] = base64.b64decode(it["value"]).decode("utf-8", "replace")
                it["encoded"] = False
            except Exception:  # noqa: BLE001
                pass
        out.append(it)
    return out


def build_server(settings: Settings, gateway: CreoGateway | None = None,
                 nativo: Nativo | None = None) -> FastMCP:
    gw = gateway or CreoGateway(settings)
    nat = nativo or Nativo(settings.toolkit_exe, settings.creo_common, gw.audit, gw._lock,
                           url=settings.toolkit_url, escritura=settings.allow_operaciones)
    tareas = GestorTareas(settings.espera_s, audit=gw.audit)
    # Taladros: propuestas pendientes de confirmar y escritura con resultado desconocido.
    propuestas: dict[str, dict[str, Any]] = {}
    propuestas_patron: dict[str, dict[str, Any]] = {}
    propuestas_redondeo: dict[str, dict[str, Any]] = {}
    propuestas_ext: dict[str, dict[str, Any]] = {}
    estado_ops: dict[str, Any] = {"desconocido": None}
    PROPUESTA_VALIDA_S = 1800
    mcp = FastMCP("creo-iac-lectura", instructions=INSTRUCTIONS)

    def limit(items: list[Any]) -> dict[str, Any]:
        items = list(items or [])
        n = len(items)
        return {
            "count": n,
            "items": items[: settings.max_items],
            "truncated": n > settings.max_items,
        }

    def guard(fn):
        """Convierte errores internos en mensajes claros para el asistente."""
        import functools

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except (CreoUnavailableError, NotAllowedError, ScopeError, ValueError, NativoError) as e:
                gw.audit.write(event="tool_error", tool=fn.__name__, error=str(e)[:500])
                raise ToolError(str(e)) from e
            except RuntimeError as e:  # error devuelto por CREOSON
                gw.audit.write(event="tool_error", tool=fn.__name__, error=str(e)[:500])
                raise ToolError(f"Creo/CREOSON devolvió un error: {e}") from e
            except Exception as e:  # noqa: BLE001  fallo interno: se registra para poder corregirlo
                import traceback

                tb = traceback.extract_tb(e.__traceback__)[-3:]
                donde = " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(tb))
                gw.audit.write(event="tool_error", tool=fn.__name__, tipo=type(e).__name__,
                               error=str(e)[:500], donde=donde)
                raise ToolError(f"Error interno en {fn.__name__}: {type(e).__name__}: {e} ({donde})") from e

        return wrapper

    # ------------------------------------------------------------------
    @mcp.tool(annotations=READ)
    @guard
    def creo_estado() -> dict[str, Any]:
        """Comprueba la conexión con CREOSON/Creo y muestra el modelo activo y la configuración.

        Si algo falla, `diagnostico` dice qué parte falla (CREOSON parado, cortado,
        Creo ocupado o no enlazado) y `que_hacer` cómo arreglarlo.
        """
        info: dict[str, Any] = {
            "servidor": f"creo-mcp-iac {__version__}",
            "creoson_url": gw.url,
            "exportacion_step": str(settings.export_dir) if settings.export_enabled else "desactivada",
        }
        try:
            info["creo_en_marcha"] = bool(gw.call("is_creo_running"))
        except CreoUnavailableError as e:
            info["creoson_responde"] = False
            info["creo_en_marcha"] = False
            info["diagnostico"] = e.codigo
            info["que_hacer"] = e.que_hacer
            info["error"] = e.mensaje
            return info
        info["creoson_responde"] = True
        if not info["creo_en_marcha"]:
            info["diagnostico"] = "creo_no_enlazado"
            info["que_hacer"] = (
                "CREOSON está arrancado pero no ve Creo. Arranca Creo (o reinícialo) y, si "
                "sigue igual, reinicia CREOSON después de Creo. Comprueba en CreosonSetup "
                "la ruta de Creo 12."
            )
            return info
        try:
            info["directorio_trabajo"] = gw.call("creo_pwd")
            active = gw.call("file_get_active") or {}
            info["modelo_activo"] = active.get("file")
            info["modelos_en_sesion"] = len(gw.models_in_session())
            info["diagnostico"] = "ok"
        except CreoUnavailableError as e:
            info["diagnostico"] = e.codigo
            info["que_hacer"] = e.que_hacer
        except RuntimeError as e:
            info["diagnostico"] = "creo_error"
            info["que_hacer"] = f"Creo respondió con error: {e}"
        return info

    @mcp.tool(annotations=READ)
    @guard
    def resultado_tarea(
        tarea_id: Annotated[str | None, Field(
            description="Identificador devuelto por la herramienta (p. ej. 'T001-221008'). Vacío = la última")] = None,
        esperar_s: Annotated[int, Field(ge=0, le=60, description="Segundos que espera si sigue en curso")] = 50,
    ) -> dict[str, Any]:
        """Recoge el resultado de una herramienta larga que respondió «en_curso» (exportar_step,
        exportar_bom_excel, presupuesto_masas, revisar_estandares, interferencias_creo,
        taladros_creo, taladros_conjugados, holguras_creo, crear_taladros, crear_patron, crear_redondeos, crear_chaflanes, crear_extrusion, crear_revolucion). Si aún no ha terminado, espera hasta esperar_s y vuelve a dar el estado:
        llámala otra vez. No repitas la herramienta original mientras siga en curso."""
        t = tareas.obtener(tarea_id)
        return tareas.entregar(t, min(esperar_s, tareas.espera_s))

    @mcp.tool(annotations=READ)
    @guard
    def listar_tareas() -> dict[str, Any]:
        """Tareas largas de esta sesión del servidor: herramienta, estado y duración."""
        return {"espera_maxima_por_llamada_s": tareas.espera_s, "tareas": tareas.lista()}

    @mcp.tool(annotations=READ)
    @guard
    def creo_capacidades() -> dict[str, Any]:
        """Qué puede hacer este servidor en esta instalación: herramientas activas (lectura,
        exportación y escritura), configuración, reglas de estándares cargadas, límites y si
        hay escrituras con resultado desconocido. Úsala antes de planificar una tarea; no
        llama a Creo salvo para comprobar que responde."""
        herramientas: dict[str, list[str]] = {"lectura": [], "exportacion": [], "escritura": []}
        for t in mcp._tool_manager.list_tools():
            a = t.annotations
            if a is None or a.readOnlyHint:
                herramientas["lectura"].append(t.name)
            elif a.destructiveHint:
                herramientas["escritura"].append(t.name)
            else:
                herramientas["exportacion"].append(t.name)
        try:
            creo = "responde" if gw.call("is_creo_running") else "CREOSON responde, pero no ve Creo"
        except CreoUnavailableError as e:
            creo = f"no disponible ({e.codigo}): {e.que_hacer}"
        try:
            reglas = estandares_mod.cargar_reglas()
            info_reglas = {"fichero": reglas["_fichero"], "materiales_con_restriccion": len(reglas["materiales"]),
                           "proyectos_con_formato_de_codigo": sorted(reglas["codigo_plano"]["por_proyecto"])}
        except Exception as e:  # noqa: BLE001
            info_reglas = {"error": f"No se pudieron cargar: {e}"}
        from .gateway import SLOW_FUNCS, SLOW_TIMEOUT_S

        return {
            "servidor": f"creo-mcp-iac {__version__}",
            "creo": creo,
            "version_creo_comunicada": settings.creo_version,
            "herramientas": {k: sorted(v) for k, v in herramientas.items()},
            "escritura_en_creo": {
                "parametros_del_cajetin_desde_excel": settings.allow_parameters,
                "planos_nativos": settings.allow_drawings,
                "taladros_en_piezas": settings.allow_operaciones,
                "taladros_servicio_con_escritura": nat.servicio_escritura,
                "guardar_modelos": False,
                "windchill": False,
            },
            "exportacion": str(settings.export_dir) if settings.export_enabled else "desactivada",
            "tiempos_s": {"normal": settings.timeout_s, "funciones_lentas": max(settings.timeout_s, SLOW_TIMEOUT_S),
                          "funciones_lentas_lista": sorted(SLOW_FUNCS)},
            "max_elementos_por_lista": settings.max_items,
            "nombres_de_personas": "ocultos" if settings.redact_names else "visibles",
            "auditoria": str(settings.audit_log),
            "reglas_de_estandares": info_reglas,
            "resultado_desconocido": gw.resultado_desconocido + ([estado_ops["desconocido"]]
                                                                 if estado_ops["desconocido"] else []),
            "lecturas_nativas_toolkit": dict(zip(("disponibles", "detalle"), nat.disponible())),
            "limites": [
                "Solo trabaja con modelos ya abiertos en Creo; no abre, guarda ni cierra modelos.",
                "Sin acceso a Windchill: los estados y versiones se leen de los parámetros PTC_WM_*.",
                "CREOSON no mide interferencias ni lee las operaciones de taladro: interferencias_creo y "
                "taladros_creo lo hacen con Creo Toolkit (si creo_iac_tk.exe está disponible). Holguras, "
                "fabricabilidad y tornillería por geometría: exportar_step y 04_AI_CAD/tools.",
                "Solo crea taladros sencillos (rectos y normalizados ISO, colocación lineal) en piezas en mm, "
                "con propuesta y confirmación; nunca guarda. El resto de la geometría nueva se modela en "
                "FreeCAD (decisión del 2026-11-20).",
            ],
        }

    @mcp.tool(annotations=READ)
    @guard
    def listar_modelos(
        patron: Annotated[str | None, Field(description="Filtro con comodines, p. ej. '*.ASM'")] = None,
    ) -> dict[str, Any]:
        """Lista los modelos (PRT, ASM, DRW) abiertos en la sesión de Creo."""
        if patron and any(c in patron for c in "/\\:"):
            raise ValueError("El patrón no puede contener rutas.")
        return limit(sorted(gw.models_in_session(patron)))

    @mcp.tool(annotations=READ)
    @guard
    def info_modelo(modelo: ModelArg = None) -> dict[str, Any]:
        """Datos generales de un modelo: fichero, unidades, material y tabla de familia."""
        m = gw.resolve_model(modelo)
        out: dict[str, Any] = {"modelo": m, "fichero": gw.call("file_get_fileinfo", file_=m)}
        if m.upper().endswith((".PRT", ".ASM")):
            out["unidades_longitud"] = gw.call("file_get_length_units", file_=m)
            out["unidades_masa"] = gw.call("file_get_mass_units", file_=m)
        if m.upper().endswith(".PRT"):
            try:
                out["material"] = gw.call("file_get_cur_material", file_=m)
            except RuntimeError:
                out["material"] = None
        if m.upper().endswith((".PRT", ".ASM")):
            try:
                out["tabla_familia"] = gw.call("file_list_instances", file_=m)
            except RuntimeError:
                out["tabla_familia"] = None
        return out

    @mcp.tool(annotations=READ)
    @guard
    def leer_parametros(
        modelo: ModelArg = None,
        nombre: Annotated[str | None, Field(description="Nombre o patrón, p. ej. 'MAT*'")] = None,
        incluir_pdm: Annotated[
            bool, Field(description="Incluye metadatos de Windchill/PDM (PTC_WM_*, PROI_*, PDM*)")
        ] = False,
    ) -> dict[str, Any]:
        """Lee los parámetros de un modelo (nombre, tipo, valor, designado).

        Por defecto omite los metadatos de Windchill/PDM, y los parámetros con
        nombres de personas (*_POR, *_BY) muestran "[rellenado]" o vacío.
        """
        m = gw.resolve_model(modelo)
        raw = _decode(gw.call("parameter_list", file_=m, name=nombre))
        params = clean_params(raw, include_pdm=incluir_pdm, redact=settings.redact_names)
        out = {"modelo": m, **limit(params)}
        if not incluir_pdm and len(params) < len(raw):
            out["omitidos_pdm"] = len(raw) - len(params)
        return out

    @mcp.tool(annotations=READ)
    @guard
    def leer_cotas(
        modelo: ModelArg = None,
        nombre: Annotated[str | None, Field(description="Nombre de cota, p. ej. 'd12'")] = None,
        tipo: Annotated[
            str | None, Field(description="linear, radial, diameter o angular")
        ] = None,
        detalle: Annotated[
            bool, Field(description="Incluye texto, hoja, vista y tolerancias (más lento)")
        ] = True,
    ) -> dict[str, Any]:
        """Lee las cotas de un modelo o plano, con tolerancias si `detalle` es True.

        Para revisar tolerancias, lee el PLANO (.DRW): solo ahí CREOSON devuelve
        la tolerancia, la hoja, la vista y la posición de cada cota mostrada. Al
        leer la pieza (.PRT) la tolerancia suele salir como "sin_datos".
        Cada cota trae `texto_legible` (p. ej. "Ø34", "R1519,17", "Ch5x45°") y
        `tolerancia` con `tipo`: explicita (±0,1), ajuste_iso (H7), general (rige
        la tolerancia general, parámetro TOLERANCIA) o sin_datos.
        No selecciona nada en Creo.
        """
        if tipo and tipo not in ("linear", "radial", "diameter", "angular"):
            raise ValueError("tipo debe ser linear, radial, diameter o angular")
        m = gw.resolve_model(modelo)
        if not m.upper().endswith((".PRT", ".ASM", ".DRW")):
            raise ValueError(f"{m} no es una pieza, conjunto o plano; no tiene cotas que leer.")
        func = "dimension_list_detail" if detalle else "dimension_list"
        dims = add_dim_text(_decode(gw.call(func, file_=m, name=nombre, dim_type=tipo)))
        out: dict[str, Any] = {"modelo": m, **limit(dims)}
        # Cotas sin tolerancia explícita: aplica la tolerancia general del cajetín.
        try:
            tg = gw.call("parameter_list", file_=m, name="TOLERANCIA")
            v = tg[0].get("value") if tg else None
            out["tolerancia_general"] = None if blank(v) else v
            if blank(v):
                out["aviso_tolerancia_general"] = "El parámetro TOLERANCIA está vacío o no existe en este modelo."
        except RuntimeError:
            pass
        return out

    @mcp.tool(annotations=READ)
    @guard
    def leer_notas(
        modelo: ModelArg = None,
        texto: Annotated[
            str | None, Field(description="Filtro del texto con comodines, p. ej. '*ANODIZ*'")
        ] = None,
    ) -> dict[str, Any]:
        """Lee las notas de un plano o modelo (texto original y con parámetros sustituidos).

        Útil para /revisar-plano: tratamientos, acabados, tolerancia general, roscas,
        notas de vacío. Las notas con símbolos de Creo se traducen igual que las cotas
        (Ø, °); los símbolos no verificados (p. ej. los de GD&T) salen como [símbolo:x] y
        "|" separa las casillas del marco. Las notas que muestran parámetros con nombres de
        personas (&DISEÑADO_POR...) salen como "[rellenado]". No selecciona nada.
        """
        m = gw.resolve_model(modelo)
        if not m.upper().endswith((".PRT", ".ASM", ".DRW")):
            raise ValueError(f"{m} no es una pieza, conjunto o plano.")
        notas = []
        for n in gw.call("note_list", file_=m, value=texto, get_expanded=True) or []:
            n = dict(n)
            encoded = bool(n.pop("encoded", False))
            for k in ("value", "value_expanded"):
                v = n.get(k)
                if encoded and isinstance(v, str):
                    try:
                        v = base64.b64decode(v).decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        pass
                if isinstance(v, str):
                    n[k] = note_text(v)
            if settings.redact_names and note_has_person(str(n.get("value", ""))):
                n["value_expanded"] = "[rellenado]" if str(n.get("value_expanded", "")).strip() else ""
            notas.append(n)
        return {"modelo": m, **limit(notas)}

    @mcp.tool(annotations=READ)
    @guard
    def lista_materiales(
        conjunto: ModelArg = None,
        solo_primer_nivel: Annotated[bool, Field(description="Solo componentes directos")] = False,
        incluir_esqueletos: bool = False,
    ) -> dict[str, Any]:
        """Lista de materiales (BOM) de un conjunto: indentada por niveles y agregada por cantidad total.

        Excluye componentes suprimidos o inactivos.
        """
        m = gw.resolve_model(conjunto)
        if not m.upper().endswith(".ASM"):
            raise ValueError(f"{m} no es un conjunto (.ASM).")
        raw = gw.call(
            "bom_get_paths",
            file_=m,
            top_level=solo_primer_nivel or None,
            skeletons=incluir_esqueletos or None,
            exclude_inactive=True,
        )
        bom = build_bom(raw or {})
        ind, flat = bom["indented"], bom["flat"]
        return {
            "conjunto": bom["assembly"],
            "generico": bom["generic"],
            "indentada": limit(ind),
            "agregada": limit(flat),
        }

    @mcp.tool(annotations=READ)
    @guard
    def listar_caracteristicas(
        modelo: ModelArg = None,
        tipo: Annotated[str | None, Field(description="Tipo con comodines, p. ej. 'HOLE', 'PROTRUSION'")] = None,
        estado: Annotated[
            str | None,
            Field(description="ACTIVE, INACTIVE, SUPPRESSED, FAMILY_TABLE_SUPPRESSED, "
                              "SIMP_REP_SUPPRESSED, PROGRAM_SUPPRESSED o UNREGENERATED"),
        ] = None,
        incluir_datums: bool = False,
        incluir_sin_nombre: Annotated[
            bool, Field(description="Incluye las características con nombre por defecto (casi todas)")
        ] = True,
    ) -> dict[str, Any]:
        """Lista el árbol de características (features) de un modelo con su tipo, estado e ID.

        Los tipos salen en el idioma de Creo (p. ej. "COMPONENTE", "Corte transversal").
        """
        m = gw.resolve_model(modelo)
        feats = gw.call(
            "feature_list",
            file_=m,
            type_=tipo,
            status=estado,
            paths=True,
            no_datum=not incluir_datums,
            inc_unnamed=incluir_sin_nombre,
        )
        return {"modelo": m, **limit(feats)}

    @mcp.tool(annotations=READ)
    @guard
    def propiedades_masa(modelo: ModelArg = None) -> dict[str, Any]:
        """Masa, volumen, densidad, área, centro de gravedad e inercias, con unidades del modelo.

        Usa la densidad asignada en Creo: si el material no tiene densidad, la masa no es fiable.
        Si el modelo tiene el parámetro MASA (cajetín IAC), `comprobacion_parametro_masa`
        indica si coincide con la masa calculada (tolerancia 0,1 %).
        """
        m = gw.resolve_model(modelo)
        if m.upper().endswith(".DRW"):
            raise ValueError("Un plano no tiene propiedades de masa; indica la pieza o el conjunto.")
        props = gw.call("file_massprops", file_=m) or {}
        lu = gw.call("file_get_length_units", file_=m)
        mu = gw.call("file_get_mass_units", file_=m)
        out: dict[str, Any] = {
            "modelo": m,
            "unidades_longitud": lu,
            "unidades_masa": mu,
            "propiedades": props,
        }
        try:
            pl = gw.call("parameter_list", file_=m, name=["MASA", "MASA_UNIT"]) or []
            pmap = {p["name"]: p.get("value") for p in pl}
            chk = check_mass_param(pmap, props.get("mass"), mu)
            if chk is not None:
                out["comprobacion_parametro_masa"] = chk
        except RuntimeError:
            pass
        si = massprops_si(props, lu, mu)
        if si is not None:
            out["si"] = si
        else:
            out["si"] = f"unidades no reconocidas ({lu}, {mu}): usa 'propiedades' con sus unidades"
        return out

    @mcp.tool(annotations=READ)
    @guard
    def info_plano(plano: ModelArg = None) -> dict[str, Any]:
        """Datos de un plano (.DRW): modelos referenciados, hojas (tamaño, escala, formato) y vistas."""
        d = gw.resolve_model(plano)
        if not d.upper().endswith(".DRW"):
            raise ValueError(f"{d} no es un plano (.DRW).")
        n = int(gw.call("drawing_get_num_sheets", drawing=d) or 0)
        hojas = []
        for s in range(1, n + 1):
            h: dict[str, Any] = {"hoja": s}
            for key, func in (("tamano", "drawing_get_sheet_size"),
                              ("escala", "drawing_get_sheet_scale"),
                              ("formato", "drawing_get_sheet_format")):
                try:
                    h[key] = gw.call(func, sheet=s, drawing=d)
                except RuntimeError as e:
                    h[key] = f"no disponible: {e}"
            hojas.append(h)
        try:
            simbolos: Any = limit(gw.call("drawing_list_symbols", drawing=d, sheet=0) or [])
        except RuntimeError as e:
            simbolos = f"no disponible: {e}"
        return {
            "plano": d,
            "modelos": gw.call("drawing_list_models", drawing=d),
            "num_hojas": n,
            "hojas": hojas,
            "vistas": limit(gw.call("drawing_list_view_details", drawing=d)),
            "simbolos": simbolos,
        }

    @mcp.tool(annotations=EXPORT)
    @guard
    @tareas.larga
    def exportar_step(
        modelo: ModelArg = None,
        nombre_fichero: Annotated[
            str | None,
            Field(description="Nombre del .stp (sin ruta). Máx. 31 caracteres antes de la extensión. Vacío = <modelo>_<aammddhhmm>.stp"),
        ] = None,
    ) -> dict[str, Any]:
        """Exporta una pieza o conjunto a STEP (sólidos) en la carpeta autorizada.

        No modifica el modelo. Nunca sobrescribe un fichero existente.
        Pide confirmación al usuario antes de usarla.
        """
        if not settings.export_enabled:
            raise ValueError("Exportación STEP desactivada: no se ha definido CREO_MCP_EXPORT_DIR.")
        m = gw.resolve_model(modelo)
        if not m.upper().endswith((".PRT", ".ASM")):
            raise ValueError("Solo se exportan piezas (.PRT) o conjuntos (.ASM).")
        fname = _step_filename(m, nombre_fichero)
        assert settings.export_dir is not None
        target = settings.export_dir / fname
        if settings.creoson_is_local and target.exists():
            raise ValueError(f"Ya existe {fname} en la carpeta de exportación; elige otro nombre.")

        # Creo 12 aborta con la función clásica de exportación (prueba 11 del
        # VALIDATION_LOG). Primero se usa la función de Creo 4+ (advanced) y,
        # si falla, la clásica. Ambas con STEP de sólidos y la misma carpeta.
        intentos: list[dict[str, str]] = []
        res: dict[str, Any] | None = None
        metodo = None
        for label, extra in (("creo4_advanced", {"advanced": True}), ("clasico", {})):
            try:
                res = gw.call(
                    "interface_export_file",
                    file_=m,
                    filename=fname,
                    dirname=str(settings.export_dir),
                    **extra,
                ) or {}
                metodo = label
                break
            except RuntimeError as e:
                if isinstance(e, CreoUnavailableError):
                    raise
                intentos.append({"metodo": label, "error": str(e)})
                if settings.creoson_is_local and target.exists():
                    break  # algo se escribió: no se reintenta para no pisarlo
        if res is None:
            raise ValueError(
                "Creo no pudo exportar el STEP. "
                + "; ".join(f"{i['metodo']}: {i['error']}" for i in intentos)
                + ". " + _EXPORT_HINT
            )
        out: dict[str, Any] = {
            "modelo": m,
            "metodo": metodo,
            "carpeta": res.get("dirname", str(settings.export_dir)),
            "fichero": res.get("filename", fname),
        }
        if intentos:
            out["intentos_fallidos"] = intentos
        written = Path(out["carpeta"]) / out["fichero"]
        if settings.creoson_is_local:
            if not written.is_file():
                out["aviso"] = "CREOSON informó de la exportación, pero el fichero no aparece en la carpeta."
            else:
                data = written.read_bytes()
                out["bytes"] = len(data)
                out["sha256"] = hashlib.sha256(data).hexdigest()
                if not data.lstrip().startswith(b"ISO-10303-21"):
                    out["aviso"] = "El fichero no empieza por ISO-10303-21: puede no ser un STEP válido."
        return out

    @mcp.tool(annotations=READ)
    @guard
    def config_creo() -> dict[str, Any]:
        """Opciones de configuración de Creo útiles para planos: plantillas, formatos,
        símbolos y fichero de configuración de detalle (solo una lista fija de opciones)."""
        out: dict[str, Any] = {}
        for opt in sorted(CONFIG_OPTIONS):
            try:
                out[opt] = gw.call("creo_get_config", name=opt)
            except RuntimeError as e:
                out[opt] = f"no disponible: {e}"
        return out

    @mcp.tool(annotations=READ)
    @guard
    def listar_plantillas_plano() -> dict[str, Any]:
        """Plantillas de plano (.drw) y formatos (.frm) que ve Creo, según su configuración.

        Busca en la carpeta de `template_drawing` y en `pro_format_dir`. Las carpetas
        de red solo se leen si este PC tiene acceso.
        """
        def values(opt: str) -> list[str]:
            try:
                v = gw.call("creo_get_config", name=opt) or []
            except RuntimeError:
                return []
            return [x for x in (v if isinstance(v, list) else [v]) if isinstance(x, str) and x.strip()]

        def scan(folder: Path, ext: str) -> list[str] | str:
            if str(folder).lower().startswith(("wtpub:", "wtws:")):
                return ("En Windchill: solo Creo ve esta carpeta. Usa el nombre de la plantilla "
                        "de template_drawing o pregunta al usuario cuáles hay.")
            try:
                names = {re.sub(r"\.\d+$", "", f.name) for f in folder.iterdir()
                         if re.search(rf"\{ext}(\.\d+)?$", f.name, re.IGNORECASE)}
                return sorted(names)
            except OSError as e:
                return f"no accesible desde este PC: {e}"

        out: dict[str, Any] = {}
        tpl = values("template_drawing")
        out["template_drawing"] = tpl
        out["plantilla_por_defecto"] = [re.sub(r"\.drw$", "", t.rsplit("/", 1)[-1], flags=re.I) for t in tpl]
        out["plantillas"] = {t.rsplit("/", 1)[0]: scan(Path(t).parent if not t.lower().startswith("wt") else Path(t), ".drw")
                             for t in tpl}
        fmt = values("pro_format_dir")
        out["pro_format_dir"] = fmt
        out["formatos"] = {d: scan(Path(d), ".frm") for d in fmt}
        return out

    @mcp.tool(annotations=READ)
    @guard
    def vistas_modelo(modelo: ModelArg = None) -> dict[str, Any]:
        """Vistas guardadas de un modelo (FRONT, TOP...), para orientar las vistas del plano."""
        m = gw.resolve_model(modelo)
        return {"modelo": m, **limit(gw.call("view_list", file_=m) or [])}

    @mcp.tool(annotations=EXPORT)
    @guard
    def exportar_pdf(
        plano: ModelArg = None,
        nombre_fichero: Annotated[
            str | None,
            Field(description="Nombre del .pdf (sin ruta). Máx. 31 caracteres. Vacío = <plano>_<aammddhhmm>.pdf"),
        ] = None,
    ) -> dict[str, Any]:
        """Exporta un plano (.DRW) a PDF en la carpeta autorizada, con los ajustes de plano
        (fuentes trazadas, escala de grises). No modifica el plano. Nunca sobrescribe.
        Pide confirmación al usuario antes de usarla."""
        if not settings.export_enabled:
            raise ValueError("Exportación desactivada: no se ha definido CREO_MCP_EXPORT_DIR.")
        d = gw.resolve_model(plano)
        if not d.upper().endswith(".DRW"):
            raise ValueError(f"{d} no es un plano (.DRW).")
        fname = _step_filename(d, nombre_fichero, (".pdf",))
        assert settings.export_dir is not None
        target = settings.export_dir / fname
        if settings.creoson_is_local and target.exists():
            raise ValueError(f"Ya existe {fname} en la carpeta de exportación; elige otro nombre.")
        try:
            gw.call("interface_export_pdf", file_=d, filename=fname, dirname=str(settings.export_dir))
        except RuntimeError as e:
            if isinstance(e, CreoUnavailableError):
                raise
            raise ValueError(f"Creo no pudo exportar el PDF: {e}. {_EXPORT_HINT}") from e
        out: dict[str, Any] = {"plano": d, "carpeta": str(settings.export_dir), "fichero": fname}
        if settings.creoson_is_local:
            if target.is_file():
                data = target.read_bytes()
                out["bytes"] = len(data)
                out["sha256"] = hashlib.sha256(data).hexdigest()
                if not data.startswith(b"%PDF"):
                    out["aviso"] = "El fichero no empieza por %PDF: puede no ser un PDF válido."
            else:
                out["aviso"] = "CREOSON informó de la exportación, pero el fichero no aparece en la carpeta."
        return out

    # --- lista de materiales a Excel (parecido a Nitro-BOM) -----------------------
    _NOMBRES_BOM = sorted(set(bom_excel.COLUMNAS) | set(bom_excel.EXTRA_LECTURA))

    def _params_de(modelo: str, nombres: list[str] | None = None) -> dict[str, dict[str, Any]]:
        raw = _decode(gw.call("parameter_list", file_=modelo, name=nombres or _NOMBRES_BOM) or [])
        return {str(p.get("name", "")).upper(): {"type": p.get("type"), "value": p.get("value")} for p in raw}

    def _params_varios(modelos: list[str], nombres: list[str] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        """Parámetros de varios modelos. Primero una sola petición con comodín (CREOSON
        devuelve el modelo de cada parámetro en owner_name); si no sirve, uno a uno
        (unos 0,6 s por modelo con Creo 12: 208 modelos, más de 2 min, el 2026-11-21)."""
        quiero = {m.upper(): m for m in modelos}
        out: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in quiero}
        try:
            raw = _decode(gw.call("parameter_list", file_="*", name=nombres or _NOMBRES_BOM) or [])
        except RuntimeError as e:
            if isinstance(e, CreoUnavailableError):
                raise
            raw = []
        if raw and all(p.get("owner_name") for p in raw):
            for p in raw:
                k = str(p["owner_name"]).upper()
                if k in out:
                    out[k][str(p.get("name", "")).upper()] = {"type": p.get("type"), "value": p.get("value")}
            return out  # un modelo que no sale no tiene ninguno de esos parámetros
        for m in quiero.values():
            out[m.upper()] = _params_de(m, nombres)
        return out

    def _fichero_bom(nombre: str) -> Path:
        if not settings.export_enabled:
            raise ValueError("No hay carpeta de exportación: define CREO_MCP_EXPORT_DIR.")
        n = (nombre or "").strip()
        if not _SAFE_FILENAME.match(n) or ".." in n or not n.lower().endswith(".xlsx"):
            raise ValueError("Indica solo el nombre del .xlsx de la carpeta de exportación, sin rutas.")
        assert settings.export_dir is not None
        ruta = settings.export_dir / n
        if not ruta.is_file():
            raise ValueError(f"No existe {n} en la carpeta de exportación ({settings.export_dir}).")
        return ruta

    @mcp.tool(annotations=EXPORT)
    @guard
    @tareas.larga
    def exportar_bom_excel(
        conjunto: ModelArg = None,
        nombre_fichero: Annotated[
            str | None, Field(description="Nombre del .xlsx (sin ruta). Vacío = BOM_<conjunto>_<aammddhhmm>.xlsx")
        ] = None,
    ) -> dict[str, Any]:
        """Lista de materiales de un conjunto a Excel en la carpeta de exportación.

        Hojas: BOM indentado; lista de piezas con los parámetros del cajetín (columnas
        amarillas editables); tornillería agrupada por elemento, métrica y longitud
        (reconocida por norma o palabra clave en el nombre); avisos (parámetros
        vacíos, PROYECTO distinto, CODIGO_PLANO repetido). No modifica Creo y no
        exporta nombres de personas. Nunca sobrescribe un fichero.
        """
        if not settings.export_enabled:
            raise ValueError("No hay carpeta de exportación: define CREO_MCP_EXPORT_DIR.")
        m = gw.resolve_model(conjunto)
        if not m.upper().endswith(".ASM"):
            raise ValueError(f"{m} no es un conjunto (.ASM).")
        if nombre_fichero:
            fname = nombre_fichero.strip()
            if not _SAFE_FILENAME.match(fname) or ".." in fname:
                raise ValueError("Nombre de fichero no válido: solo letras, números, '_', '-' y '.', sin rutas.")
            if not fname.lower().endswith(".xlsx"):
                fname += ".xlsx"
        else:
            base = re.sub(r"[^A-Za-z0-9_\-]", "_", m.rsplit(".", 1)[0])[:60]
            fname = f"BOM_{base}_{datetime.now():%y%m%d%H%M}.xlsx"
        assert settings.export_dir is not None
        ruta = settings.export_dir / fname
        if ruta.exists():
            raise ValueError(f"Ya existe {fname} en la carpeta de exportación; elige otro nombre.")
        raw = gw.call("bom_get_paths", file_=m, exclude_inactive=True)
        bom = build_bom(raw or {})
        modelos = list(dict.fromkeys([m] + [f["file"] for f in bom["flat"]]))
        parametros = _params_varios(modelos)
        datos = bom_excel.preparar(bom, parametros)
        resumen = bom_excel.exportar(datos, ruta, {"servidor": f"creo-mcp-iac {__version__}"})
        return {
            "conjunto": bom["assembly"] or m,
            "carpeta": str(settings.export_dir),
            "fichero": fname,
            **resumen,
            "tornilleria": [{k: g[k] for k in ("elemento", "tipo", "norma", "metrica", "longitud", "cantidad")}
                            for g in datos["tornilleria"]],
            "avisos_detalle": datos["avisos"][:50],
            "siguiente": "Edita las columnas amarillas de «Lista de piezas», guarda el Excel y usa "
                         "revisar_parametros_excel para ver los cambios antes de aplicarlos.",
        }

    _NOMBRES_MASA = sorted({"CODIGO_PLANO", "PIEZA", "PTC_MATERIAL_NAME", "MASA", "MASA_UNIT", "PTC_UNITS_MASS"})

    @mcp.tool(annotations=EXPORT)
    @guard
    @tareas.larga
    def presupuesto_masas(
        conjunto: ModelArg = None,
        nombre_fichero: Annotated[
            str | None, Field(description="Nombre del .xlsx (sin ruta). Vacío = MASAS_<conjunto>_<aammddhhmm>.xlsx")
        ] = None,
    ) -> dict[str, Any]:
        """Presupuesto de masas de un conjunto a Excel en la carpeta de exportación. No modifica Creo.

        Masa de cada pieza calculada por Creo con la densidad del material asignado; los
        conjuntos con CODIGO_PLANO = NA (comerciales) cuentan enteros. Hojas: Resumen (masa
        según Creo y suma de piezas, CdG e inercias del conjunto), Por subconjunto, Piezas
        (cantidad, masa unitaria y total, margen editable y masa con margen, con fórmulas)
        y Avisos (sin material o densidad, parámetro MASA desactualizado). Con conjuntos
        grandes tarda minutos: si la respuesta no llega, el Excel aparece igualmente.
        """
        if not settings.export_enabled:
            raise ValueError("No hay carpeta de exportación: define CREO_MCP_EXPORT_DIR.")
        m = gw.resolve_model(conjunto)
        if not m.upper().endswith(".ASM"):
            raise ValueError(f"{m} no es un conjunto (.ASM). Para una pieza, usa propiedades_masa.")
        if nombre_fichero:
            fname = nombre_fichero.strip()
            if not _SAFE_FILENAME.match(fname) or ".." in fname:
                raise ValueError("Nombre de fichero no válido: solo letras, números, '_', '-' y '.', sin rutas.")
            if not fname.lower().endswith(".xlsx"):
                fname += ".xlsx"
        else:
            base = re.sub(r"[^A-Za-z0-9_\-]", "_", m.rsplit(".", 1)[0])[:60]
            fname = f"MASAS_{base}_{datetime.now():%y%m%d%H%M}.xlsx"
        assert settings.export_dir is not None
        ruta = settings.export_dir / fname
        if ruta.exists():
            raise ValueError(f"Ya existe {fname} en la carpeta de exportación; elige otro nombre.")
        bom = build_bom(gw.call("bom_get_paths", file_=m, exclude_inactive=True) or {})
        modelos = list(dict.fromkeys([m] + [f["file"] for f in bom["flat"]]))
        params = _params_varios(modelos, _NOMBRES_MASA)
        h = masas_mod.hojas(bom, params)
        lu = gw.call("file_get_length_units", file_=m)
        mu_asm = (params.get(m.upper(), {}).get("PTC_UNITS_MASS", {}).get("value")
                  or gw.call("file_get_mass_units", file_=m))
        datos_masa: dict[str, dict[str, Any]] = {}
        for k, hoja in h["hojas"].items():
            unidad = params.get(k, {}).get("PTC_UNITS_MASS", {}).get("value")
            try:
                props = gw.call("file_massprops", file_=hoja["modelo"]) or {}
                if not unidad:
                    unidad = gw.call("file_get_mass_units", file_=hoja["modelo"])
                datos_masa[k] = {"props": props, "unidad_masa": unidad}
            except RuntimeError as e:
                if isinstance(e, CreoUnavailableError):
                    raise
                datos_masa[k] = {"error": str(e)[:200]}
        props_asm = gw.call("file_massprops", file_=m) or {}
        conj = {"props": props_asm, "unidad_masa": mu_asm, "unidad_longitud": lu,
                "si": massprops_si(props_asm, lu, mu_asm)}
        d = masas_mod.preparar(bom, params, datos_masa, conj)
        resumen = masas_mod.exportar(d, ruta, {"servidor": f"creo-mcp-iac {__version__}"})
        return {"conjunto": bom["assembly"] or m, "carpeta": str(settings.export_dir), "fichero": fname,
                **resumen, "subtotales_kg": {g: round(v, 4) for g, v in d["subtotales"].items()},
                "avisos_detalle": d["avisos"][:40]}

    @mcp.tool(annotations=EXPORT)
    @guard
    @tareas.larga
    def revisar_estandares(
        conjunto: ModelArg = None,
        vacio: Annotated[bool, Field(description="Revisar materiales para vacío")] = True,
        criogenia: Annotated[bool, Field(description="Revisar materiales para criogenia")] = False,
        comprobar_masa: Annotated[bool, Field(description="Comparar el parámetro MASA con la masa calculada")] = True,
        nombre_fichero: Annotated[
            str | None, Field(description="Nombre del .xlsx (sin ruta). Vacío = ESTANDARES_<conjunto>_<aammddhhmm>.xlsx")
        ] = None,
    ) -> dict[str, Any]:
        """Revisa los modelos propios de un conjunto contra los estándares del departamento y
        escribe un Excel en la carpeta de exportación. No modifica Creo.

        Reglas: parámetros obligatorios, formato y repetición de CODIGO_PLANO, PROYECTO
        distinto, valores escritos de varias formas, «válido para fabricación» con el
        modelo sin liberar en Windchill, material (sin asignar o con restricciones para vacío
        o criogenia), unidades, revisión del plano frente al modelo (solo planos abiertos en
        la sesión) y parámetro MASA. Omite la tornillería y el contenido de los comerciales
        (CODIGO_PLANO = NA). Las reglas están en estandares_iac.json (o CREO_MCP_ESTANDARES).
        """
        if not settings.export_enabled:
            raise ValueError("No hay carpeta de exportación: define CREO_MCP_EXPORT_DIR.")
        m = gw.resolve_model(conjunto)
        if not m.upper().endswith(".ASM"):
            raise ValueError(f"{m} no es un conjunto (.ASM).")
        if nombre_fichero:
            fname = nombre_fichero.strip()
            if not _SAFE_FILENAME.match(fname) or ".." in fname:
                raise ValueError("Nombre de fichero no válido: solo letras, números, '_', '-' y '.', sin rutas.")
            if not fname.lower().endswith(".xlsx"):
                fname += ".xlsx"
        else:
            base = re.sub(r"[^A-Za-z0-9_\-]", "_", m.rsplit(".", 1)[0])[:60]
            fname = f"ESTANDARES_{base}_{datetime.now():%y%m%d%H%M}.xlsx"
        assert settings.export_dir is not None
        ruta = settings.export_dir / fname
        if ruta.exists():
            raise ValueError(f"Ya existe {fname} en la carpeta de exportación; elige otro nombre.")
        reglas = estandares_mod.cargar_reglas()
        bom = build_bom(gw.call("bom_get_paths", file_=m, exclude_inactive=True) or {})
        modelos = list(dict.fromkeys([m] + [f["file"] for f in bom["flat"]]))
        dibujos = [d for d in gw.models_in_session("*.drw")]
        params = _params_varios(modelos + dibujos, estandares_mod.NOMBRES)
        # planos abiertos: modelo que dibujan y su revisión
        planos: dict[str, dict[str, Any]] = {}
        en_bom = {x.upper() for x in modelos}
        for dwg in dibujos:
            try:
                mods = gw.call("drawing_list_models", drawing=dwg) or []
            except RuntimeError as e:
                if isinstance(e, CreoUnavailableError):
                    raise
                continue
            rev = params.get(dwg.upper(), {}).get("PTC_WM_REVISION", {}).get("value")
            for mm in mods:
                if mm.upper() in en_bom and rev:
                    planos[mm.upper()] = {"plano": dwg, "revision": rev}
        masas_r: dict[str, dict[str, Any]] = {}
        if comprobar_masa:
            for k in en_bom:
                pk = params.get(k, {})
                if "MASA" not in pk or not k.endswith(".PRT"):
                    continue
                try:
                    props = gw.call("file_massprops", file_=next(x for x in modelos if x.upper() == k)) or {}
                except RuntimeError as e:
                    if isinstance(e, CreoUnavailableError):
                        raise
                    continue
                unidad = pk.get("PTC_UNITS_MASS", {}).get("value")
                chk = check_mass_param({x: pk[x]["value"] for x in ("MASA", "MASA_UNIT") if x in pk},
                                       props.get("mass"), unidad)
                if chk and chk.get("estado") == "difiere" and chk.get("diferencia_pct") is not None:
                    masas_r[k] = {"kg": chk["calculada_kg"], "param_kg": chk["parametro_kg"],
                                  "dif_pct": chk["diferencia_pct"]}
        r = estandares_mod.revisar(bom, params, reglas, vacio=vacio, criogenia=criogenia,
                                   masas=masas_r, planos=planos)
        estandares_mod.exportar(r, reglas, ruta, {"servidor": f"creo-mcp-iac {__version__}"})
        return {"conjunto": r["conjunto"] or m, "carpeta": str(settings.export_dir), "fichero": fname,
                "modelos_revisados": r["revisados"], "planos_comparados": r["planos_comparados"],
                "por_regla": {f"{k} {estandares_mod.REGLAS[k]}": v for k, v in r["por_regla"].items()},
                "incumplimientos": r["incumplimientos"][:60],
                "total_incumplimientos": len(r["incumplimientos"])}

    # --- lecturas nativas con Creo Toolkit (creo_iac_tk.exe) -----------------------
    def _tornilleria(nombre: str) -> dict[str, Any] | None:
        return bom_excel.clasificar_tornilleria(nombre)

    def _nuevo_fichero(prefijo: str, modelo: str, nombre_fichero: str | None) -> tuple[str, Path]:
        if nombre_fichero:
            fname = nombre_fichero.strip()
            if not _SAFE_FILENAME.match(fname) or ".." in fname:
                raise ValueError("Nombre de fichero no válido: solo letras, números, '_', '-' y '.', sin rutas.")
            if not fname.lower().endswith(".xlsx"):
                fname += ".xlsx"
        else:
            base = re.sub(r"[^A-Za-z0-9_\-]", "_", modelo.rsplit(".", 1)[0])[:60]
            fname = f"{prefijo}_{base}_{datetime.now():%y%m%d%H%M}.xlsx"
        assert settings.export_dir is not None
        ruta = settings.export_dir / fname
        if ruta.exists():
            raise ValueError(f"Ya existe {fname} en la carpeta de exportación; elige otro nombre.")
        return fname, ruta

    def _modelo_nativo(modelo: str | None) -> str | None:
        """Modelo para la lectura nativa. Con CREOSON se comprueba que está en sesión; sin
        CREOSON (parado o sin enlace) se pasa tal cual y, si viene vacío, el programa nativo
        usa el modelo activo: las lecturas nativas no dependen de CREOSON."""
        try:
            return gw.resolve_model(modelo)
        except CreoUnavailableError:
            return modelo.strip() if modelo and modelo.strip() else None
        except RuntimeError as e:
            if "session" in str(e).lower():
                return modelo.strip() if modelo and modelo.strip() else None
            raise

    def _cab(*cols: str) -> list[tuple[str, int]]:
        return [(c, xlsx_mod.CABECERA) for c in cols]

    @mcp.tool(annotations=EXPORT)
    @guard
    @tareas.larga
    def interferencias_creo(
        conjunto: ModelArg = None,
        rapido: Annotated[bool, Field(
            description="Cálculo rápido: solo los pares, sin volumen; puede incluir contactos")] = False,
        nombre_fichero: Annotated[
            str | None, Field(description="Nombre del .xlsx (sin ruta). Vacío = INTERF_<conjunto>_<aammddhhmm>.xlsx")
        ] = None,
    ) -> dict[str, Any]:
        """Interferencias de un conjunto con el análisis global nativo de Creo (Creo Toolkit),
        sin exportar STEP. Da cada par de piezas que se interfieren, su ruta en el conjunto y
        el volumen interferido; separa la tornillería (rosca modelada al diámetro nominal) y
        los pasadores (avisa si un pasador cilíndrico se mete en material sin taladro) de las
        interferencias reales. Si hay carpeta de exportación, escribe además un Excel.
        No modifica Creo. En conjuntos grandes puede tardar varios minutos. No mide holguras
        ni contactos: para eso, /revisar-interferencias con el STEP."""
        m = _modelo_nativo(conjunto)
        if m and not m.upper().endswith(".ASM"):
            raise ValueError(f"{m} no es un conjunto (.ASM).")
        datos = nat.ejecutar("interferencias", m, rapido=rapido)
        r = resumir_interferencias(datos, _tornilleria)
        m = r["conjunto"]
        fichero = None
        if settings.export_enabled:
            fname, ruta = _nuevo_fichero("INTERF", m, nombre_fichero)
            u = r["unidad_longitud"] or "?"
            filas = [_cab("Categoría", "Pieza A", "Ruta A", "Pieza B", "Ruta B", f"Volumen ({u}³)", "Nota")]
            filas += [[f["categoria"], f["a"], "/".join(map(str, f["ruta_a"])), f["b"],
                       "/".join(map(str, f["ruta_b"])),
                       (f["volumen"], xlsx_mod.NUM3) if f["volumen"] is not None else "", f.get("nota", "")]
                      for f in r["interferencias"]]
            resumen = [_cab("Dato", "Valor"),
                       ["Conjunto", r["conjunto"]], ["Fecha", f"{datetime.now():%Y-%m-%d %H:%M}"],
                       ["Herramienta", f"creo-mcp-iac {__version__} / creo_iac_tk {datos.get('version_tk')}"],
                       ["Cálculo", "rápido (sin volumen)" if r["rapido"] else "exacto"],
                       ["Unidad de longitud", u], ["Pares con interferencia", r["total"]]]
            resumen += [[f"  {k}", v] for k, v in r["por_categoria"].items()]
            resumen += [[""], ["Borrador generado por el asistente: revisar cada par en Creo antes de decidir."]]
            xlsx_mod.escribir(ruta, [
                {"nombre": "Resumen", "filas": resumen, "anchos": [28, 70]},
                {"nombre": "Interferencias", "filas": filas, "anchos": [44, 40, 14, 40, 14, 16, 44],
                 "fijar_cabecera": True, "autofiltro": len(filas)},
            ])
            fichero = fname
        return {"conjunto": r["conjunto"], "unidad_longitud": r["unidad_longitud"], "rapido": r["rapido"],
                "total": r["total"], "por_categoria": r["por_categoria"],
                "interferencias": r["interferencias"][:60],
                "mostradas": min(60, r["total"]),
                "carpeta": str(settings.export_dir) if fichero else None, "fichero": fichero,
                "fuente": f"Creo Toolkit (creo_iac_tk {datos.get('version_tk')}), análisis global de interferencias"}

    @mcp.tool(annotations=EXPORT)
    @guard
    @tareas.larga
    def taladros_creo(
        modelo: ModelArg = None,
        nombre_fichero: Annotated[
            str | None, Field(description="Nombre del .xlsx (sin ruta). Vacío = TALADROS_<modelo>_<aammddhhmm>.xlsx")
        ] = None,
    ) -> dict[str, Any]:
        """Operaciones de taladro y roscas cosméticas de una pieza o de todas las piezas de un
        conjunto, leídas de forma nativa (Creo Toolkit): tipo (recto, normalizado, croquis),
        roscado/holgura/taladrado, serie de rosca y tamaño de tornillo, diámetro, profundidad,
        rosca, avellanados y eje (punto y dirección en coordenadas de la pieza). Cuenta los
        taladros por tamaño teniendo en cuenta las instancias. Si hay carpeta de exportación,
        escribe además un Excel. No modifica Creo. No ve taladros hechos como extrusión o
        revolución: para esos, la herramienta de tornillería con el STEP."""
        datos = nat.ejecutar("taladros", _modelo_nativo(modelo))
        r = resumir_taladros(datos)
        m = r["modelo"]
        fichero = None
        if settings.export_enabled:
            fname, ruta = _nuevo_fichero("TALADROS", m, nombre_fichero)
            filas = [_cab("Modelo", "Instancias", "Id", "Nombre", "Clase", "Tipo", "Estándar", "Serie",
                          "Tamaño", "Ajuste", "Ø", "Profundidad (tipo)", "Profundidad", "Rosca",
                          "Avellanado", "Eje: punto", "Eje: dirección")]
            for mm in r["modelos"]:
                for op in mm["operaciones"]:
                    rosca = op.get("rosca") or {}
                    av = op.get("avellanado_cilindrico") or op.get("avellanado_conico") or {}
                    eje = op.get("eje") or {}
                    filas.append([mm["modelo"], mm["instancias"], op.get("id"), op.get("nombre", ""),
                                  op.get("clase", ""), op.get("tipo", ""), op.get("estandar", ""),
                                  op.get("serie_rosca", ""), op.get("tamano_tornillo", ""), op.get("ajuste", ""),
                                  (op["diametro"], xlsx_mod.NUM3) if isinstance(op.get("diametro"), (int, float)) else "",
                                  op.get("profundidad_tipo", ""),
                                  (op["profundidad"], xlsx_mod.NUM3) if isinstance(op.get("profundidad"), (int, float)) else "",
                                  " ".join(str(x) for x in (
                                      f"Ø{rosca['diametro']:g}" if isinstance(rosca.get("diametro"), (int, float)) else None,
                                      rosca.get("profundidad_tipo"), rosca.get("profundidad")) if x is not None),
                                  " ".join(f"{k} {v}" for k, v in av.items()),
                                  " ".join(f"{c:g}" for c in eje.get("punto", [])),
                                  " ".join(f"{c:g}" for c in eje.get("direccion", []))])
            resumen = [_cab("Dato", "Valor"), ["Modelo", r["modelo"]],
                       ["Fecha", f"{datetime.now():%Y-%m-%d %H:%M}"],
                       ["Herramienta", f"creo-mcp-iac {__version__} / creo_iac_tk {datos.get('version_tk')}"],
                       ["Unidad de longitud", r["unidad_longitud"]],
                       ["Modelos revisados", r["modelos_revisados"]],
                       ["Modelos con taladros", r["modelos_con_taladros"]],
                       ["Taladros en el conjunto (× instancias)", r["operaciones_en_conjunto"]], [""],
                       _cab("Tamaño", "Cantidad (× instancias)")]
            resumen += [[k, v] for k, v in r["por_tamano"].items()]
            xlsx_mod.escribir(ruta, [
                {"nombre": "Resumen", "filas": resumen, "anchos": [40, 30]},
                {"nombre": "Taladros", "filas": filas,
                 "anchos": [36, 10, 8, 22, 16, 13, 11, 14, 12, 8, 9, 18, 11, 22, 26, 28, 24],
                 "fijar_cabecera": True, "autofiltro": len(filas)},
            ])
            fichero = fname
        planos = [dict(op, modelo=mm["modelo"], instancias=mm["instancias"])
                  for mm in r["modelos"] for op in mm["operaciones"]]
        lim = limit(planos)
        return {"modelo": r["modelo"], "unidad_longitud": r["unidad_longitud"],
                "modelos_revisados": r["modelos_revisados"], "modelos_con_taladros": r["modelos_con_taladros"],
                "taladros_en_conjunto": r["operaciones_en_conjunto"], "por_tamano": r["por_tamano"],
                "operaciones": lim["items"], "operaciones_count": lim["count"], "truncated": lim["truncated"],
                "carpeta": str(settings.export_dir) if fichero else None, "fichero": fichero,
                "fuente": f"Creo Toolkit (creo_iac_tk {datos.get('version_tk')}), árbol de elementos de cada operación"}

    @mcp.tool(annotations=EXPORT)
    @guard
    @tareas.larga
    def taladros_conjugados(
        conjunto: ModelArg = None,
        separacion_max: Annotated[float, Field(
            ge=0, le=50, description="Separación máxima a lo largo del eje entre los dos taladros (mm), p. ej. "
                                     "el espesor de una arandela o un suplemento entre las piezas")] = 1.0,
        nombre_fichero: Annotated[
            str | None, Field(description="Nombre del .xlsx (sin ruta). Vacío = CONJUGADOS_<conjunto>_<aammddhhmm>.xlsx")
        ] = None,
        incluir_tornilleria: Annotated[bool, Field(
            description="Incluir los tornillos (sus roscas cosméticas exteriores no son taladros)")] = False,
    ) -> dict[str, Any]:
        """Comprueba los taladros conjugados de un conjunto (Creo Toolkit, sin STEP): busca los
        taladros de piezas distintas que están enfrentados (ejes casi coincidentes y tocándose) y
        de cada pareja dice si están alineados (descentrado frente a la holgura que deja el
        tornillo; ejes paralelos) y si casan de tamaño (roscado M6 con holgura M6, no M5; recto con
        Ø mayor que la métrica; dos roscas enfrentadas; taladrado sin rosca). Cuenta además los
        taladros sin pareja. Si hay carpeta de exportación, escribe un Excel. No modifica Creo.
        Solo ve operaciones de taladro (no cortes por extrusión o revolución)."""
        m = _modelo_nativo(conjunto)
        if m and not m.upper().endswith(".ASM"):
            raise ValueError(f"{m} no es un conjunto (.ASM): abre el conjunto o indica su nombre.")
        datos_t = nat.ejecutar("taladros", m)
        datos_p = nat.ejecutar("posiciones", m)
        excluir = None if incluir_tornilleria else (lambda nombre: _tornilleria(nombre) is not None)
        r = revisar_conjugados(datos_t, datos_p, separacion_max=separacion_max, excluir=excluir)
        fichero = None
        if settings.export_enabled:
            fname, ruta = _nuevo_fichero("CONJUGADOS", r["conjunto"], nombre_fichero)
            u = r["unidad_longitud"] or "?"
            filas = [_cab("Estado", "Pieza A", "Taladro A", "Id A", "Pieza B", "Taladro B", "Id B",
                          f"Descentrado ({u})", "Ángulo (°)", "Punto (conjunto)", "Nota")]
            filas += [[x["estado"], x["a"]["pieza"], x["a"]["taladro"], x["a"]["id"], x["b"]["pieza"],
                       x["b"]["taladro"], x["b"]["id"], (x["descentrado"], xlsx_mod.NUM3), x["angulo"],
                       " ".join(f"{c:g}" for c in x["punto"]), x["nota"]] for x in r["pares"]]
            sueltos = [_cab("Pieza", "Id", "Nombre", "Taladro", "Punto (conjunto)")]
            sueltos += [[t["pieza"], t["id"], t["nombre"] or "", t["taladro"], " ".join(f"{c:g}" for c in t["punto"])]
                        for t in r["sin_pareja"]]
            resumen = [_cab("Dato", "Valor"), ["Conjunto", r["conjunto"]],
                       ["Fecha", f"{datetime.now():%Y-%m-%d %H:%M}"],
                       ["Herramienta", f"creo-mcp-iac {__version__} / creo_iac_tk {datos_t.get('version_tk')}"],
                       ["Unidad de longitud", u], ["Separación máxima a lo largo del eje", separacion_max],
                       ["Piezas con taladros (instancias)", r["piezas_con_taladros"]],
                       ["Taladros revisados", r["taladros_revisados"]], ["Parejas", len(r["pares"])]]
            resumen += [[f"  {k}", v] for k, v in r["por_estado"].items()]
            resumen += [["Taladros sin pareja", len(r["sin_pareja"])], ["  roscados", r["roscados_sin_pareja"]]]
            resumen += [[a] for a in r["avisos"]]
            resumen += [[""], ["Borrador generado por el asistente: revisar cada pareja en Creo. No ve cortes "
                               "por extrusión ni revolución."]]
            xlsx_mod.escribir(ruta, [
                {"nombre": "Resumen", "filas": resumen, "anchos": [40, 60]},
                {"nombre": "Parejas", "filas": filas, "anchos": [10, 34, 30, 8, 34, 30, 8, 14, 10, 26, 70],
                 "fijar_cabecera": True, "autofiltro": len(filas)},
                {"nombre": "Sin pareja", "filas": sueltos, "anchos": [34, 8, 26, 30, 26],
                 "fijar_cabecera": True, "autofiltro": len(sueltos)},
            ])
            fichero = fname
        mostrar = [x for x in r["pares"] if x["estado"] != "correcto"][:60]
        return {"conjunto": r["conjunto"], "unidad_longitud": r["unidad_longitud"],
                "piezas_con_taladros": r["piezas_con_taladros"], "taladros_revisados": r["taladros_revisados"],
                "parejas": len(r["pares"]), "por_estado": r["por_estado"],
                "problemas": mostrar, "problemas_mostrados": len(mostrar),
                "correctas_ejemplo": [x for x in r["pares"] if x["estado"] == "correcto"][:5],
                "sin_pareja": len(r["sin_pareja"]), "roscados_sin_pareja": r["roscados_sin_pareja"],
                **({"avisos": r["avisos"]} if r["avisos"] else {}),
                "carpeta": str(settings.export_dir) if fichero else None, "fichero": fichero,
                "fuente": f"Creo Toolkit (creo_iac_tk {datos_t.get('version_tk')}): taladros de cada pieza y "
                          "posición de cada componente"}

    @mcp.tool(annotations=EXPORT)
    @guard
    @tareas.larga
    def holguras_creo(
        conjunto: ModelArg = None,
        umbral: Annotated[float, Field(gt=0, le=50, description="Se listan los pares de piezas a menos de esta "
                                                                "distancia (mm)")] = 2.0,
        holgura_minima: Annotated[float, Field(ge=0, le=50, description="Holgura mínima admisible (mm); por "
                                                                       "debajo se marca 'insuficiente'")] = 0.5,
        temperatura: Annotated[float | None, Field(
            ge=1, le=293, description="Temperatura de operación (K) para estimar las holguras en frío. Vacío = "
                                      "solo a temperatura ambiente")] = None,
        temperaturas: Annotated[dict[str, float] | None, Field(
            description="Temperatura (K) de piezas concretas, p. ej. {'PANTALLA.PRT': 80}; el resto, `temperatura`")] = None,
        materiales: Annotated[dict[str, str] | None, Field(
            description="Material de piezas concretas si en Creo no está o no se reconoce, p. ej. "
                        "{'SOPORTE.PRT': 'Ti-6Al-4V'} (nombres de contraccion_iac.json)")] = None,
        punto_fijo: Annotated[list[float] | None, Field(
            min_length=3, max_length=3, description="Punto (coordenadas del conjunto, mm) alrededor del que se "
                                                    "contrae todo: por dónde está sujeto. Vacío = origen del conjunto")] = None,
        fijo_en_seleccion: Annotated[bool, Field(
            description="Tomar el punto fijo de lo seleccionado en Creo (centro de la cara por la que se sujeta "
                        "el conjunto)")] = False,
        incluir_tornilleria: Annotated[bool, Field(description="Incluir tornillos, tuercas, arandelas y pasadores")] = False,
        max_pares: Annotated[int, Field(ge=1, le=20000, description="Máximo de pares que se calculan en Creo "
                                                                  "(los de cajas más cercanas)")] = 3000,
        nombre_fichero: Annotated[
            str | None, Field(description="Nombre del .xlsx (sin ruta). Vacío = HOLGURAS_<conjunto>_<aammddhhmm>.xlsx")
        ] = None,
    ) -> dict[str, Any]:
        """Holguras mínimas entre las piezas de un conjunto (Creo Toolkit, distancia exacta entre
        sólidos) y, si se da `temperatura`, su estimación en frío: cada pieza se contrae con la
        contracción integrada de su material (desde 293 K, tabla del departamento) alrededor de
        `punto_fijo`. Marca las holguras por debajo de `holgura_minima`, los contactos y las que
        se cierran en frío. Primero filtra los pares por sus cajas; sin tornillería por defecto.
        Excel opcional. No modifica Creo. Las interferencias las da interferencias_creo.
        Los ajustes concéntricos (eje dentro de agujero: las dos caras más cercanas son cilindros
        coaxiales) se calculan con la contracción de cada radio, sin depender del punto fijo. El
        resto es una estimación de primer orden: con materiales distintos depende del punto fijo;
        no considera gradientes ni que cambien los puntos más cercanos."""
        m = _modelo_nativo(conjunto)
        if m and not m.upper().endswith(".ASM"):
            raise ValueError(f"{m} no es un conjunto (.ASM): abre el conjunto o indica su nombre.")
        tabla = cargar_tabla()
        for mod, nom in (materiales or {}).items():
            if buscar_material(tabla, nom) is None:
                raise ValueError(f"Material '{nom}' ({mod}) no está en la tabla. Disponibles: "
                                 + ", ".join(x["nombre"] for x in tabla["materiales"]))
        sel = nat.ejecutar("seleccion") if fijo_en_seleccion else None
        pos = nat.ejecutar("posiciones", m)
        lista = piezas(pos)
        if lista and all(p["caja"] is None for p in lista):
            raise ValueError("creo_iac_tk no devuelve las cajas de las piezas: hace falta la versión 0.6.0 o "
                             "posterior (native\\compilar.cmd).")
        conj = nombre_fichero_nat(pos["modelo"]["nombre"], pos["modelo"]["tipo"])
        unidad = pos.get("unidad_longitud")
        excluir = None if incluir_tornilleria else (lambda nombre: _tornilleria(nombre) is not None)
        cand, total = candidatos(lista, umbral, excluir, max_pares)
        if cand:
            res = nat.ejecutar("holguras", m, pares=[[lista[i]["ruta"], lista[j]["ruta"]] for i, j, _ in cand])
        else:
            res = {"pares": [], "calculados": 0}
        if sel is not None:
            pf, pf_origen = punto_de_seleccion(sel, pos)
        elif punto_fijo:
            pf, pf_origen = tuple(punto_fijo), "indicado"
        else:
            pf, pf_origen = (0.0, 0.0, 0.0), "origen del conjunto (no se indicó por dónde se sujeta)"
        rev = revisar_holguras(lista, cand, res, umbral, holgura_minima, temperatura, temperaturas, materiales,
                               pf, tabla)
        avisos = list(rev["avisos"])
        if unidad and str(unidad).lower() not in ("mm", "millimeter", "millimetre", "milímetro"):
            avisos.insert(0, f"El conjunto está en {unidad}: umbral, holguras y punto fijo van en esa unidad.")
        if total > len(cand):
            avisos.append(f"Hay {total} pares de cajas cercanas y solo se han calculado {len(cand)} (max_pares).")
        mats: dict[str, Any] = {}
        if temperatura is not None or temperaturas:
            tmp = {k.upper(): v for k, v in (temperaturas or {}).items()}
            mt = {k.upper(): v for k, v in (materiales or {}).items()}
            for p in lista:
                if p["modelo"] in mats:
                    continue
                nombre_mat = mt.get(p["modelo"]) or p.get("material")
                fila = buscar_material(tabla, nombre_mat)
                t = tmp.get(p["modelo"], temperatura)
                mats[p["modelo"]] = {"material_creo": p.get("material"), "material_tabla": fila["nombre"] if fila else None,
                                     "temperatura": t,
                                     "contraccion": round(contraccion(fila, t), 6) if fila and t is not None else None}
        filas = rev["filas"]
        fichero = None
        if settings.export_enabled:
            fname, ruta = _nuevo_fichero("HOLGURAS", conj, nombre_fichero)
            u = unidad or "?"
            frio = temperatura is not None or bool(temperaturas)
            cab = ["Estado", "Pieza A", "Pieza B", f"Holgura ({u})", "Punto A", "Punto B", "Geometría"]
            if frio:
                cab += ["Material A", "Material B", "T A (K)", "T B (K)", "ΔL/L A", "ΔL/L B",
                        f"Holgura en frío ({u})", f"Cambio ({u})", "Estado en frío", "Cálculo en frío"]
            hoja = [_cab(*cab)]
            for f in filas:
                fila = [f["estado"], f["a"], f["b"], (f["distancia"], xlsx_mod.NUM3),
                        " ".join(f"{c:g}" for c in f["punto_a"]), " ".join(f"{c:g}" for c in f["punto_b"]),
                        "con superficies o facetas (pieza sin sólido)" if f.get("superficies") else "sólidos"]
                if frio:
                    fila += [f.get("material_a") or "", f.get("material_b") or "", f.get("temperatura_a"),
                             f.get("temperatura_b"), f.get("contraccion_a"), f.get("contraccion_b"),
                             (f["distancia_frio"], xlsx_mod.NUM3) if "distancia_frio" in f else "",
                             (f["cambio"], xlsx_mod.NUM3) if "cambio" in f else "", f.get("estado_frio", ""),
                             (f"concéntrico (R {f['radio_exterior']:g} / {f['radio_interior']:g})"
                              if f.get("ajuste") == "concéntrico" else
                              ("contracción alrededor del punto fijo" if "distancia_frio" in f else ""))]
                hoja.append(fila)
            resumen = [_cab("Dato", "Valor"), ["Conjunto", conj], ["Fecha", f"{datetime.now():%Y-%m-%d %H:%M}"],
                       ["Herramienta", f"creo-mcp-iac {__version__} / creo_iac_tk {res.get('version_tk') or pos.get('version_tk')}"],
                       ["Unidad de longitud", u], ["Umbral", umbral], ["Holgura mínima", holgura_minima],
                       ["Tornillería", "incluida" if incluir_tornilleria else "excluida"],
                       ["Pares calculados en Creo", rev["calculados"]], ["Pares listados", len(filas)]]
            resumen += [[f"  {k}", v] for k, v in rev["por_estado"].items()]
            if frio:
                resumen += [["Temperatura (K)", temperatura],
                            ["Punto fijo", " ".join(f"{c:.3f}" for c in pf) + f" ({pf_origen})"],
                            ["Tabla de contracción", Path(tabla["_fichero"]).name]]
                resumen += [[f"  en frío: {k}", v] for k, v in rev["por_estado_frio"].items()]
            resumen += [[a] for a in avisos]
            resumen += [[""], ["Borrador generado por el asistente. La holgura en frío es una estimación de primer "
                               "orden (contracción alrededor del punto fijo); comprobar las críticas con un análisis."]]
            hojas = [{"nombre": "Resumen", "filas": resumen, "anchos": [34, 70]},
                     {"nombre": "Holguras", "filas": hoja, "fijar_cabecera": True, "autofiltro": len(hoja),
                      "anchos": [16, 34, 34, 12, 26, 26, 22] + ([22, 22, 8, 8, 11, 11, 14, 12, 20, 30] if frio else [])}]
            if rev["lista_fallidos"]:
                hf = [_cab("Pieza A", "Pieza B", "Ruta A", "Ruta B", "Código", "Error de Creo Toolkit")]
                hf += [[x["a"], x["b"], "/".join(map(str, x["ruta_a"])), "/".join(map(str, x["ruta_b"])), x["codigo"],
                        x["error"]] for x in rev["lista_fallidos"]]
                hojas.append({"nombre": "Sin calcular", "filas": hf, "anchos": [36, 36, 14, 14, 8, 50],
                              "fijar_cabecera": True})
            if mats:
                hm = [_cab("Pieza", "Material en Creo", "Material de la tabla", "T (K)", "ΔL/L")]
                hm += [[k, v["material_creo"] or "", v["material_tabla"] or "SIN DATOS", v["temperatura"],
                        v["contraccion"] if v["contraccion"] is not None else ""] for k, v in sorted(mats.items())]
                hojas.append({"nombre": "Materiales", "filas": hm, "anchos": [36, 26, 32, 8, 12],
                              "fijar_cabecera": True})
            xlsx_mod.escribir(ruta, hojas)
            fichero = fname
        problemas = [f for f in filas if f["estado"] not in ("correcta", "contacto")
                     or f.get("estado_frio") not in (None, "correcta", "no evaluada (contacto)")]
        return {"conjunto": conj, "unidad_longitud": unidad, "umbral": umbral, "holgura_minima": holgura_minima,
                "piezas": len(lista), "pares_candidatos": total, "pares_calculados": rev["calculados"],
                "segundos_creo": rev["segundos"], "pares_listados": len(filas), "por_estado": rev["por_estado"],
                **({"temperatura": temperatura, "punto_fijo": [round(c, 3) for c in pf], "punto_fijo_origen": pf_origen,
                    "por_estado_frio": rev["por_estado_frio"],
                    "materiales": mats} if mats else {}),
                "problemas": problemas[:60], "problemas_mostrados": min(60, len(problemas)),
                "problemas_total": len(problemas),
                **({"pares_sin_calcular": rev["lista_fallidos"][:40]} if rev["lista_fallidos"] else {}),
                **({"avisos": avisos} if avisos else {}),
                "carpeta": str(settings.export_dir) if fichero else None, "fichero": fichero,
                "fuente": "Creo Toolkit (ProFitClearanceCompute) y tabla de contracción "
                          f"{Path(tabla['_fichero']).name}"}

    # ------------------------------------------------------------------
    # Operaciones sencillas (taladros) con Creo Toolkit

    def _pieza(modelo: str | None) -> str | None:
        m = _modelo_nativo(modelo)
        if m and not m.upper().endswith(".PRT"):
            raise ValueError(f"{m} no es una pieza (.PRT). Las operaciones se hacen en una pieza; ábrela en "
                             "su ventana o indica su nombre.")
        return m

    def _caras(modelo: str | None) -> dict[str, Any]:
        datos = nat.ejecutar("caras", modelo)
        u = (datos.get("unidad_longitud") or "").lower()
        if u and u not in ("mm", "millimeter", "millimetre", "milímetro"):
            raise ValueError(f"La pieza está en {datos.get('unidad_longitud')}: de momento las operaciones solo "
                             "se hacen en piezas en mm.")
        return datos

    @mcp.tool(annotations=READ)
    @guard
    def caras_creo(modelo: ModelArg = None) -> dict[str, Any]:
        """Caras planas, planos de referencia y cuerpo de una pieza en sesión (lectura nativa con
        Creo Toolkit). Sirve para decidir dónde va un taladro: cada cara con su id, orientación
        (+X…-Z si es perpendicular a un eje), normal exterior (hacia fuera del material), un punto
        de la cara, área y caja; y los planos de referencia (PERFIL, ALZADO, PLANTA…) con su normal
        y origen. Coordenadas de la pieza. No modifica Creo."""
        return {**resumir_caras(_caras(_pieza(modelo))),
                "fuente": "Creo Toolkit (creo_iac_tk), geometría de la pieza"}

    @mcp.tool(annotations=READ)
    @guard
    def seleccion_creo() -> dict[str, Any]:
        """Lo que el usuario tiene seleccionado ahora mismo en Creo: caras (con su id, normal y
        caja), puntos de referencia (con coordenadas), aristas, ejes, operaciones… y el punto donde
        pinchó, en coordenadas de la pieza. Úsala cuando el usuario diga «en esta cara» o «aquí»:
        pídele que seleccione en Creo y llama a esta herramienta. No modifica Creo."""
        datos = nat.ejecutar("seleccion")
        sel = resumir_seleccion(datos)
        r: dict[str, Any] = {"total": len(sel), "seleccion": sel}
        if not sel:
            r["nota"] = ("No hay nada seleccionado en Creo. Pide al usuario que seleccione la cara (o un punto de "
                         "referencia) en la ventana de la pieza, sin abrir ninguna herramienta, y repite.")
        return r

    TIPOS_OP = {0: "primera", 911: "taladro", 913: "redondeo", 914: "chaflán", 916: "corte", 917: "protrusión",
                920: "nervio", 923: "plano de referencia", 926: "eje", 927: "desmoldeo", 928: "vaciado",
                931: "punto", 932: "importada", 933: "cosmética", 942: "superficie", 979: "sistema de coordenadas",
                1070: "rosca cosmética", 1084: "copia de geometría", 1104: "cabecera de grupo",
                1232: "cabecera de patrón"}
    PATRON = {0: None, 1: "guía de patrón", 2: "miembro de patrón", 3: "cabecera de patrón"}

    @mcp.tool(annotations=READ)
    @guard
    def operaciones_creo(
        modelo: ModelArg = None,
        ids: Annotated[list[int] | None, Field(
            max_length=20, description="Ids de operaciones para volcar su árbol de elementos (diagnóstico)")] = None,
    ) -> dict[str, Any]:
        """Operaciones (features) de una pieza en sesión, leídas con Creo Toolkit: id, nombre, tipo,
        estado y si forman parte de un patrón. Con `ids`, vuelca el árbol de elementos de esas
        operaciones y el de su patrón (para desarrollar plantillas nuevas). No modifica Creo."""
        m = _pieza(modelo)
        datos = nat.ejecutar("arbol", m, ids=list(ids) if ids else None)
        mm = datos.get("modelo") or {}
        r: dict[str, Any] = {"modelo": f"{mm.get('nombre', '')}.{mm.get('tipo', 'PRT')}"}
        if ids:
            r["arboles"] = datos.get("arboles", [])
        else:
            ops = []
            for o in datos.get("operaciones", []):
                x = {"id": o["id"], "nombre": o.get("nombre"), "tipo": TIPOS_OP.get(o.get("tipo"), o.get("tipo")),
                     "activa": o.get("estado") == 0}
                if PATRON.get(o.get("patron")):
                    x["patron"] = PATRON[o["patron"]]
                ops.append(x)
            lim = limit(ops)
            r.update(operaciones=lim["items"], total=lim["count"], truncated=lim["truncated"])
        r["fuente"] = "Creo Toolkit (creo_iac_tk), árbol de operaciones"
        return r

    @mcp.tool(annotations=READ)
    @guard
    def proponer_taladros(
        taladros: Annotated[list[Taladro], Field(min_length=1, max_length=20,
                                                 description="Taladros que se quieren hacer en la pieza")],
        modelo: ModelArg = None,
    ) -> dict[str, Any]:
        """Prepara taladros en una pieza SIN tocar Creo: elige la cara y dos planos de referencia,
        calcula las cotas de colocación, toma de la tabla ISO de Creo la broca, la rosca o la holgura,
        y devuelve una tabla con cada taladro y un `codigo`. Enseña la tabla al usuario y, solo con su
        confirmación expresa, crea los taladros con crear_taladros(codigo). El código vale 30 min,
        una sola vez, y deja de valer si la pieza cambia.
        Tipos: recto (diámetro libre), roscado (métrica ISO; rosca por defecto 1,5·d), holgura (paso
        de tornillo, ajuste fino/medio/libre, pasante) y taladrado (broca de roscar, sin rosca).
        Los normalizados admiten cajeado (rebaje cilíndrico) o avellanado (cónico), con las medidas
        de la tabla de Creo salvo que se indiquen."""
        m = _pieza(modelo)
        caras = _caras(m)
        nota_desconocido = None
        if estado_ops["desconocido"]:
            nota_desconocido = ("La escritura anterior quedó con resultado desconocido: "
                                f"{estado_ops['desconocido']}. Comprueba en Creo si existe (taladros_creo lista "
                                "las operaciones IA_*) antes de confirmar esta propuesta.")
            estado_ops["desconocido"] = None
        tabla = ruta_tabla = None
        pets = [t.model_dump(exclude_none=True) if hasattr(t, "model_dump") else dict(t) for t in taladros]
        if any(p.get("tipo") != "recto" for p in pets):
            ruta_tabla = settings.tabla_hol("ISO")
            try:
                tabla = leer_hol(ruta_tabla)
            except OSError as e:
                raise ValueError(f"No se puede leer la tabla de taladros de Creo {ruta_tabla} ({e}). "
                                 "Indica la carpeta en CREO_MCP_HOL_DIR.") from e
        sello = f"{datetime.now():%H%M%S}"
        planes, filas, ids_caras, ids_planos = [], [], [], []
        for i, pet in enumerate(pets, 1):
            try:
                previo = planificar_taladro(pet, caras, tabla, "IA_X")
                nombre = nombre_operacion(previo["base_nombre"], f"{sello}_{i}")
                pl = planificar_taladro(pet, caras, tabla, nombre)
            except PlanError as e:
                raise ValueError(f"Taladro {i}: {e}") from e
            planes.append({"plan": pl["plan"], "resumen": {"n": i, **pl["resumen"]}})
            filas.append({"n": i, **pl["resumen"], **({"avisos": pl["avisos"]} if pl["avisos"] else {})})
            ids_caras += pl["ids_caras"]
            ids_planos += pl["ids_planos"]
        modelo_pieza = planes[0]["resumen"]["modelo"]
        codigo = hashlib.sha256(("".join(p["plan"] for p in planes)).encode()).hexdigest()[:8].upper()
        ahora = time.time()
        for k in [k for k, v in propuestas.items() if ahora - v["creada"] > PROPUESTA_VALIDA_S]:
            propuestas.pop(k, None)
        while len(propuestas) >= 10:
            propuestas.pop(min(propuestas, key=lambda k: propuestas[k]["creada"]))
        propuestas[codigo] = {"modelo": modelo_pieza, "planes": planes, "creada": ahora,
                              "huella": huella(caras, ids_caras, ids_planos), "ids_caras": ids_caras,
                              "ids_planos": ids_planos}
        gw.audit.write(event="propuesta_operaciones", modelo=modelo_pieza, codigo=codigo, n=len(planes))
        crear_activo = settings.allow_operaciones
        r: dict[str, Any] = {
            "modelo": modelo_pieza, "unidad_longitud": caras.get("unidad_longitud"), "codigo": codigo,
            "taladros": filas, "crear_activo": crear_activo,
            "tabla_roscas": str(ruta_tabla) if ruta_tabla else None,
            "siguiente": ("Enseña esta tabla al usuario (tipo, tamaño, profundidad, cara, punto y cotas) y pide su "
                          "confirmación expresa. Solo entonces llama a crear_taladros con este código."
                          if crear_activo else
                          "La creación está desactivada en esta instalación (CREO_MCP_ALLOW_OPERACIONES=1 y servicio "
                          "con --escritura). La tabla sirve para hacerlos a mano en Creo."),
            "fuente": "Creo Toolkit (caras de la pieza) y tabla de taladros de Creo",
        }
        if nota_desconocido:
            r["resultado_desconocido_anterior"] = nota_desconocido
        return r

    def _guia_patron(m: str | None, guia: int) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        """Taladro guía (resumido, con su cara de colocación), el resto de taladros y el modelo."""
        datos = nat.ejecutar("taladros", m)
        modelo_pieza = None
        guia_op, otros = None, []
        for mm in datos.get("modelos", []):
            modelo_pieza = f"{mm['nombre']}.PRT"
            for op in mm.get("operaciones", []):
                res = resumir_operacion(op)
                ref = (op.get("elementos") or {}).get("386.1340.1341")
                if isinstance(ref, str) and ref.startswith("ref:5:"):
                    res["cara"] = int(ref.split(":")[2])
                if op.get("id") == guia:
                    guia_op = res
                else:
                    otros.append(res)
        if guia_op is None:
            raise ValueError(f"No hay ningún taladro con id {guia} en la pieza (taladros_creo da los ids).")
        if "cara" not in guia_op:
            raise ValueError(f"El taladro {guia} no está colocado sobre una cara plana con colocación lineal.")
        return guia_op, otros, modelo_pieza or ""

    @mcp.tool(annotations=READ)
    @guard
    def proponer_patron(
        guia: Annotated[int, Field(gt=0, description="Id del taladro que se repite (taladros_creo o crear_taladros)")],
        tipo: Annotated[Literal["lineal", "circular"], Field(description="lineal (1 o 2 direcciones) o circular (alrededor de un eje)")],
        direccion1: Annotated[str | None, Field(description="Lineal: +X, -X, +Y, -Y, +Z o -Z (coordenadas de la pieza)")] = None,
        separacion1: Annotated[float | None, Field(gt=0, description="Lineal: separación entre miembros (mm)")] = None,
        cantidad1: Annotated[int | None, Field(ge=2, le=50, description="Lineal: miembros en la dirección 1, incluido el guía")] = None,
        direccion2: Annotated[str | None, Field(description="Lineal, opcional: segunda dirección, perpendicular a la primera")] = None,
        separacion2: Annotated[float | None, Field(gt=0, description="Lineal: separación en la dirección 2 (mm)")] = None,
        cantidad2: Annotated[int | None, Field(ge=2, le=50, description="Lineal: miembros en la dirección 2")] = None,
        eje: Annotated[str | None, Field(description="Circular: nombre del eje de referencia (p. ej. Y o A_11)")] = None,
        cantidad: Annotated[int | None, Field(ge=2, le=100, description="Circular: miembros, incluido el guía")] = None,
        angulo: Annotated[float | None, Field(description="Circular: ángulo entre miembros (°). Vacío = 360/cantidad")] = None,
        modelo: ModelArg = None,
    ) -> dict[str, Any]:
        """Prepara un patrón de un taladro SIN tocar Creo: lineal en una o dos direcciones o circular
        alrededor de un eje de referencia. Calcula la posición de cada miembro, comprueba que caen
        dentro de la cara (si alguno sale de la pieza, no lo propone), que no se pisan entre sí y
        avisa si se pisan con otros taladros. Devuelve la tabla y un `codigo`: enséñala al usuario y,
        solo con su confirmación expresa, crea el patrón con crear_patron(codigo)."""
        m = _pieza(modelo)
        caras = _caras(m)
        guia_op, otros, _ = _guia_patron(m, guia)
        pet = {k: v for k, v in dict(tipo=tipo, direccion1=direccion1, separacion1=separacion1, cantidad1=cantidad1,
                                     direccion2=direccion2, separacion2=separacion2, cantidad2=cantidad2, eje=eje,
                                     cantidad=cantidad, angulo=angulo).items() if v is not None}
        nombre = nombre_operacion("PAT", f"{datetime.now():%H%M%S}")
        try:
            pl = planificar_patron(pet, caras, guia_op, otros, nombre)
        except PlanError as e:
            raise ValueError(str(e)) from e
        codigo = "P" + hashlib.sha256(pl["plan"].encode()).hexdigest()[:7].upper()
        ahora = time.time()
        for k in [k for k, v in propuestas_patron.items() if ahora - v["creada"] > PROPUESTA_VALIDA_S]:
            propuestas_patron.pop(k, None)
        propuestas_patron[codigo] = {"modelo": pl["resumen"]["modelo"], "plan": pl["plan"], "resumen": pl["resumen"],
                                     "creada": ahora, "guia": guia, "eje_guia": guia_op["eje"],
                                     "huella": huella(caras, pl["ids_caras"], [])}
        gw.audit.write(event="propuesta_patron", modelo=pl["resumen"]["modelo"], codigo=codigo,
                       miembros=pl["resumen"]["miembros"])
        r = {"codigo": codigo, **pl["resumen"], "crear_activo": settings.allow_operaciones,
             "siguiente": ("Enseña la tabla al usuario (guía, tipo, direcciones o eje, miembros y posiciones) y pide su "
                           "confirmación expresa. Solo entonces llama a crear_patron con este código."
                           if settings.allow_operaciones else
                           "La creación está desactivada en esta instalación (CREO_MCP_ALLOW_OPERACIONES=1)."),
             "fuente": "Creo Toolkit (caras y taladros de la pieza)"}
        if pl["avisos"]:
            r["avisos"] = pl["avisos"]
        return r

    @mcp.tool(annotations=READ)
    @guard
    def aristas_creo(
        modelo: ModelArg = None,
        forma: Annotated[Literal["concava", "convexa", "tangente"] | None, Field(
            description="concava = esquina interior (p. ej. de una cajera, la que la fresa no puede dejar viva); "
                        "convexa = esquina exterior. Vacío = todas")] = None,
        cara: Annotated[int | None, Field(description="Solo las aristas de esta cara (id de caras_creo)")] = None,
        longitud_min: Annotated[float, Field(ge=0, description="Longitud mínima (mm)")] = 0.0,
        direccion: Annotated[Literal["X", "Y", "Z"] | None, Field(
            description="Solo aristas rectas paralelas a este eje de la pieza: con forma=concava y el eje de "
                        "la fresa, son las esquinas interiores que la fresa deja con su radio")] = None,
    ) -> dict[str, Any]:
        """Aristas de una pieza (Creo Toolkit): id, recta o arco, longitud, punto medio, dirección,
        las dos caras que une y si es cóncava (esquina interior) o convexa. Sirve para elegir SOLO
        las aristas que necesitan radio para fabricarse: las esquinas interiores paralelas al eje de
        la fresa (forma=concava, direccion = eje de la fresa) o las que señale la revisión de
        fabricabilidad (se localizan por el punto medio). Las aristas del fondo de una cajera no
        necesitan radio con fresa plana. No modifica Creo."""
        datos = nat.ejecutar("aristas", _pieza(modelo))
        return {**resumir_aristas(datos, forma, cara, longitud_min, direccion=direccion),
                "fuente": "Creo Toolkit (creo_iac_tk), aristas de la pieza"}

    @mcp.tool(annotations=READ)
    @guard
    def proponer_redondeos(
        aristas: Annotated[list[int], Field(min_length=1, max_length=64,
                                            description="Ids de las aristas (aristas_creo o seleccion_creo)")],
        radio: Annotated[float, Field(gt=0, le=500, description="Radio (mm)")],
        modelo: ModelArg = None,
    ) -> dict[str, Any]:
        """Prepara un redondeo de radio constante sobre aristas de una pieza SIN tocar Creo: comprueba
        que existen y no son tangentes, y devuelve la tabla y un `codigo`. Para esquinas interiores
        de cajeras, el radio debe ser mayor que el de la fresa que se vaya a usar (pregúntalo o usa
        el de la revisión de fabricabilidad). Enseña la tabla y, solo con la confirmación expresa del
        usuario, crea con crear_redondeos(codigo). Un radio por propuesta."""
        m = _pieza(modelo)
        datos = nat.ejecutar("aristas", m)
        nombre = nombre_operacion(f"RED_R{radio:g}", f"{datetime.now():%H%M%S}")
        try:
            pl = planificar_redondeo({"aristas": list(aristas), "radio": radio}, datos, nombre)
        except PlanError as e:
            raise ValueError(str(e)) from e
        codigo = "R" + hashlib.sha256(pl["plan"].encode()).hexdigest()[:7].upper()
        ahora = time.time()
        for k in [k for k, v in propuestas_redondeo.items() if ahora - v["creada"] > PROPUESTA_VALIDA_S]:
            propuestas_redondeo.pop(k, None)
        propuestas_redondeo[codigo] = {"modelo": pl["resumen"]["modelo"], "plan": pl["plan"], "resumen": pl["resumen"],
                                       "creada": ahora, "ids": list(aristas),
                                       "huella": huella_aristas(datos, list(aristas))}
        gw.audit.write(event="propuesta_redondeo", modelo=pl["resumen"]["modelo"], codigo=codigo, n=len(aristas))
        r = {"codigo": codigo, **pl["resumen"], "crear_activo": settings.allow_operaciones,
             "siguiente": ("Enseña la tabla (aristas, forma, longitud, punto medio y radio) y pide confirmación "
                           "expresa. Solo entonces llama a crear_redondeos con este código."
                           if settings.allow_operaciones else
                           "La creación está desactivada en esta instalación (CREO_MCP_ALLOW_OPERACIONES=1)."),
             "fuente": "Creo Toolkit (aristas de la pieza)"}
        if pl["avisos"]:
            r["avisos"] = pl["avisos"]
        return r

    @mcp.tool(annotations=READ)
    @guard
    def proponer_chaflanes(
        aristas: Annotated[list[int], Field(min_length=1, max_length=64,
                                            description="Ids de las aristas (aristas_creo o seleccion_creo)")],
        distancia: Annotated[float, Field(gt=0, le=200, description="Distancia D del chaflán (mm)")],
        esquema: Annotated[Literal["45xD", "DxD"], Field(
            description="45xD: chaflán a 45° de lado D (lo habitual: Ch D×45°). DxD: misma distancia D a las dos "
                        "caras (igual que 45xD si las caras forman 90°)")] = "45xD",
        modelo: ModelArg = None,
    ) -> dict[str, Any]:
        """Prepara un chaflán de distancia constante sobre aristas de una pieza SIN tocar Creo
        (romper aristas vivas, entradas de taladros o de ejes). Lo normal es achaflanar aristas
        exteriores (convexas); avisa si alguna es interior. Devuelve la tabla y un `codigo`: enséñala
        y, solo con la confirmación expresa del usuario, crea con crear_chaflanes(codigo)."""
        m = _pieza(modelo)
        datos = nat.ejecutar("aristas", m)
        nombre = nombre_operacion(f"CHF_{distancia:g}", f"{datetime.now():%H%M%S}")
        try:
            pl = planificar_chaflan({"aristas": list(aristas), "distancia": distancia, "esquema": esquema}, datos, nombre)
        except PlanError as e:
            raise ValueError(str(e)) from e
        codigo = "C" + hashlib.sha256(pl["plan"].encode()).hexdigest()[:7].upper()
        ahora = time.time()
        for k in [k for k, v in propuestas_redondeo.items() if ahora - v["creada"] > PROPUESTA_VALIDA_S]:
            propuestas_redondeo.pop(k, None)
        propuestas_redondeo[codigo] = {"modelo": pl["resumen"]["modelo"], "plan": pl["plan"], "resumen": pl["resumen"],
                                       "creada": ahora, "ids": list(aristas), "clase": "chaflan",
                                       "huella": huella_aristas(datos, list(aristas))}
        gw.audit.write(event="propuesta_chaflan", modelo=pl["resumen"]["modelo"], codigo=codigo, n=len(aristas))
        r = {"codigo": codigo, **pl["resumen"], "crear_activo": settings.allow_operaciones,
             "siguiente": ("Enseña la tabla (aristas, forma, longitud, punto medio, esquema y distancia) y pide "
                           "confirmación expresa. Solo entonces llama a crear_chaflanes con este código."
                           if settings.allow_operaciones else
                           "La creación está desactivada en esta instalación (CREO_MCP_ALLOW_OPERACIONES=1)."),
             "fuente": "Creo Toolkit (aristas de la pieza)"}
        if pl["avisos"]:
            r["avisos"] = pl["avisos"]
        return r

    @mcp.tool(annotations=READ)
    @guard
    def proponer_extrusion(
        tipo: Annotated[Literal["anadir", "quitar"], Field(
            description="anadir: extrusión que añade material; quitar: corte que quita material")],
        plano: Annotated[str, Field(description="Plano del croquis: nombre de un plano de referencia (FRONT, TOP, "
                                                "RIGHT, ALZADO…) o id de una cara plana (caras_creo o seleccion_creo). "
                                                "Debe ser normal a X, Y o Z")],
        contornos: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=100, description=(
            "Contornos cerrados en coordenadas de la pieza dentro del plano (normal a Z: [x, y]; a Y: [x, z]; "
            "a X: [y, z]). Tipos: {tipo:'circulo', centro:[u,v], diametro}; {tipo:'rectangulo', centro|esquina:[u,v], "
            "ancho (en u), alto (en v), radio_esquinas?}; {tipo:'ranura', centro1, centro2, ancho}; "
            "{tipo:'perfil', puntos:[[u,v], [u,v], {hasta:[u,v], por:[u,v]}, {hasta, centro, giro:'horario'|"
            "'antihorario'}, …]} (se cierra solo). Un contorno dentro de otro es un hueco"))],
        extension: Annotated[Literal["ciega", "simetrica", "pasante", "pasante_ambos"], Field(
            description="ciega: profundidad hacia `sentido`; simetrica: profundidad repartida a los dos lados del "
                        "plano; pasante: corte hasta atravesar la pieza hacia `sentido`; pasante_ambos: corte que "
                        "atraviesa a los dos lados (pasante solo para quitar)")] = "ciega",
        profundidad: Annotated[float | None, Field(gt=0, le=10000, description="mm (ciega o simétrica)")] = None,
        sentido: Annotated[str | None, Field(description="+X, -X, +Y, -Y, +Z o -Z, el eje normal al plano "
                                                         "(ciega o pasante)")] = None,
        modelo: ModelArg = None,
    ) -> dict[str, Any]:
        """Prepara una extrusión (añadir material) o un corte (quitar material) con croquis SIN
        tocar Creo. Sirve para levantar una pieza a partir de un boceto acotado: primero una extrusión
        con el contorno exterior, luego cortes, taladros, redondeos y chaflanes. Para empezar una pieza
        nueva, el usuario crea en Creo una pieza vacía con la plantilla del IAC y la deja abierta.
        Calcula el área y el volumen teórico y comprueba que los contornos no se corten. El croquis se
        acota con las cotas del boceto (diámetros, radios, posición de centros y vértices desde los planos
        de referencia, ancho y alto); Creo completa lo que falte. Para que las cotas salgan como en el
        dibujo, da los arcos por su centro cuando el dibujo lo acote. Devuelve la tabla y un `codigo`:
        enséñala (contornos con sus medidas, cotas, plano, extensión, sentido) y, solo con la confirmación
        expresa del usuario, crea con crear_extrusion(codigo)."""
        m = _pieza(modelo)
        caras = _caras(m)
        nombre = nombre_operacion("EXT" if tipo == "anadir" else "CORTE", f"{datetime.now():%H%M%S}")
        try:
            pl = planificar_extrusion({"tipo": tipo, "plano": plano, "contornos": contornos, "extension": extension,
                                       "profundidad": profundidad, "sentido": sentido}, caras, nombre)
        except PlanError as e:
            raise ValueError(str(e)) from e
        codigo = "X" + hashlib.sha256(pl["plan"].encode()).hexdigest()[:7].upper()
        ahora = time.time()
        for k in [k for k, v in propuestas_ext.items() if ahora - v["creada"] > PROPUESTA_VALIDA_S]:
            propuestas_ext.pop(k, None)
        while len(propuestas_ext) >= 10:
            propuestas_ext.pop(min(propuestas_ext, key=lambda k: propuestas_ext[k]["creada"]))
        propuestas_ext[codigo] = {"modelo": pl["resumen"]["modelo"], "plan": pl["plan"], "resumen": pl["resumen"],
                                  "creada": ahora, "ids_caras": pl["ids_caras"], "ids_planos": pl["ids_planos"],
                                  "huella": huella(caras, pl["ids_caras"], pl["ids_planos"]), "clase": "extrusion"}
        gw.audit.write(event="propuesta_extrusion", modelo=pl["resumen"]["modelo"], codigo=codigo, tipo=tipo,
                       contornos=len(contornos))
        r = {"codigo": codigo, **pl["resumen"], "crear_activo": settings.allow_operaciones,
             "siguiente": ("Enseña la tabla (contornos con sus medidas, plano del croquis, extensión y sentido, "
                           "volumen teórico) y pide confirmación expresa. Solo entonces llama a crear_extrusion "
                           "con este código." if settings.allow_operaciones else
                           "La creación está desactivada en esta instalación (CREO_MCP_ALLOW_OPERACIONES=1)."),
             "fuente": "Creo Toolkit (caras y planos de la pieza)"}
        if pl["avisos"]:
            r["avisos"] = pl["avisos"]
        return r

    @mcp.tool(annotations=READ)
    @guard
    def proponer_revolucion(
        tipo: Annotated[Literal["anadir", "quitar"], Field(
            description="anadir: revolución que añade material (piezas torneadas); quitar: corte por revolución "
                        "(gargantas, cajeados interiores, chaflanes de torneado)")],
        plano: Annotated[str, Field(description="Plano del croquis que contiene el eje de giro: nombre de un plano de "
                                                "referencia (ALZADO, PERFIL…) o id de una cara plana; normal a X, Y o Z")],
        eje: Annotated[Literal["X", "Y", "Z"], Field(description="Dirección del eje de giro (en el plano del croquis)")],
        contornos: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=100, description=(
            "Media sección, a un lado del eje, con los mismos tipos de contorno que proponer_extrusion y en "
            "coordenadas de la pieza dentro del plano (normal a Z: [x, y]; a Y: [x, z]; a X: [y, z]). Lo habitual "
            "es un 'perfil' con los escalones del eje: [[0,0],[10,0],[10,30],[6,30],[6,50],[0,50]] para un eje "
            "Ø20×30 + Ø12×20 que gira sobre Y"))],
        eje_desplazado: Annotated[float, Field(description="Posición del eje de giro en la otra coordenada del plano "
                                                           "(0 = pasa por el origen)")] = 0.0,
        angulo: Annotated[float, Field(gt=0, le=360, description="Grados; 360 = vuelta completa, menos de 360 "
                                                                   "simétrico respecto al plano del croquis")] = 360.0,
        modelo: ModelArg = None,
    ) -> dict[str, Any]:
        """Prepara una revolución (pieza torneada: ejes, casquillos, bridas, poleas) o un corte por
        revolución SIN tocar Creo. El croquis es media sección a un lado del eje de giro; el eje es una
        línea de centros del croquis paralela a X, Y o Z. Las cotas perpendiculares al eje se ponen como
        diámetros (como en un plano de torno) y las axiales desde el plano de referencia. Calcula el
        volumen teórico (Pappus). Devuelve la tabla y un `codigo`: enséñala y, solo con la confirmación
        expresa del usuario, crea con crear_revolucion(codigo)."""
        m = _pieza(modelo)
        caras = _caras(m)
        nombre = nombre_operacion("REV" if tipo == "anadir" else "CORTE_REV", f"{datetime.now():%H%M%S}")
        try:
            pl = planificar_revolucion({"tipo": tipo, "plano": plano, "eje": eje, "contornos": contornos,
                                        "eje_desplazado": eje_desplazado, "angulo": angulo}, caras, nombre)
        except PlanError as e:
            raise ValueError(str(e)) from e
        codigo = "R" + hashlib.sha256(pl["plan"].encode()).hexdigest()[:7].upper()
        ahora = time.time()
        for k in [k for k, v in propuestas_ext.items() if ahora - v["creada"] > PROPUESTA_VALIDA_S]:
            propuestas_ext.pop(k, None)
        while len(propuestas_ext) >= 10:
            propuestas_ext.pop(min(propuestas_ext, key=lambda k: propuestas_ext[k]["creada"]))
        propuestas_ext[codigo] = {"modelo": pl["resumen"]["modelo"], "plan": pl["plan"], "resumen": pl["resumen"],
                                  "creada": ahora, "ids_caras": pl["ids_caras"], "ids_planos": pl["ids_planos"],
                                  "huella": huella(caras, pl["ids_caras"], pl["ids_planos"]), "clase": "revolucion"}
        gw.audit.write(event="propuesta_revolucion", modelo=pl["resumen"]["modelo"], codigo=codigo, tipo=tipo,
                       contornos=len(contornos))
        r = {"codigo": codigo, **pl["resumen"], "crear_activo": settings.allow_operaciones,
             "siguiente": ("Enseña la tabla (media sección con sus medidas, eje de giro, ángulo, cotas y volumen "
                           "teórico) y pide confirmación expresa. Solo entonces llama a crear_revolucion con este "
                           "código." if settings.allow_operaciones else
                           "La creación está desactivada en esta instalación (CREO_MCP_ALLOW_OPERACIONES=1)."),
             "fuente": "Creo Toolkit (caras y planos de la pieza)"}
        if pl["avisos"]:
            r["avisos"] = pl["avisos"]
        return r

    if settings.allow_operaciones:

        @mcp.tool(annotations=DRAW_DESTRUCTIVE)
        @guard
        @tareas.larga
        def crear_taladros(
            codigo: Annotated[str, Field(description="Código que devolvió proponer_taladros")],
        ) -> dict[str, Any]:
            """Crea en Creo los taladros de una propuesta ya confirmada por el usuario.

            Solo con la confirmación expresa del usuario sobre la tabla de proponer_taladros. Vuelve
            a leer la pieza y, si ha cambiado, no crea nada. Cada taladro se crea con nombre IA_* y
            se comprueba que su eje pasa por el punto pedido (si no, se borra y se prueba el otro
            sentido de las cotas). Nunca guarda: el usuario revisa la pieza y guarda. Para deshacer,
            borrar_operaciones_ia con los ids creados. Si responde «resultado_desconocido», no
            repitas: comprueba Creo con taladros_creo."""
            if estado_ops["desconocido"]:
                raise ValueError("Hay una escritura anterior con resultado desconocido. Comprueba Creo (taladros_creo) "
                                 "y vuelve a proponer los taladros antes de crear nada.")
            p = propuestas.pop((codigo or "").strip().upper(), None)
            if p is None:
                raise ValueError("No hay ninguna propuesta con ese código (vale una sola vez, 30 min, y se pierde al "
                                 "reiniciar Claude). Vuelve a llamar a proponer_taladros.")
            if time.time() - p["creada"] > PROPUESTA_VALIDA_S:
                raise ValueError("La propuesta ha caducado (30 min). Vuelve a llamar a proponer_taladros.")
            caras = nat.ejecutar("caras", p["modelo"])
            if huella(caras, p["ids_caras"], p["ids_planos"]) != p["huella"]:
                raise ValueError("La pieza ha cambiado desde la propuesta (caras o planos de referencia): no se ha "
                                 "creado nada. Vuelve a llamar a proponer_taladros.")
            creados: list[dict[str, Any]] = []
            fallidos: list[dict[str, Any]] = []
            for i, pl in enumerate(p["planes"]):
                try:
                    r = nat.ejecutar("crear", plan=pl["plan"])
                except NativoSinRespuesta as e:
                    estado_ops["desconocido"] = {**pl["resumen"], "error": str(e)[:200]}
                    gw.audit.write(event="resultado_desconocido", tool="crear_taladros", modelo=p["modelo"],
                                   nombre=pl["resumen"].get("nombre"))
                    return {"estado": "resultado_desconocido", "modelo": p["modelo"], "creados": creados,
                            "resultado_desconocido": pl["resumen"],
                            "no_intentados": [x["resumen"] for x in p["planes"][i + 1:]], "fallidos": fallidos,
                            "que_hacer": "Creo no respondió a tiempo: el taladro puede haberse creado o no. Espera "
                                         "a que Creo responda y comprueba con taladros_creo si existe "
                                         f"{pl['resumen'].get('nombre')}. Hasta entonces no se crea nada más."}
                except NativoError as e:
                    fallidos.append({**pl["resumen"], "error": str(e)})
                    continue
                op = r.get("operacion") or {}
                creados.append({**pl["resumen"], "id": op.get("id"), "nombre": op.get("nombre") or pl["resumen"]["nombre"],
                                "intentos": op.get("intentos"), "desviacion_eje_mm": op.get("desviacion")})
                gw.audit.write(event="operacion_creada", modelo=p["modelo"], id=op.get("id"),
                               nombre=op.get("nombre"), intentos=op.get("intentos"))
            return {
                "estado": "ok" if not fallidos else ("con_incidencias" if creados else "sin_crear"),
                "modelo": p["modelo"], "creados": creados, "fallidos": fallidos,
                "ids_creados": [c["id"] for c in creados],
                "aviso": "Pieza modificada en la sesión de Creo, SIN GUARDAR. Revísala en Creo y guarda tú si estás "
                         "conforme. Para deshacer: borrar_operaciones_ia con ids_creados (o bórralos a mano).",
            }

        @mcp.tool(annotations=DRAW_DESTRUCTIVE)
        @guard
        @tareas.larga
        def crear_patron(
            codigo: Annotated[str, Field(description="Código que devolvió proponer_patron")],
        ) -> dict[str, Any]:
            """Crea en Creo el patrón de una propuesta ya confirmada por el usuario.

            Solo con la confirmación expresa del usuario sobre la tabla de proponer_patron. Vuelve a
            leer la pieza y el taladro guía: si han cambiado, no crea nada. El patrón se llama IA_PAT_*
            y se comprueba que cada miembro queda en la posición calculada; si no, se borra y se
            prueba el otro sentido de las direcciones. Nunca guarda. Para deshacer,
            borrar_operaciones_ia con el id del patrón (deja el taladro guía)."""
            if estado_ops["desconocido"]:
                raise ValueError("Hay una escritura anterior con resultado desconocido. Comprueba Creo (taladros_creo) "
                                 "y vuelve a proponer antes de crear nada.")
            p = propuestas_patron.pop((codigo or "").strip().upper(), None)
            if p is None:
                raise ValueError("No hay ninguna propuesta de patrón con ese código (vale una vez, 30 min). "
                                 "Vuelve a llamar a proponer_patron.")
            if time.time() - p["creada"] > PROPUESTA_VALIDA_S:
                raise ValueError("La propuesta ha caducado (30 min). Vuelve a llamar a proponer_patron.")
            caras = nat.ejecutar("caras", p["modelo"])
            guia_op, _, _ = _guia_patron(p["modelo"], p["guia"])
            if huella(caras, [p["resumen"]["cara"]], []) != p["huella"] or guia_op["eje"] != p["eje_guia"]:
                raise ValueError("La pieza o el taladro guía han cambiado desde la propuesta: no se ha creado nada. "
                                 "Vuelve a llamar a proponer_patron.")
            try:
                r = nat.ejecutar("crear", plan=p["plan"])
            except NativoSinRespuesta as e:
                estado_ops["desconocido"] = {**p["resumen"], "error": str(e)[:200]}
                gw.audit.write(event="resultado_desconocido", tool="crear_patron", modelo=p["modelo"])
                return {"estado": "resultado_desconocido", "patron": p["resumen"],
                        "que_hacer": "Creo no respondió a tiempo: el patrón puede haberse creado o no. Comprueba con "
                                     "operaciones_creo si existe " + str(p["resumen"].get("nombre")) + "."}
            op = r.get("operacion") or {}
            gw.audit.write(event="operacion_creada", modelo=p["modelo"], id=op.get("id"), nombre=op.get("nombre"),
                           intentos=op.get("intentos"))
            return {"estado": "ok", "modelo": p["modelo"],
                    "patron": {**p["resumen"], "id": op.get("id"), "nombre": op.get("nombre") or p["resumen"]["nombre"],
                               "intentos": op.get("intentos"), "desviacion_mm": op.get("desviacion"),
                               "miembros_creados": [m_.get("id") for m_ in op.get("miembros", [])]},
                    "aviso": "Pieza modificada en la sesión de Creo, SIN GUARDAR. Revísala y guarda tú si estás conforme. "
                             "Para deshacer: borrar_operaciones_ia con el id del patrón."}

        @mcp.tool(annotations=DRAW_DESTRUCTIVE)
        @guard
        @tareas.larga
        def crear_redondeos(
            codigo: Annotated[str, Field(description="Código que devolvió proponer_redondeos")],
        ) -> dict[str, Any]:
            """Crea en Creo el redondeo de una propuesta ya confirmada por el usuario.

            Solo con la confirmación expresa del usuario sobre la tabla de proponer_redondeos. Vuelve
            a leer las aristas: si han cambiado, no crea nada. El redondeo se llama IA_RED_* y se
            comprueba que las aristas han desaparecido (si no, se borra). Nunca guarda. Para deshacer,
            borrar_operaciones_ia con su id."""
            return _crear_rndch(codigo, "redondeo")

        @mcp.tool(annotations=DRAW_DESTRUCTIVE)
        @guard
        @tareas.larga
        def crear_chaflanes(
            codigo: Annotated[str, Field(description="Código que devolvió proponer_chaflanes")],
        ) -> dict[str, Any]:
            """Crea en Creo el chaflán de una propuesta ya confirmada por el usuario.

            Solo con la confirmación expresa del usuario sobre la tabla de proponer_chaflanes. Vuelve
            a leer las aristas: si han cambiado, no crea nada. El chaflán se llama IA_CHF_* y se
            comprueba que las aristas han desaparecido (si no, se borra). Nunca guarda. Para deshacer,
            borrar_operaciones_ia con su id."""
            return _crear_rndch(codigo, "chaflan")

        def _crear_rndch(codigo: str, clase: str) -> dict[str, Any]:
            herramienta = "crear_redondeos" if clase == "redondeo" else "crear_chaflanes"
            propuesta = "proponer_redondeos" if clase == "redondeo" else "proponer_chaflanes"
            if estado_ops["desconocido"]:
                raise ValueError("Hay una escritura anterior con resultado desconocido. Comprueba Creo "
                                 "(operaciones_creo) y vuelve a proponer antes de crear nada.")
            clave = (codigo or "").strip().upper()
            p = propuestas_redondeo.get(clave)
            if p is None or p.get("clase", "redondeo") != clase:
                raise ValueError(f"No hay ninguna propuesta de {clase} con ese código (vale una vez, 30 min). "
                                 f"Vuelve a llamar a {propuesta}.")
            propuestas_redondeo.pop(clave, None)
            if time.time() - p["creada"] > PROPUESTA_VALIDA_S:
                raise ValueError(f"La propuesta ha caducado (30 min). Vuelve a llamar a {propuesta}.")
            datos = nat.ejecutar("aristas", p["modelo"])
            if huella_aristas(datos, p["ids"]) != p["huella"]:
                raise ValueError("Las aristas han cambiado desde la propuesta: no se ha creado nada. "
                                 f"Vuelve a llamar a aristas_creo y {propuesta}.")
            try:
                r = nat.ejecutar("crear", plan=p["plan"])
            except NativoSinRespuesta as e:
                estado_ops["desconocido"] = {**p["resumen"], "error": str(e)[:200]}
                gw.audit.write(event="resultado_desconocido", tool=herramienta, modelo=p["modelo"])
                return {"estado": "resultado_desconocido", clase: p["resumen"],
                        "que_hacer": f"Creo no respondió a tiempo: el {clase} puede haberse creado o no. Comprueba con "
                                     "operaciones_creo si existe " + str(p["resumen"].get("nombre")) + "."}
            op = r.get("operacion") or {}
            # Comprobación: las aristas redondeadas ya no están en la pieza.
            quedan = []
            try:
                despues = nat.ejecutar("aristas", p["modelo"])
                vivas = {x["id"] for x in despues.get("aristas", [])}
                quedan = [i for i in p["ids"] if i in vivas]
            except NativoError:
                pass
            if quedan and op.get("id"):
                try:
                    nat.ejecutar("borrar", p["modelo"], ids=[op["id"]])
                    borrado = True
                except NativoError:
                    borrado = False
                gw.audit.write(event="verificacion_fallida", tool=herramienta, modelo=p["modelo"], quedan=quedan)
                raise ValueError(f"El {clase} se creó pero las aristas {quedan} siguen en la pieza: "
                                 + ("se ha borrado." if borrado else f"NO se pudo borrar (id {op['id']}); bórralo a mano."))
            gw.audit.write(event="operacion_creada", modelo=p["modelo"], id=op.get("id"), nombre=op.get("nombre"))
            return {"estado": "ok", "modelo": p["modelo"], "aristas_comprobadas": True,
                    clase: {**p["resumen"], "id": op.get("id"), "nombre": op.get("nombre") or p["resumen"]["nombre"]},
                    "aviso": "Pieza modificada en la sesión de Creo, SIN GUARDAR. Revísala y guarda tú si estás conforme. "
                             f"Para deshacer: borrar_operaciones_ia con el id del {clase}."}

        @mcp.tool(annotations=DRAW_DESTRUCTIVE)
        @guard
        @tareas.larga
        def crear_extrusion(
            codigo: Annotated[str, Field(description="Código que devolvió proponer_extrusion")],
        ) -> dict[str, Any]:
            """Crea en Creo la extrusión o el corte de una propuesta ya confirmada por el usuario.

            Solo con la confirmación expresa del usuario sobre la tabla de proponer_extrusion. Vuelve
            a leer la pieza: si el plano o las referencias han cambiado, no crea nada. La operación se
            llama IA_EXT_* o IA_CORTE_*; se comprueba que queda al lado pedido del plano y que el
            volumen cambia como debe (si no, se borra y se prueba el otro sentido). Nunca guarda. Para
            deshacer, borrar_operaciones_ia con su id."""
            return _crear_croquis(codigo, "extrusion")

        @mcp.tool(annotations=DRAW_DESTRUCTIVE)
        @guard
        @tareas.larga
        def crear_revolucion(
            codigo: Annotated[str, Field(description="Código que devolvió proponer_revolucion")],
        ) -> dict[str, Any]:
            """Crea en Creo la revolución o el corte por revolución de una propuesta ya confirmada por el
            usuario. Solo con la confirmación expresa sobre la tabla de proponer_revolucion. Vuelve a leer
            la pieza: si el plano o las referencias han cambiado, no crea nada. La operación se llama
            IA_REV_* o IA_CORTE_REV_*; se comprueba que queda en su tramo del eje y que el volumen cambia
            como debe. Nunca guarda. Para deshacer, borrar_operaciones_ia con su id."""
            return _crear_croquis(codigo, "revolucion")

        def _crear_croquis(codigo: str, clase: str) -> dict[str, Any]:
            herramienta = "crear_extrusion" if clase == "extrusion" else "crear_revolucion"
            propuesta = "proponer_extrusion" if clase == "extrusion" else "proponer_revolucion"
            if estado_ops["desconocido"]:
                raise ValueError("Hay una escritura anterior con resultado desconocido. Comprueba Creo "
                                 "(operaciones_creo) y vuelve a proponer antes de crear nada.")
            clave = (codigo or "").strip().upper()
            p = propuestas_ext.get(clave)
            if p is None or p.get("clase", "extrusion") != clase:
                raise ValueError(f"No hay ninguna propuesta de {'extrusión' if clase == 'extrusion' else 'revolución'} "
                                 f"con ese código (vale una vez, 30 min). Vuelve a llamar a {propuesta}.")
            propuestas_ext.pop(clave, None)
            if time.time() - p["creada"] > PROPUESTA_VALIDA_S:
                raise ValueError(f"La propuesta ha caducado (30 min). Vuelve a llamar a {propuesta}.")
            caras = nat.ejecutar("caras", p["modelo"])
            if huella(caras, p["ids_caras"], p["ids_planos"]) != p["huella"]:
                raise ValueError("La pieza ha cambiado desde la propuesta (plano del croquis o referencias): no se ha "
                                 f"creado nada. Vuelve a llamar a {propuesta}.")
            try:
                r = nat.ejecutar("crear", plan=p["plan"])
            except NativoSinRespuesta as e:
                estado_ops["desconocido"] = {**p["resumen"], "error": str(e)[:200]}
                gw.audit.write(event="resultado_desconocido", tool=herramienta, modelo=p["modelo"])
                return {"estado": "resultado_desconocido", clase: p["resumen"],
                        "que_hacer": "Creo no respondió a tiempo: la operación puede haberse creado o no. Comprueba "
                                     "con operaciones_creo si existe " + str(p["resumen"].get("nombre")) + "."}
            op = r.get("operacion") or {}
            gw.audit.write(event="operacion_creada", modelo=p["modelo"], id=op.get("id"), nombre=op.get("nombre"),
                           intentos=op.get("intentos"))
            return {"estado": "ok", "modelo": p["modelo"],
                    clase: {**p["resumen"], "id": op.get("id"), "nombre": op.get("nombre") or p["resumen"]["nombre"],
                            "intentos": op.get("intentos"), "cambio_volumen_mm3": op.get("cambio_volumen"),
                            "volumen_pieza_mm3": op.get("volumen_despues"),
                            "extension_en_eje": [op.get("extension_min"), op.get("extension_max")],
                            "cotas_croquis": op.get("cotas_croquis"),
                            "cotas_en_creo": [f"{c.get('tipo')} {c.get('valor'):.4f}" + (" (boceto)" if c.get("del_boceto") else "")
                                              for c in op.get("cotas", []) if isinstance(c.get("valor"), (int, float))],
                            "desviacion_croquis_mm": op.get("desviacion_croquis_mm"),
                            **({"avisos_croquis": op["avisos_croquis"]} if op.get("avisos_croquis") else {})},
                    "aviso": "Pieza modificada en la sesión de Creo, SIN GUARDAR. Revísala y guarda tú si estás conforme. "
                             "Para deshacer: borrar_operaciones_ia con su id."
                             + (" Las cotas del boceto chocaron con restricciones de Creo y el croquis quedó con el "
                                "acotado automático: revisa sus cotas." if op.get("cotas_croquis") == "automaticas_por_conflicto" else "")}

        @mcp.tool(annotations=DRAW_DESTRUCTIVE)
        @guard
        def borrar_operaciones_ia(
            ids: Annotated[list[int], Field(min_length=1, max_length=64,
                                            description="Ids de operaciones IA_* (los da crear_taladros o taladros_creo)")],
            modelo: ModelArg = None,
        ) -> dict[str, Any]:
            """Borra de una pieza en sesión operaciones creadas por el asistente (nombre IA_*), por
            ejemplo para deshacer crear_taladros. Pide confirmación al usuario antes. No borra nada
            que no se llame IA_* ni operaciones con hijas que no estén en la lista. No guarda."""
            m = _pieza(modelo)
            try:
                r = nat.ejecutar("borrar", m, ids=list(ids))
            except NativoSinRespuesta as e:
                estado_ops["desconocido"] = {"borrar": list(ids), "modelo": m, "error": str(e)[:200]}
                return {"estado": "resultado_desconocido", "ids": list(ids),
                        "que_hacer": "Creo no respondió a tiempo: puede haber borrado o no. Comprueba con taladros_creo."}
            gw.audit.write(event="operaciones_borradas", modelo=m, ids=r.get("borradas"))
            mm = r.get("modelo") or {}
            return {"estado": "ok", "modelo": f"{mm.get('nombre', m)}.{mm.get('tipo', 'PRT')}" if mm else m,
                    "borradas": r.get("borradas"),
                    "aviso": "Pieza modificada en la sesión de Creo, sin guardar."}

    def _revisar(fichero: str) -> tuple[dict[str, Any], dict[str, Any]]:
        ruta = _fichero_bom(fichero)
        try:
            libro = bom_excel.leer_libro(ruta)
        except PermissionError as e:
            raise ValueError("No se puede leer el Excel: ciérralo en Excel y vuelve a intentarlo.") from e
        en_sesion = {x.upper(): x for x in gw.models_in_session()}
        actuales = _params_varios([en_sesion[k] for k in libro["filas"] if k in en_sesion])
        res = bom_excel.comparar(libro, actuales)
        estados = {}
        for c in res["cambios"]:
            k = c["modelo"].upper()
            if k not in estados:
                p = actuales.get(k, {})
                estados[k] = {
                    "version_windchill": p.get("PTC_WM_VERSION", {}).get("value"),
                    "estado": p.get("PTC_WM_LIFECYCLE_STATE", {}).get("value"),
                }
        res["windchill"] = estados
        return libro, res

    @mcp.tool(annotations=READ)
    @guard
    def revisar_parametros_excel(
        fichero: Annotated[str, Field(description="Nombre del .xlsx exportado con exportar_bom_excel (sin ruta)")],
    ) -> dict[str, Any]:
        """Compara el Excel del BOM editado con los parámetros actuales en Creo. No escribe nada.

        Devuelve los cambios (antes → después), los conflictos (el valor cambió en Creo
        después de exportar), lo ignorado y los errores, más un `codigo` que exige
        aplicar_parametros_excel. Enseña la tabla de cambios al usuario y pide su
        confirmación expresa antes de aplicar.
        """
        libro, res = _revisar(fichero)
        if gw.resultado_desconocido:
            res["resultado_desconocido_anterior"] = gw.resultado_desconocido
            res["nota"] = ("La escritura anterior quedó con resultado desconocido; esta revisión ya refleja lo que "
                           "hay en Creo ahora. Se puede volver a aplicar con el código nuevo.")
            gw.resultado_desconocido = []
        res["conjunto"] = libro["meta"].get("conjunto")
        res["exportado"] = libro["meta"].get("fecha")
        res["aplicar_activo"] = settings.allow_parameters
        if res["cambios"]:
            res["aviso"] = (
                "Aplicar cambia los modelos en sesión y no guarda. En Windchill, los modelos deben "
                "estar en check-out (o Creo pedirá hacerlo y la petición puede quedar esperando). "
                "Después revisa y guarda en Creo."
            )
            try:
                res["dm_checkout_on_the_fly"] = gw.call("creo_get_config", name="dm_checkout_on_the_fly")
            except RuntimeError:
                pass
        return res

    if settings.allow_parameters:

        @mcp.tool(annotations=DRAW_DESTRUCTIVE)
        @guard
        def aplicar_parametros_excel(
            fichero: Annotated[str, Field(description="Nombre del .xlsx (sin ruta)")],
            codigo: Annotated[str, Field(description="Código que devolvió revisar_parametros_excel")],
        ) -> dict[str, Any]:
            """Escribe en Creo los cambios de parámetros del Excel del BOM ya revisados.

            Solo con la confirmación expresa del usuario sobre la tabla de
            revisar_parametros_excel. Repite la comparación y, si el resultado no coincide
            con `codigo` (el Excel o Creo cambiaron), no escribe nada. Solo parámetros del
            cajetín que ya existen; nunca crea parámetros, nunca guarda, no toca conflictos.
            """
            if gw.resultado_desconocido:
                raise ValueError(
                    "Hay escrituras anteriores con resultado desconocido (Creo dejó de responder a mitad). "
                    "Comprueba Creo y vuelve a ejecutar revisar_parametros_excel antes de aplicar más cambios."
                )
            _, res = _revisar(fichero)
            if not res["cambios"]:
                raise ValueError("No hay cambios que aplicar.")
            if res["codigo"] != (codigo or "").strip():
                raise ValueError(
                    "Los cambios ya no coinciden con la revisión (el Excel o Creo han cambiado). "
                    "Vuelve a revisar con revisar_parametros_excel."
                )
            hechos, fallos = [], []
            pendientes = list(res["cambios"])
            while pendientes:
                c = pendientes.pop(0)
                valor = c["despues"]
                if valor == "" and c.get("tipo") == "STRING":
                    valor = " "  # vacío en el cajetín IAC
                try:
                    gw.call("parameter_set", name=c["parametro"], value=valor, file_=c["modelo"])
                    hechos.append(c)
                except CreoUnavailableError as e:
                    # No se sabe si Creo llegó a escribir este valor: no se sigue escribiendo.
                    gw.resultado_desconocido = [{**c, "error": e.mensaje}]
                    gw.audit.write(event="resultado_desconocido", tool="aplicar_parametros_excel",
                                   modelo=c["modelo"], parametro=c["parametro"])
                    return {
                        "estado": "resultado_desconocido",
                        "aplicados_confirmados": hechos,
                        "resultado_desconocido": gw.resultado_desconocido,
                        "no_intentados": pendientes,
                        "fallidos": fallos,
                        "que_hacer": f"{e.que_hacer} Después vuelve a ejecutar revisar_parametros_excel: dirá qué "
                                     "cambios llegaron a Creo y cuáles faltan. Hasta entonces no se escribe nada más.",
                    }
                except (RuntimeError, ScopeError) as e:
                    fallos.append({**c, "error": str(e)})
            # Verificación: se vuelve a leer cada parámetro escrito y se compara con lo pedido.
            verificados, distintos = [], []
            leidos = _params_varios(list(dict.fromkeys(c["modelo"] for c in hechos))) if hechos else {}
            for c in hechos:
                actual = bom_excel.celda(leidos.get(c["modelo"].upper(), {}).get(c["parametro"], {}).get("value"))
                (verificados if actual == c["despues"] else distintos).append({**c, "leido": actual})
            out = {
                "estado": "ok" if not (fallos or distintos) else "con_incidencias",
                "aplicados": verificados,
                "fallidos": fallos + [{**c, "error": f"Tras escribir, Creo devuelve «{c['leido']}»."} for c in distintos],
                "conflictos_no_aplicados": res["conflictos"],
                "aviso": "Modelos modificados en la sesión de Creo, sin guardar. Revisa y guarda en Creo "
                         "(y registra en Windchill).",
            }
            return out

    if settings.allow_drawings:
        _register_drawing_tools(mcp, gw, guard)

    return mcp


def _register_drawing_tools(mcp: FastMCP, gw: CreoGateway, guard) -> None:
    """Herramientas que escriben en Creo. Solo se registran con CREO_MCP_ALLOW_DRAWINGS=1."""

    def own(plano: str) -> tuple[str, str]:
        """Nombre exacto de un plano creado por el asistente y su modelo."""
        key = (plano or "").strip().upper()
        if key and not key.endswith(".DRW"):
            key += ".DRW"
        if key not in gw.created_drawings:
            creados = ", ".join(sorted(gw.created_drawings)) or "ninguno"
            raise ValueError(
                f"{plano!r} no es un plano creado por el asistente en esta sesión (creados: {creados}). "
                "Los planos existentes no se modifican."
            )
        return key.lower(), gw.created_drawings[key]

    def pt(x: float, y: float) -> dict[str, float]:
        return {"x": float(x), "y": float(y), "z": 0.0}

    def _set_format(d: str, hoja: int, formato: str) -> str:
        """Pone un formato en una hoja. Si Creo no lo encuentra por nombre, lo busca en
        la carpeta de formatos de su configuración (pro_format_dir)."""
        f = FORMATOS_IAC.get(formato.strip().upper(), formato.strip())
        f = re.sub(r"\.frm(\.\d+)?$", "", f, flags=re.IGNORECASE)
        if not _CREO_NAME.match(f):
            raise ValueError("Formato no válido: A0-A3 o el nombre de un .frm, sin rutas.")
        try:
            gw.call("drawing_set_sheet_format", sheet=hoja, file_format=f, drawing=d)
            return f
        except RuntimeError as e:
            if isinstance(e, CreoUnavailableError):
                raise
            dirs = gw.call("creo_get_config", name="pro_format_dir") or []
            if not dirs:
                raise
            gw.call("drawing_set_sheet_format", sheet=hoja, file_format=f, drawing=d,
                    dirname=dirs[0] if isinstance(dirs, list) else dirs)
            return f

    @mcp.tool(annotations=DRAW)
    @guard
    def crear_plano(
        modelo: Annotated[str, Field(description="Pieza o conjunto abierto en sesión")],
        plantilla: Annotated[str, Field(description="Plantilla de plano de Creo (ver listar_plantillas_plano)")],
        nombre: Annotated[
            str | None, Field(description="Nombre del plano sin extensión. Vacío = mismo nombre que el modelo")
        ] = None,
        escala: Annotated[float | None, Field(description="Escala de la hoja, p. ej. 0.5 para 1:2")] = None,
        formato: Annotated[
            str | None,
            Field(description="Formato IAC con marco y cajetín: A0, A1, A2 o A3 (form_iac_a-N). "
                              "Vacío = el que traiga la plantilla"),
        ] = None,
    ) -> dict[str, Any]:
        """Crea un plano NUEVO de Creo a partir de una plantilla y lo abre en una ventana.

        Plantilla del departamento: `plantilla_plano` (pone las vistas). El marco y el
        cajetín los da el formato IAC (`formato`: A0-A3); elige el tamaño que deje
        las vistas legibles a una escala normalizada.

        No guarda nada: el plano queda en sesión para que el usuario lo revise, lo
        guarde en su espacio de trabajo y lo registre en Windchill.
        """
        m = gw.resolve_model(modelo)
        if not m.upper().endswith((".PRT", ".ASM")):
            raise ValueError("Solo se crean planos de piezas (.PRT) o conjuntos (.ASM).")
        tpl = plantilla.strip()
        tpl = re.sub(r"\.drw(\.\d+)?$", "", tpl, flags=re.IGNORECASE)
        if not _CREO_NAME.match(tpl):
            raise ValueError("Nombre de plantilla no válido: solo el nombre, sin rutas ni extensión.")
        base = (nombre or m.rsplit(".", 1)[0]).strip()
        base = re.sub(r"\.drw$", "", base, flags=re.IGNORECASE)
        if not _CREO_NAME.match(base):
            raise ValueError("Nombre de plano no válido: letras, números, '_' y '-', máx. 31 caracteres.")
        drw = f"{base}.drw"
        key = drw.upper()
        if key in {x.upper() for x in gw.models_in_session()}:
            if gw.pending_drawings.get(key, "").upper() == m.upper():
                # Se pidió antes y Creo no contestó (diálogo); ya existe: se adopta.
                gw.register_drawing(drw, m)
                del gw.pending_drawings[key]
                return {"plano": drw, "modelo": m, "plantilla": tpl, "guardado": False,
                        "aviso": "Plano creado en el intento anterior; se toma ese.",
                        "siguiente": "Revisa el plano en Creo; guárdalo tú cuando esté bien."}
            raise ValueError(f"Ya hay un {drw} en la sesión de Creo; elige otro nombre.")
        if escala is not None and not (0 < escala <= 100):
            raise ValueError("Escala fuera de rango.")
        gw.pending_drawings[key] = m
        try:
            res = gw.call("drawing_create", template=tpl, model=m, drawing=base, scale=escala,
                          display=True, activate=True, new_window=True)
        except CreoUnavailableError as e:
            if e.codigo == "creo_ocupado":
                raise ValueError(
                    "Creo no contestó: probablemente hay un diálogo abierto. Ciérralo en Creo y "
                    f"vuelve a llamar a crear_plano con el mismo modelo y nombre ({base}): si el "
                    "plano llegó a crearse, se tomará ese."
                ) from e
            raise
        gw.pending_drawings.pop(key, None)
        name = res if isinstance(res, str) and res else drw
        if not name.upper().endswith(".DRW"):
            name += ".drw"
        gw.register_drawing(name, m)
        out: dict[str, Any] = {"plano": name, "modelo": m, "plantilla": tpl, "guardado": False,
                               "siguiente": "Revisa el plano en Creo; guárdalo tú cuando esté bien."}
        if formato:
            try:
                out["formato"] = _set_format(name.lower(), 1, formato)
            except (ValueError, RuntimeError) as e:
                if isinstance(e, CreoUnavailableError):
                    raise
                out["formato"] = f"no se pudo poner: {e}"
        return out

    @mcp.tool(annotations=DRAW)
    @guard
    def anadir_vista(
        plano: str,
        x: Point,
        y: Point,
        vista_modelo: Annotated[
            str | None, Field(description="Vista general: orientación guardada del modelo (ver vistas_modelo)")
        ] = None,
        vista_padre: Annotated[
            str | None, Field(description="Vista proyectada: nombre de la vista del plano de la que se proyecta")
        ] = None,
        nombre: Annotated[str | None, Field(description="Nombre de la nueva vista")] = None,
        escala: Annotated[float | None, Field(description="Solo vista general; vacío = escala de la hoja")] = None,
        hoja: int | None = None,
    ) -> dict[str, Any]:
        """Añade una vista general (con `vista_modelo`) o proyectada (con `vista_padre`) a un
        plano creado por el asistente. La proyectada se coloca alineada con su padre."""
        d, m = own(plano)
        if bool(vista_modelo) == bool(vista_padre):
            raise ValueError("Indica vista_modelo (vista general) o vista_padre (proyectada), no ambas.")
        if vista_modelo:
            gw.call("drawing_create_gen_view", model_view=vista_modelo, point=pt(x, y), drawing=d,
                    view=nombre, sheet=hoja, model=m, scale=escala)
        else:
            gw.call("drawing_create_proj_view", parent_view=vista_padre, point=pt(x, y), drawing=d,
                    view=nombre, sheet=hoja)
        return {"plano": d, "vistas": gw.call("drawing_list_views", drawing=d)}

    @mcp.tool(annotations=DRAW)
    @guard
    def ajustar_vista(
        plano: str,
        vista: str,
        x: float | None = None,
        y: float | None = None,
        escala: float | None = None,
    ) -> dict[str, Any]:
        """Mueve o cambia la escala de una vista de un plano creado por el asistente."""
        d, _ = own(plano)
        if (x is None) != (y is None):
            raise ValueError("Para mover la vista indica x e y.")
        if x is not None:
            gw.call("drawing_set_view_loc", view=vista, point=pt(x, y), drawing=d)
        if escala is not None:
            gw.call("drawing_scale_view", scale=escala, view=vista, drawing=d)
        return {"plano": d, "vista": gw.call("drawing_list_view_details", view=vista, drawing=d)}

    @mcp.tool(annotations=DRAW_DESTRUCTIVE)
    @guard
    def borrar_vista(plano: str, vista: str, con_hijas: bool = False) -> dict[str, Any]:
        """Borra una vista de un plano creado por el asistente (para corregir un intento)."""
        d, _ = own(plano)
        gw.call("drawing_delete_view", view=vista, drawing=d, del_children=con_hijas)
        return {"plano": d, "vistas": gw.call("drawing_list_views", drawing=d)}

    @mcp.tool(annotations=DRAW)
    @guard
    def configurar_hoja(
        plano: str,
        hoja: int = 1,
        escala: float | None = None,
        formato: Annotated[
            str | None, Field(description="A0, A1, A2, A3 (formatos IAC) o el nombre de un .frm")
        ] = None,
    ) -> dict[str, Any]:
        """Cambia la escala o el formato de una hoja de un plano creado por el asistente."""
        d, m = own(plano)
        if formato:
            _set_format(d, hoja, formato)
        if escala is not None:
            gw.call("drawing_scale_sheet", sheet=hoja, scale=escala, drawing=d, model=m)
        return {"plano": d, "hoja": hoja,
                "escala": gw.call("drawing_get_sheet_scale", sheet=hoja, drawing=d),
                "formato": gw.call("drawing_get_sheet_format", sheet=hoja, drawing=d)}

    @mcp.tool(annotations=DRAW)
    @guard
    def mostrar_cotas(
        plano: str,
        cotas: Annotated[list[str], Field(description="Nombres de cotas del modelo (ver leer_cotas), p. ej. ['d12','d15']")],
    ) -> dict[str, Any]:
        """Muestra en el plano cotas del modelo. No crea cotas nuevas ni las coloca:
        Creo decide la posición y hay que ordenarlas en Creo. EXPERIMENTAL."""
        d, m = own(plano)
        if not cotas or len(cotas) > 200:
            raise ValueError("Indica entre 1 y 200 cotas.")
        hechas, errores = [], {}
        for c in cotas:
            if not re.match(r"^[A-Za-z0-9_:]{1,40}$", c):
                errores[c] = "nombre no válido"
                continue
            try:
                gw.call("dimension_show", name=c, file_=m)
                hechas.append(c)
            except RuntimeError as e:
                if isinstance(e, CreoUnavailableError):
                    raise
                errores[c] = str(e)
        return {"plano": d, "modelo": m, "mostradas": hechas, "errores": errores}

    @mcp.tool(annotations=DRAW)
    @guard
    def poner_nota(
        plano: str,
        nombre: Annotated[str, Field(description="Nombre de la nota, p. ej. 'NOTA_IA_1'")],
        texto: Annotated[str, Field(description="Texto; usa saltos de línea. Puede incluir &PARAMETRO")],
        x: Point,
        y: Point,
    ) -> dict[str, Any]:
        """Crea o cambia una nota de un plano creado por el asistente."""
        d, _ = own(plano)
        if not re.match(r"^[A-Za-z0-9_]{1,31}$", nombre):
            raise ValueError("Nombre de nota no válido.")
        if len(texto) > 2000:
            raise ValueError("Nota demasiado larga.")
        gw.call("note_set", name=nombre, file_=d, location=pt(x, y), value=texto)
        return {"plano": d, "nota": nombre}

    @mcp.tool(annotations=DRAW)
    @guard
    def poner_simbolo(
        plano: str,
        simbolo: Annotated[str, Field(description="Símbolo del plano o de la biblioteca, p. ej. 'RUGOSIDAD'")],
        x: Point,
        y: Point,
        valores: Annotated[
            dict[str, str] | None, Field(description="Valores de las variables del símbolo")
        ] = None,
        hoja: int | None = None,
    ) -> dict[str, Any]:
        """Coloca un símbolo (rugosidad, etc.) en un plano creado por el asistente."""
        d, _ = own(plano)
        if not _CREO_NAME.match(simbolo):
            raise ValueError("Nombre de símbolo no válido: sin rutas ni extensión.")
        gw.call("drawing_create_symbol", symbol_file=simbolo, point=pt(x, y), drawing=d,
                replace_values=valores, sheet=hoja)
        return {"plano": d, "simbolo": simbolo}

    @mcp.tool(annotations=DRAW_DESTRUCTIVE)
    @guard
    def rellenar_cajetin(
        plano: str,
        parametros: Annotated[
            dict[str, str],
            Field(description="Parámetros del cajetín y su valor, p. ej. {'CODIGO_PLANO': 'XXX-DWG-...'}"),
        ],
    ) -> dict[str, Any]:
        """Rellena parámetros del cajetín IAC en el MODELO del plano (solo los que ya existen
        y están en la lista del cajetín). Cambia la pieza: en Windchill necesita check-out.
        Pide los valores al usuario; no los inventes. Devuelve el valor anterior y el nuevo."""
        d, m = own(plano)
        if not parametros:
            raise ValueError("No hay parámetros que rellenar.")
        fuera = [k for k in parametros if k.upper() not in CAJETIN_PARAMS]
        if fuera:
            raise ValueError(f"Fuera del cajetín: {', '.join(fuera)}. Permitidos: {', '.join(sorted(CAJETIN_PARAMS))}")
        antes = {p["name"].upper(): p.get("value")
                 for p in gw.call("parameter_list", file_=m, name=list(parametros)) or []}
        faltan = [k for k in parametros if k.upper() not in antes]
        if faltan:
            raise ValueError(f"No existen en {m}: {', '.join(faltan)}. El asistente no crea parámetros.")
        cambios = {}
        for k, v in parametros.items():
            if not isinstance(v, str) or len(v) > 200:
                raise ValueError(f"Valor no válido para {k}.")
            gw.call("parameter_set", name=k, value=v, file_=m)
            cambios[k] = {"antes": antes[k.upper()], "ahora": v}
        gw.call("drawing_regenerate", drawing=d)
        return {"plano": d, "modelo": m, "cambios": cambios,
                "aviso": "Se ha modificado el modelo en sesión; no se ha guardado."}

    @mcp.tool(annotations=DRAW)
    @guard
    def regenerar_plano(plano: str) -> dict[str, Any]:
        """Regenera un plano creado por el asistente."""
        d, _ = own(plano)
        gw.call("drawing_regenerate", drawing=d)
        return {"plano": d, "regenerado": True}


def main() -> None:
    import sys

    from .config import ConfigError

    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"[creo-mcp-iac] Configuración no válida: {e}", file=sys.stderr)
        raise SystemExit(2)
    build_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
