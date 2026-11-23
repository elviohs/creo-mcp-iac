"""Pasarela de solo lectura sobre creopyson.

Todo acceso a Creo pasa por `CreoGateway.call()`, que:
  1. Solo permite las funciones de creopyson de la lista ALLOWED (lista blanca).
     Cualquier otra (parameter_set, file_save, kill_creo, windchill_*, ...)
     se rechaza antes de llegar a CREOSON.
  2. Fuerza argumentos seguros (p. ej. select=False en cotas, tipo STEP y
     carpeta fija en la exportación).
  3. Envía las peticiones HTTP solo al host de CREOSON configurado, sin proxy
     del sistema, sin redirecciones y con tiempo máximo.
  4. Registra cada llamada en un fichero de auditoría local (sin valores).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import creopyson
import creopyson.connection as _creo_conn
import requests

from .bom_excel import EDITABLES as BOM_PARAMS
from .config import Settings

# Funciones de creopyson.Client permitidas. Todas son de lectura salvo
# interface_export_file, que solo escribe un fichero nuevo en la carpeta de
# exportación y no modifica el modelo.
ALLOWED: frozenset[str] = frozenset(
    {
        # conexión
        "connect",
        "disconnect",
        "is_creo_running",
        # sesión y ficheros (lectura)
        "creo_pwd",
        "file_get_active",
        "file_list",
        "file_get_fileinfo",
        "file_get_length_units",
        "file_get_mass_units",
        "file_get_cur_material",
        "file_list_instances",
        "file_massprops",
        # contenido del modelo
        "parameter_list",
        "dimension_list",
        "dimension_list_detail",
        "feature_list",
        "bom_get_paths",
        # planos (lectura)
        "drawing_list_models",
        "drawing_get_num_sheets",
        "drawing_get_sheet_size",
        "drawing_get_sheet_scale",
        "drawing_get_sheet_format",
        "drawing_list_view_details",
        "drawing_list_symbols",
        "drawing_list_views",
        "view_list",
        # configuración de Creo (solo opciones de CONFIG_OPTIONS)
        "creo_get_config",
        # notas (lectura; la selección se fuerza a False)
        "note_list",
        # exportación controlada (fichero nuevo en la carpeta autorizada)
        "interface_export_file",
        "interface_export_pdf",
    }
)

# Opciones de configuración de Creo que se pueden consultar.
CONFIG_OPTIONS: frozenset[str] = frozenset(
    {
        "template_drawing",
        "pro_format_dir",
        "pro_symbol_dir",
        "drawing_setup_file",
        "template_solidpart",
        "template_designasm",
        "intf3d_out_default_option",
        "export_profiles_step",
        "dm_checkout_on_the_fly",
    }
)

# Funciones que ESCRIBEN en la sesión de Creo. Solo con CREO_MCP_ALLOW_DRAWINGS=1
# y solo sobre planos creados por este servidor (o su modelo, para el cajetín).
# Ninguna guarda: file_save no está en ninguna lista.
WRITE_DRAWING: frozenset[str] = frozenset(
    {
        "drawing_create",
        "drawing_create_gen_view",
        "drawing_create_proj_view",
        "drawing_set_view_loc",
        "drawing_scale_view",
        "drawing_scale_sheet",
        "drawing_set_sheet_format",
        "drawing_add_sheet",
        "drawing_delete_view",
        "drawing_regenerate",
        "drawing_create_symbol",
        "note_set",
        "dimension_show",
        "parameter_set",
    }
)

# Parámetros del cajetín IAC que se pueden rellenar (KB-DEP-0003, apartado 5).
# Solo se modifican si ya existen en el modelo (no_create).
CAJETIN_PARAMS: frozenset[str] = frozenset(
    {
        "CODIGO_PLANO", "PROYECTO", "SUBSISTEMA", "PIEZA", "PROTECCION_SUPERFICIAL",
        "RUGOSIDAD", "TOLERANCIA", "CANTIDAD_FABRICADA", "VALIDO_PARA",
        "MASA", "MASA_UNIT", "DISEÑADO_POR", "FECHA_DISEÑADO", "DIBUJADO_POR",
        "FECHA_DIBUJADO", "REVISADO_POR", "AUTORIZADO_POR",
    }
)

# Argumentos impuestos por la pasarela, sea cual sea lo que pida la herramienta.
FORCED_KWARGS: dict[str, dict[str, Any]] = {
    "dimension_list": {"select": False},
    "dimension_list_detail": {"select": False},
    "note_list": {"select": False},
    "interface_export_pdf": {"use_drawing_settings": True},
    "parameter_set": {"no_create": True},
    "interface_export_file": {"file_type": "STEP", "geom_flags": "solids"},
}


# Funciones que en conjuntos grandes tardan más que el tiempo normal (medido el 2026-11-21 con
# conjunto real de 208 modelos: bom_get_paths de 29 a más de 60 s; parameter_list con comodín ~50 s).
SLOW_FUNCS: frozenset[str] = frozenset({"bom_get_paths", "file_massprops"})
SLOW_TIMEOUT_S = 300.0


class NotAllowedError(PermissionError):
    """Se intentó una función de creopyson fuera de la lista blanca."""


class ScopeError(PermissionError):
    """Escritura permitida, pero fuera del plano o modelo autorizado."""


class CreoUnavailableError(RuntimeError):
    """CREOSON no responde o Creo no está en marcha."""

    def __init__(self, message: str, codigo: str = "desconocido", que_hacer: str = ""):
        super().__init__(message if not que_hacer else f"{message} -> {que_hacer}")
        self.mensaje = message
        self.codigo = codigo
        self.que_hacer = que_hacer


# Diagnóstico de fallos de conexión vistos en el PC del departamento (2026-11-20).
_DIAGNOSTICS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("10061", "Connection refused", "denegó expresamente"),
        "creoson_parado",
        "CREOSON no está arrancado. Abre CreosonSetup y pulsa 'Start CREOSON' (puerto 9056).",
    ),
    (
        ("10054", "Connection aborted", "ConnectionReset", "forzado la interrupción"),
        "creoson_cortado",
        "CREOSON cortó la conexión: suele pasar cuando pierde el enlace con Creo. "
        "Cierra CREOSON, reinicia Creo y vuelve a arrancar CREOSON.",
    ),
    (
        ("Read timed out", "ReadTimeout"),
        "creo_ocupado",
        "Creo no contesta a tiempo. Probablemente tiene una ventana de diálogo abierta "
        "o está regenerando: ciérrala en Creo y vuelve a intentarlo.",
    ),
    (
        ("Max retries exceeded", "NewConnectionError"),
        "creoson_inaccesible",
        "No se llega a CREOSON. Comprueba que está arrancado y en el puerto configurado.",
    ),
)


def diagnose(error: BaseException | str) -> tuple[str, str]:
    """Devuelve (código, qué hacer) para un fallo de conexión con CREOSON."""
    text = str(error)
    for needles, code, hint in _DIAGNOSTICS:
        if any(n in text for n in needles):
            return code, hint
    return "desconocido", "Revisa que CREOSON y Creo estén arrancados y enlazados."


class _LocalOnlyRequests:
    """Sustituto del módulo `requests` dentro de creopyson.connection.

    creopyson llama a `requests.post(url, data=...)` sin timeout y respetando
    HTTP(S)_PROXY. Aquí se usa una sesión sin variables de entorno (sin proxy),
    sin redirecciones, con timeout y solo hacia la URL de CREOSON configurada.
    """

    exceptions = requests.exceptions

    def __init__(self, allowed_url: str, timeout_s: float):
        self._allowed = urlsplit(allowed_url)
        self._timeout = timeout_s
        self._session = requests.Session()
        self._session.trust_env = False  # ignora proxies y .netrc del sistema
        self.timeout_actual: float | None = None  # lo fija la pasarela para funciones lentas

    def post(self, url: str, data: Any = None, **_ignored: Any) -> requests.Response:
        u = urlsplit(url)
        if (u.scheme, u.hostname, u.port, u.path) != (
            self._allowed.scheme,
            self._allowed.hostname,
            self._allowed.port,
            self._allowed.path,
        ):
            raise requests.exceptions.InvalidURL(f"Destino no permitido: {url}")
        return self._session.post(
            url,
            data=data,
            timeout=self.timeout_actual or self._timeout,
            allow_redirects=False,
            headers={"Content-Type": "application/json"},
        )


class AuditLog:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def write(self, **record: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # la auditoría nunca debe tumbar el servidor


class CreoGateway:
    def __init__(self, settings: Settings, client: Any | None = None):
        self.settings = settings
        self.url = f"http://{settings.creoson_host}:{settings.creoson_port}/creoson"
        # Endurece el transporte de creopyson para todo el proceso.
        self._transport = _LocalOnlyRequests(self.url, settings.timeout_s)
        _creo_conn.requests = self._transport
        self._client = client or creopyson.Client(
            ip_adress=settings.creoson_host, port=settings.creoson_port
        )
        self._lock = threading.Lock()  # CREOSON atiende una petición cada vez
        self.audit = AuditLog(settings.audit_log)
        # Planos creados por este servidor en esta sesión -> modelo que dibujan.
        self.created_drawings: dict[str, str] = {}
        # Planos pedidos a Creo cuya respuesta no llegó (p. ej. por un diálogo).
        self.pending_drawings: dict[str, str] = {}
        # Escrituras cuyo resultado no se pudo confirmar (Creo dejó de responder a mitad).
        # Mientras haya alguna, no se escribe más hasta volver a leer el estado de Creo.
        self.resultado_desconocido: list[dict[str, Any]] = []

    # -- núcleo -----------------------------------------------------------
    def call(self, func: str, /, **kwargs: Any) -> Any:
        writes = func in WRITE_DRAWING
        params_ok = func == "parameter_set" and self.settings.allow_parameters
        if func not in ALLOWED and not ((writes and self.settings.allow_drawings) or params_ok):
            self.audit.write(event="blocked", func=func)
            if func == "parameter_set":
                raise NotAllowedError(
                    "parameter_set escribe en Creo y está desactivada "
                    "(CREO_MCP_ALLOW_PARAMETERS=1 o CREO_MCP_ALLOW_DRAWINGS=1 para activarla)."
                )
            if writes:
                raise NotAllowedError(
                    f"{func} escribe en Creo y está desactivada (CREO_MCP_ALLOW_DRAWINGS=1 para activarla)."
                )
            raise NotAllowedError(f"Función de creopyson no permitida: {func}")
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        kwargs.update(FORCED_KWARGS.get(func, {}))
        if func == "creo_get_config" and kwargs.get("name") not in CONFIG_OPTIONS:
            self.audit.write(event="blocked", func=func, args={"name": kwargs.get("name")})
            raise NotAllowedError(f"Opción de configuración no permitida: {kwargs.get('name')}")
        if writes:
            try:
                self._check_write_scope(func, kwargs)
            except ScopeError:
                self.audit.write(event="blocked_scope", func=func,
                                 args={k: v for k, v in kwargs.items() if k in _LOGGABLE})
                raise
        method = getattr(self._client, func)
        lento = func in SLOW_FUNCS or (func == "parameter_list" and "*" in str(kwargs.get("file_", "")))
        t0 = time.perf_counter()
        with self._lock:
            self._ensure_session(func)
            if lento:
                self._transport.timeout_actual = max(self.settings.timeout_s, SLOW_TIMEOUT_S)
            try:
                try:
                    result = method(**kwargs)
                except RuntimeError as e:
                    # CREOSON reiniciado: la sesión guardada ya no existe. Se reconecta una vez
                    # (visto el 2026-11-22 al parar y arrancar CREOSON con el servidor abierto).
                    if "No session found" not in str(e) or func in ("connect", "disconnect"):
                        raise
                    self._client.sessionId = ""
                    self._ensure_session(func)
                    result = method(**kwargs)
                ok, err = True, None
                return result
            except (ConnectionError, requests.exceptions.RequestException) as e:
                self._client.sessionId = ""
                ok, err = False, f"CREOSON no responde: {e}"
                code, hint = diagnose(e)
                raise CreoUnavailableError(err, code, hint) from e
            except Exception as e:  # noqa: BLE001
                ok, err = False, str(e)
                raise
            finally:
                self._transport.timeout_actual = None
                self.audit.write(
                    event="call",
                    func=func,
                    args={k: v for k, v in kwargs.items() if k in _LOGGABLE},
                    ok=ok,
                    error=err,
                    ms=round((time.perf_counter() - t0) * 1000),
                )

    def _check_write_scope(self, func: str, kw: dict[str, Any]) -> None:
        """Toda escritura nombra su destino y el destino está autorizado.

        parameter_set vale por dos vías: la de planos (modelo de un plano creado por el
        asistente, parámetros del cajetín) o la de la lista de materiales
        (CREO_MCP_ALLOW_PARAMETERS=1: cualquier modelo en sesión, solo BOM_PARAMS).
        """
        if func == "parameter_set" and self.settings.allow_parameters:
            try:
                if self.settings.allow_drawings:
                    self._check_drawing_scope(func, kw)
                    return
            except ScopeError:
                pass
            self._check_bom_param_scope(kw)
            return
        self._check_drawing_scope(func, kw)

    def _check_bom_param_scope(self, kw: dict[str, Any]) -> None:
        target = str(kw.get("file_") or "")
        name = str(kw.get("name") or "")
        if not target or any(c in target for c in "*?/\\:"):
            raise ScopeError(f"parameter_set necesita un modelo concreto: {target!r}")
        if name.upper() not in BOM_PARAMS or any(c in name for c in "*?"):
            raise ScopeError(f"Parámetro fuera de la lista editable desde el BOM: {name}")
        if target.upper() not in {m.upper() for m in self.models_in_session()}:
            raise ScopeError(f"El modelo no está en la sesión de Creo: {target}")
        for extra in ("designate", "description", "type_"):
            kw.pop(extra, None)

    def _check_drawing_scope(self, func: str, kw: dict[str, Any]) -> None:
        if func == "drawing_create":
            if not kw.get("model") or not kw.get("drawing"):
                raise ScopeError("drawing_create necesita modelo y nombre de plano explícitos.")
            return
        if func in ("note_set",):
            target = kw.get("file_")
            if not target or target.upper() not in self.created_drawings:
                raise ScopeError(f"Solo se escriben notas en planos creados por el asistente: {target}")
            return
        if func in ("dimension_show", "parameter_set"):
            target = kw.get("file_")
            models = {m.upper() for m in self.created_drawings.values()}
            if not target or target.upper() not in models:
                raise ScopeError(
                    f"{func} solo actúa sobre el modelo de un plano creado por el asistente: {target}"
                )
            if func == "parameter_set":
                name = str(kw.get("name", ""))
                if name.upper() not in CAJETIN_PARAMS or any(c in name for c in "*?"):
                    raise ScopeError(f"Parámetro fuera de la lista del cajetín: {name}")
                for extra in ("designate", "description", "type_"):
                    kw.pop(extra, None)
            return
        target = kw.get("drawing")
        if not target or target.upper() not in self.created_drawings:
            raise ScopeError(
                f"Solo se modifican planos creados por el asistente en esta sesión: {target}"
            )

    def register_drawing(self, drawing: str, model: str) -> None:
        self.created_drawings[drawing.upper()] = model

    def _ensure_session(self, func: str) -> None:
        if func in ("connect", "disconnect", "is_creo_running"):
            return
        if not getattr(self._client, "sessionId", ""):
            try:
                self._client.connect()
            except (ConnectionError, requests.exceptions.RequestException) as e:
                code, hint = diagnose(e)
                raise CreoUnavailableError(
                    f"No se puede conectar con CREOSON en {self.url}", code, hint
                ) from e
            self._set_creo_version()

    def _set_creo_version(self) -> None:
        """Comunica a CREOSON la versión de Creo (solo afecta a la sesión de CREOSON).

        Sin esto CREOSON supone un Creo antiguo y, antes de regenerar, cambia la opción
        obsoleta `regen_failure_handling`; Creo 12 la bloquea con un diálogo modal que
        deja colgada la petición (visto al crear un plano el 2026-11-20).
        """
        ok, err = True, None
        try:
            self._client.creo_set_creo_version(self.settings.creo_version)
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)
        self.audit.write(event="call", func="creo_set_creo_version",
                         args={"version": self.settings.creo_version}, ok=ok, error=err, ms=0)

    # -- utilidades -------------------------------------------------------
    def models_in_session(self, pattern: str | None = None) -> list[str]:
        return list(self.call("file_list", file_=pattern) or [])

    def resolve_model(self, model: str | None) -> str:
        """Devuelve el nombre exacto del modelo, que debe estar ya en sesión.

        Nunca abre modelos desde disco: el responsable abre en Creo lo que
        autoriza a leer.
        """
        if not model:
            active = self.call("file_get_active")
            if not active or not active.get("file"):
                raise ValueError(
                    "No hay modelo activo en Creo. Abre el modelo en Creo o indica su nombre."
                )
            return active["file"]
        model = model.strip()
        if any(c in model for c in "*?/\\:") or len(model) > 80:
            raise ValueError(f"Nombre de modelo no válido: {model!r}")
        in_session = self.models_in_session()
        by_upper = {m.upper(): m for m in in_session}
        if model.upper() in by_upper:
            return by_upper[model.upper()]
        raise ValueError(
            f"El modelo {model!r} no está abierto en la sesión de Creo. "
            "Este servidor no abre modelos: pide al responsable que lo abra en Creo."
        )


# Argumentos que se registran en auditoría (nombres, no valores del modelo).
_LOGGABLE = {
    "file_",
    "name",
    "type_",
    "status",
    "dim_type",
    "drawing",
    "sheet",
    "filename",
    "top_level",
    "paths",
    "advanced",
    "get_expanded",
    "model",
    "template",
    "view",
    "parent_view",
    "model_view",
    "symbol_file",
}
