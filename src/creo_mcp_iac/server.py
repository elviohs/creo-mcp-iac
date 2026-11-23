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
from .config import Settings
from .formato import add_dim_text, blank, check_mass_param, clean_params, massprops_si, note_has_person, note_text
from .gateway import CAJETIN_PARAMS, CONFIG_OPTIONS, CreoGateway, CreoUnavailableError, NotAllowedError, ScopeError
from .nativo import (Nativo, NativoError, NativoSinRespuesta, resumir_interferencias, resumir_operacion,
                     resumir_taladros)
from .operaciones import (PlanError, huella, leer_hol, nombre_operacion, planificar_patron, planificar_taladro,
                          resumir_caras, resumir_seleccion)
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
- Las herramientas largas (exportar_step, exportar_bom_excel, presupuesto_masas,
  revisar_estandares, interferencias_creo, taladros_creo, crear_taladros) responden «en_curso» con un
  `tarea_id` si tardan más de ~55 s: recoge el resultado con `resultado_tarea`; no las repitas.
- `exportar_bom_excel` escribe un Excel nuevo en la carpeta de exportación (no toca
  Creo). Si `aplicar_parametros_excel` está activa (CREO_MCP_ALLOW_PARAMETERS=1), solo
  la uses tras enseñar al usuario la tabla de `revisar_parametros_excel` y recibir su
  confirmación expresa; cambia parámetros del cajetín en sesión y no guarda.
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
        taladros_creo, crear_taladros). Si aún no ha terminado, espera hasta esperar_s y vuelve a dar el estado:
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
