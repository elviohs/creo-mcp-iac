"""Configuración del servidor, leída solo de variables de entorno.

Variables:
    CREOSON_HOST                 Host de CREOSON (por defecto: localhost).
    CREOSON_PORT                 Puerto de CREOSON (por defecto: 9056).
    CREO_MCP_ALLOW_LAN           "1" para permitir un CREOSON en otra máquina de
                                 la red interna (solo IP privadas). Por defecto
                                 solo se admite loopback.
    CREO_MCP_EXPORT_DIR          Carpeta donde se permite exportar STEP. Si no
                                 se define, la exportación queda DESACTIVADA.
                                 "auto" = %LOCALAPPDATA%\\creo_mcp_iac\\export (se
                                 crea sola). Creo tiene que poder escribir en ella.
    CREO_MCP_AUDIT_LOG           Fichero JSONL de auditoría
                                 (por defecto: ~/.creo_mcp_iac/audit.jsonl).
    CREO_MCP_TIMEOUT             Tiempo máximo por petición a CREOSON, en s
                                 (por defecto: 60).
    CREO_MCP_MAX_ITEMS           Máximo de elementos devueltos por lista
                                 (por defecto: 500).
    CREO_MCP_ALLOW_DRAWINGS      "1" para activar las herramientas que crean planos
                                 en Creo (escriben en la sesión, nunca guardan).
                                 Por defecto desactivadas.
    CREO_MCP_ALLOW_PARAMETERS    "1" para activar importar_parametros_excel: escribe en
                                 los modelos en sesión los parámetros del cajetín
                                 editados en el Excel del BOM (nunca guarda).
                                 Por defecto desactivada.
    CREO_MCP_CREO_VERSION        Versión principal de Creo que se comunica a CREOSON
                                 (por defecto: 12). Sin ella, CREOSON usa un apaño
                                 para Creo antiguo que en Creo 12 abre un diálogo.
    CREO_MCP_REDACT_NAMES        "0" para devolver los nombres de personas de
                                 los parámetros (*_POR, *_BY). Por defecto se
                                 sustituyen por "[rellenado]".
    CREO_MCP_ALLOW_OPERACIONES   "1" para activar crear_taladros y borrar_operaciones_ia: crean
                                 o borran operaciones en piezas en sesión con Creo Toolkit
                                 (nunca guardan). Además, el servicio debe arrancarse con
                                 --escritura. Por defecto desactivada.
    CREO_MCP_TOOLKIT_EXE         Ruta de creo_iac_tk.exe (lecturas nativas con Creo
                                 Toolkit: interferencias y taladros). Por defecto
                                 native\\build\\creo_iac_tk.exe junto al código; "0" las
                                 desactiva.
    CREO_MCP_TOOLKIT_URL         Servicio local de lecturas nativas (native\\servicio_tk.py,
                                 arrancado fuera de Claude). Por defecto
                                 http://127.0.0.1:9057; "directo" lanza el programa desde
                                 el servidor (no funciona con Claude de la Tienda). Solo
                                 loopback.
    CREO_MCP_ESPERA              Segundos que espera una herramienta larga antes de
                                 responder «en_curso» (por defecto 55; el enlace con
                                 el PC corta a los 80 s).
    CREO_MCP_CREO_COMMON         Carpeta "Common Files" de Creo (por defecto
                                 C:\\Program Files\\PTC\\Creo 12.4.1.0\\Common Files). De ella
                                 salen pro_comm_msg.exe y las DLL que necesita
                                 creo_iac_tk.exe.
    CREO_MCP_HOL_DIR             Carpeta de las tablas de taladros normalizados de Creo
                                 (ISO.hol). Por defecto <Common Files>\\text\\hole.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Configuración no válida o insegura."""


@dataclass(frozen=True)
class Settings:
    creoson_host: str = "localhost"
    creoson_port: int = 9056
    allow_lan: bool = False
    export_dir: Path | None = None
    audit_log: Path = Path.home() / ".creo_mcp_iac" / "audit.jsonl"
    timeout_s: float = 60.0
    max_items: int = 500
    redact_names: bool = True
    allow_drawings: bool = False
    allow_parameters: bool = False
    allow_operaciones: bool = False
    creo_version: int = 12
    toolkit_exe: Path | None = None
    toolkit_url: str | None = "http://127.0.0.1:9057"
    espera_s: float = 55.0
    creo_common: Path = Path(r"C:\Program Files\PTC\Creo 12.4.1.0\Common Files")
    hol_dir: Path | None = None

    def tabla_hol(self, serie: str = "ISO") -> Path:
        """Ruta de la tabla de taladros normalizados de Creo (p. ej. ISO.hol)."""
        return (self.hol_dir or self.creo_common / "text" / "hole") / f"{serie}.hol"

    @property
    def export_enabled(self) -> bool:
        return self.export_dir is not None

    @property
    def creoson_is_local(self) -> bool:
        return is_loopback(self.creoson_host)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        export_raw = env.get("CREO_MCP_EXPORT_DIR", "").strip()
        if export_raw.lower() == "auto":
            base = env.get("LOCALAPPDATA", "").strip()
            export_dir = (Path(base) if base else Path.home() / ".creo_mcp_iac") / "creo_mcp_iac" / "export"
            try:
                export_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ConfigError(f"No se puede crear la carpeta de exportación {export_dir}: {e}")
            export_dir = export_dir.resolve()
        else:
            export_dir = Path(export_raw).expanduser().resolve() if export_raw else None
        audit_raw = env.get("CREO_MCP_AUDIT_LOG", "").strip()
        tk_raw = env.get("CREO_MCP_TOOLKIT_EXE", "").strip()
        if tk_raw == "0":
            toolkit_exe = None
        elif tk_raw:
            toolkit_exe = Path(tk_raw).expanduser()
        else:
            from .nativo import exe_por_defecto

            toolkit_exe = exe_por_defecto()
        common_raw = env.get("CREO_MCP_CREO_COMMON", "").strip()
        url_raw = env.get("CREO_MCP_TOOLKIT_URL", "").strip()
        toolkit_url = None if url_raw.lower() == "directo" else (url_raw or cls.toolkit_url)
        s = cls(
            creoson_host=env.get("CREOSON_HOST", "localhost").strip() or "localhost",
            creoson_port=int(env.get("CREOSON_PORT", "9056")),
            allow_lan=env.get("CREO_MCP_ALLOW_LAN", "0").strip() == "1",
            export_dir=export_dir,
            audit_log=Path(audit_raw).expanduser() if audit_raw else cls.audit_log,
            timeout_s=float(env.get("CREO_MCP_TIMEOUT", "60")),
            max_items=int(env.get("CREO_MCP_MAX_ITEMS", "500")),
            redact_names=env.get("CREO_MCP_REDACT_NAMES", "1").strip() != "0",
            allow_drawings=env.get("CREO_MCP_ALLOW_DRAWINGS", "0").strip() == "1",
            allow_parameters=env.get("CREO_MCP_ALLOW_PARAMETERS", "0").strip() == "1",
            allow_operaciones=env.get("CREO_MCP_ALLOW_OPERACIONES", "0").strip() == "1",
            creo_version=int(env.get("CREO_MCP_CREO_VERSION", "12")),
            toolkit_exe=toolkit_exe,
            toolkit_url=toolkit_url,
            espera_s=float(env.get("CREO_MCP_ESPERA", "55")),
            creo_common=Path(common_raw) if common_raw else cls.creo_common,
            hol_dir=Path(env["CREO_MCP_HOL_DIR"].strip()) if env.get("CREO_MCP_HOL_DIR", "").strip() else None,
        )
        s.validate()
        return s

    def validate(self) -> None:
        if not (0 < self.creoson_port < 65536):
            raise ConfigError(f"Puerto de CREOSON no válido: {self.creoson_port}")
        check_host_allowed(self.creoson_host, self.allow_lan)
        if self.export_dir is not None and not self.export_dir.is_dir():
            raise ConfigError(
                f"CREO_MCP_EXPORT_DIR no existe o no es una carpeta: {self.export_dir}"
            )
        if not (4 <= self.creo_version <= 30):
            raise ConfigError(f"CREO_MCP_CREO_VERSION no válida: {self.creo_version}")
        if self.toolkit_url:
            from urllib.parse import urlparse

            u = urlparse(self.toolkit_url)
            if u.scheme != "http" or not u.hostname or not is_loopback(u.hostname):
                raise ConfigError(f"CREO_MCP_TOOLKIT_URL debe ser http en localhost: {self.toolkit_url}")
        if not (1 <= self.espera_s <= 75):
            raise ConfigError("CREO_MCP_ESPERA debe estar entre 1 y 75 s (el enlace corta a los 80 s).")
        if self.timeout_s <= 0 or self.max_items <= 0:
            raise ConfigError("CREO_MCP_TIMEOUT y CREO_MCP_MAX_ITEMS deben ser > 0")


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ConfigError(f"No se puede resolver el host de CREOSON '{host}': {e}")
    return [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]


def is_loopback(host: str) -> bool:
    try:
        return all(a.is_loopback for a in _resolve(host))
    except ConfigError:
        return False


def check_host_allowed(host: str, allow_lan: bool) -> None:
    """Solo loopback; con allow_lan, además IP privadas. Nunca IP públicas."""
    addrs = _resolve(host)
    if all(a.is_loopback for a in addrs):
        return
    if allow_lan and all(a.is_private and not a.is_link_local for a in addrs):
        return
    raise ConfigError(
        f"Host de CREOSON '{host}' no permitido ({', '.join(map(str, addrs))}). "
        "Solo se admite localhost, o IP privadas con CREO_MCP_ALLOW_LAN=1."
    )
