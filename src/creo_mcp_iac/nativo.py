"""Puente con creo_iac_tk.exe: lecturas nativas de Creo que CREOSON no hace.

creo_iac_tk.exe (carpeta native/ del repositorio) es un programa en C hecho con
Creo Toolkit (modo asíncrono simple). Se conecta a la sesión de Creo ya abierta,
ejecuta UNA orden de lectura, escribe el resultado en JSON por la salida estándar
y se desconecta. No modifica ni guarda modelos.

Este módulo:
  - solo lanza las órdenes de ORDENES (todas de lectura);
  - prepara el entorno que necesita el programa fuera de Creo (PRO_COMM_MSG_EXE y
    las DLL de Creo en el PATH);
  - se coordina con CREOSON (un solo acceso a Creo a la vez) y lo registra en la
    auditoría;
  - traduce el árbol de elementos de los taladros a nombres legibles.

Licencia: el desarrollo con Creo Toolkit en C está cubierto por la licencia
Educational del departamento (uso sin fines lucrativos; confirmado el 2026-11-22).
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

# Orden -> tiempo máximo (s). Todas son de lectura.
ORDENES: dict[str, float] = {
    "estado": 30,
    "interferencias": 900,
    "taladros": 600,
    "posiciones": 300,
    "caras": 60,
    "seleccion": 30,
    "arbol": 60,
    "aristas": 60,
}
# Escritura en Creo (nunca guarda). Solo con CREO_MCP_ALLOW_OPERACIONES=1 en el servidor y
# el servicio arrancado con --escritura.
ESCRITURA: dict[str, float] = {"crear": 120, "borrar": 60}

_MODELO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,79}\.(PRT|ASM)$", re.IGNORECASE)

CREATE_NO_WINDOW = 0x08000000


class NativoError(RuntimeError):
    """El programa nativo no está disponible o devolvió un error."""


class NativoSinRespuesta(NativoError):
    """Se cortó la espera: Creo puede haber terminado o no. En una escritura, el resultado es
    desconocido y hay que comprobar Creo antes de seguir."""


def exe_por_defecto() -> Path:
    """native/build/creo_iac_tk.exe junto al código fuente (instalación editable)."""
    return Path(__file__).resolve().parents[2] / "native" / "build" / "creo_iac_tk.exe"


class Nativo:
    """Dos modos:
    - servicio (url): pide la orden a native/servicio_tk.py, arrancado por el usuario fuera
      de Claude. Es el modo por defecto: con Claude Desktop de la Tienda, el programa lanzado
      desde el paquete de Claude no encuentra la sesión de Creo (2026-11-22).
    - directo (url=None): lanza el programa desde este proceso (pruebas o instalaciones
      sin el paquete de la Tienda).
    """

    def __init__(self, exe: Path | None, creo_common: Path, audit: Any, lock: threading.Lock,
                 runner: Callable[..., Any] = subprocess.run, url: str | None = None, escritura: bool = False):
        self.escritura = escritura
        self.servicio_escritura: bool | None = None   # lo que dice GET /estado del servicio
        self.exe = exe
        self.creo_common = creo_common
        self.audit = audit
        self.lock = lock
        self.runner = runner
        self.url = url.rstrip("/") if url else None

    # -- disponibilidad -------------------------------------------------
    def disponible(self) -> tuple[bool, str]:
        if self.url:
            try:
                r = self._http("GET", "/estado", None, 3)
            except NativoError as e:
                return False, str(e)
            self.servicio_escritura = bool(r.get("escritura"))
            if not r.get("exe_existe"):
                return False, f"El servicio responde, pero no encuentra {r.get('exe')}: compílalo con native\\compilar.cmd."
            return True, (f"servicio {self.url} ({r.get('exe')}); escritura "
                          + ("permitida" if self.servicio_escritura else "no permitida (arrancado sin --escritura)"))
        if self.exe is None:
            return False, "No está configurado (CREO_MCP_TOOLKIT_EXE)."
        if not self.exe.is_file():
            return False, (f"No se encuentra {self.exe}. Compílalo con native\\compilar.cmd "
                           "o indica la ruta en CREO_MCP_TOOLKIT_EXE.")
        return True, str(self.exe)

    def entorno(self) -> dict[str, str]:
        env = dict(os.environ)
        base = self.creo_common / "x86e_win64"
        env.setdefault("PRO_COMM_MSG_EXE", str(base / "obj" / "pro_comm_msg.exe"))
        env["PATH"] = f"{base / 'lib'}{os.pathsep}{env.get('PATH', '')}"
        return env

    def _por_servicio(self, orden: str, modelo: str | None, rapido: bool, plan: str | None = None,
                      ids: list[int] | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        error = None
        datos: dict[str, Any] = {"orden": orden, "modelo": modelo.upper() if modelo else None, "rapido": bool(rapido)}
        if plan is not None:
            datos["plan"] = plan
        if ids:
            datos["ids"] = ids
        try:
            with self.lock:
                r = self._http("POST", "/orden", datos, (ORDENES.get(orden) or ESCRITURA[orden]) + 15)
            if not r.get("ok"):
                error = f"{r.get('fase')}: {r.get('error')} ({r.get('codigo')})"
                cls = NativoSinRespuesta if r.get("fase") == "sin_respuesta" else NativoError
                raise cls(f"Creo Toolkit ({r.get('fase')}): {r.get('error')} [código {r.get('codigo')}]")
            return r
        except NativoError as e:
            error = error or str(e)[:200]
            raise
        finally:
            self.audit.write(event="nativo", orden=orden, modelo=modelo, rapido=rapido or None, via="servicio",
                             ok=error is None, error=error, ms=round((time.perf_counter() - t0) * 1000))

    def _diagnostico(self, env: dict[str, str]) -> None:
        """Deja en entorno_servidor.txt (junto al programa) el entorno con el que lo lanza el
        servidor, para compararlo con una ventana de comandos (set > entorno_cmd.txt).
        Solo nombres de variables relevantes; sin credenciales."""
        claves = ("USERNAME", "USERDOMAIN", "COMPUTERNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                  "TEMP", "TMP", "HOMEDRIVE", "HOMEPATH", "PRO_COMM_MSG_EXE", "PRO_DIRECTORY",
                  "PRO_LANG", "SYSTEMROOT", "PATH")
        try:
            lineas = [f"{k}={env.get(k, '')}" for k in claves]
            lineas.append(f"cwd={self.exe.parent}")
            (self.exe.parent / "entorno_servidor.txt").write_text("\n".join(lineas), encoding="utf-8")
        except OSError:
            pass

    def _http(self, metodo: str, ruta: str, datos: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        abridor = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cuerpo = json.dumps(datos).encode("utf-8") if datos is not None else None
        req = urllib.request.Request(self.url + ruta, data=cuerpo, method=metodo,
                                     headers={"Content-Type": "application/json"})
        try:
            with abridor.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                raise NativoError(f"El servicio de lecturas nativas respondió {e.code}.")
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if "timed out" in str(e):
                raise NativoSinRespuesta(f"El servicio de lecturas nativas no respondió en {timeout:.0f} s. "
                                  "Creo puede seguir calculando: espera antes de repetir.")
            raise NativoError(
                f"El servicio de lecturas nativas no está arrancado ({self.url}). Ejecuta "
                "native\\iniciar_servicio_tk.cmd (fuera de Claude, como CREOSON) y deja la ventana abierta.")

    # -- ejecución ------------------------------------------------------
    def ejecutar(self, orden: str, modelo: str | None = None, rapido: bool = False,
                 plan: str | None = None, ids: list[int] | None = None) -> dict[str, Any]:
        if orden in ESCRITURA:
            if not self.escritura:
                raise NativoError("La escritura en Creo está desactivada (CREO_MCP_ALLOW_OPERACIONES=1).")
        elif orden not in ORDENES:
            raise NativoError(f"Orden nativa no permitida: {orden}")
        if modelo and not _MODELO_RE.match(modelo):
            raise NativoError(f"Nombre de modelo no válido para la lectura nativa: {modelo}")
        if orden == "crear":
            from .operaciones import plan_valido

            if not plan or not plan_valido(plan):
                raise NativoError("Plan de operación no válido.")
        if (orden == "borrar" or ids) and not (ids and all(isinstance(i, int) and i > 0 for i in ids)):
            raise NativoError("Ids de operaciones no válidos.")
        if self.url:
            return self._por_servicio(orden, modelo, rapido, plan, ids)
        ok, motivo = self.disponible()
        if not ok:
            raise NativoError(f"Las lecturas nativas de Creo no están disponibles: {motivo}")
        args = [str(self.exe), orden]
        if modelo:
            args += ["--modelo", modelo.upper()]
        if rapido:
            args.append("--rapido")
        if plan is not None:
            carpeta = self.exe.parent / "planes"
            carpeta.mkdir(exist_ok=True)
            fichero = carpeta / f"plan_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.txt"
            fichero.write_text(plan, encoding="ascii")
            args += ["--plan", str(fichero)]
        if ids:
            args += ["--ids", ",".join(map(str, ids))]
        limite = ORDENES.get(orden) or ESCRITURA[orden]
        t0 = time.perf_counter()
        error = None
        try:
            with self.lock:
                try:
                    env = self.entorno()
                    self._diagnostico(env)
                    p = self.runner(args, capture_output=True, timeout=limite, env=env,
                                    stdin=subprocess.DEVNULL, cwd=str(self.exe.parent),
                                    creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
                except subprocess.TimeoutExpired:
                    error = f"sin respuesta en {limite:.0f} s"
                    raise NativoSinRespuesta(
                        f"La lectura nativa '{orden}' no terminó en {limite:.0f} s y se ha cortado. "
                        "Creo puede seguir calculando: espera a que vuelva a responder antes de repetir."
                    )
                except OSError as e:
                    error = str(e)
                    raise NativoError(f"No se pudo lanzar {self.exe.name}: {e}")
            salida = (p.stdout or b"").decode("utf-8", "replace").strip()
            try:
                r = json.loads(salida.splitlines()[-1]) if salida else None
            except (json.JSONDecodeError, IndexError):
                r = None
            if not isinstance(r, dict):
                err = (p.stderr or b"").decode("utf-8", "replace").strip()[:300]
                error = f"salida no válida (código {p.returncode})"
                raise NativoError(
                    f"{self.exe.name} terminó sin un resultado válido (código {p.returncode}). "
                    + (f"Detalle: {err}. " if err else "")
                    + "Si Windows avisa de que falta ucore*.dll, revisa CREO_MCP_CREO_COMMON."
                )
            if not r.get("ok"):
                error = f"{r.get('fase')}: {r.get('error')} ({r.get('codigo')})"
                raise NativoError(f"Creo Toolkit ({r.get('fase')}): {r.get('error')} [código {r.get('codigo')}]")
            return r
        finally:
            self.audit.write(event="nativo", orden=orden, modelo=modelo, rapido=rapido or None,
                             ok=error is None, error=error, ms=round((time.perf_counter() - t0) * 1000))


# ---------------------------------------------------------------- utilidades

def nombre_fichero(nombre: str, tipo: str | int | None) -> str:
    """Nombre de Creo sin extensión + tipo -> NOMBRE.EXT (Toolkit da el nombre sin extensión)."""
    ext = {"ASM": ".ASM", "PRT": ".PRT", 1: ".ASM", 2: ".PRT", 98: ".PRT"}.get(tipo, ".PRT")
    return f"{nombre.upper()}{ext}"


# --------------------------------------------------------------- taladros

# Identificadores de elementos (ProElemId.h, Creo 12.4) usados en el resumen.
E = {
    "tipo": 1672,            # PRO_E_HLE_TYPE_NEW
    "estandar": 1673,        # PRO_E_HLE_STAN_TYPE
    "diametro": 82,          # PRO_E_DIAMETER (taladro recto)
    "diametro_broca": 1347,  # PRO_E_HLE_HOLEDIAM (normalizado)
    "prof_tipo_std": 1662,   # PRO_E_HLE_DEPTH
    "prof_broca": 1663,      # PRO_E_HLE_DRILLDEPTH
    "rosca_anadir": 1674,    # PRO_E_HLE_ADD_THREAD
    "rosca_prof_tipo": 1680,  # PRO_E_HLE_THRD_DEPTH
    "rosca_prof": 1354,      # PRO_E_HLE_THRDDEPTH
    "rosca_diam": 1353,      # PRO_E_HLE_THRDDIAM
    "cbore": 1675, "cbore_diam": 1350, "cbore_prof": 1349,
    "csink": 1676, "csink_diam": 1351, "csink_ang": 1352,
    "ajuste": 1661,          # PRO_E_HLE_FITTYPE
    # rosca cosmética
    "cos_diam": 4900, "cos_prof_opt": 4904, "cos_prof": 4906, "cos_normalizada": 4974,
}
TIPOS = {16: "recto", 24: "normalizado", 6: "croquis", 7: "personalizado"}
ESTANDAR = {14: "roscado", 15: "holgura", 17: "taladrado", 25: "cónico"}
AJUSTE = {0: "fino", 1: "libre", 2: "medio"}
PROF_RECTO = {1: "ciego", 2: "pasante (hasta el siguiente)", 3: "pasante", 4: "pasante hasta", 5: "hasta referencia",
              6: "ninguna", 7: "simétrica"}
PROF_STD = {29: "ciego", 30: "pasante (hasta el siguiente)", 31: "pasante", 40: "pasante hasta", 41: "hasta referencia"}
ROSCA_PROF = {32: "rosca pasante", 33: "rosca variable", 52: "rosca hasta referencia"}


def diametro_nominal(tamano: str | None) -> float | None:
    """Diámetro nominal de una designación métrica de Creo: 'M4x.7' -> 4, 'M2.5x.45' -> 2.5."""
    m = re.match(r"^\s*M(\d+(?:[.,]\d+)?)", str(tamano or ""), re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def _ultimo(elementos: dict[str, Any], eid: int, dentro_de: int | None = None) -> Any:
    for k, v in elementos.items():
        ids = k.split(".")
        if ids[-1] == str(eid) and (dentro_de is None or str(dentro_de) in ids[:-1]):
            return v
    return None


def _eje(ejes: list[list[float]]) -> dict[str, Any] | None:
    if not ejes:
        return None
    x1, y1, z1, x2, y2, z2 = ejes[0][:6]
    d = (x2 - x1, y2 - y1, z2 - z1)
    n = math.sqrt(sum(c * c for c in d)) or 1.0
    return {"punto": [round(x1, 4), round(y1, 4), round(z1, 4)],
            "direccion": [round(c / n, 6) for c in d], "longitud_eje": round(n, 4)}


def resumir_operacion(op: dict[str, Any]) -> dict[str, Any]:
    e = op.get("elementos") or {}
    r: dict[str, Any] = {"id": op.get("id"), "nombre": op.get("nombre"), "clase": op.get("clase")}
    if op.get("clase") == "rosca_cosmetica":
        r["normalizada"] = _ultimo(e, E["cos_normalizada"])
        r["diametro"] = _ultimo(e, E["cos_diam"])
        prof_sel = _ultimo(e, E["cos_prof_opt"])
        r["profundidad"] = None if prof_sel else _ultimo(e, E["cos_prof"])
        r["profundidad_tipo"] = "hasta referencia" if prof_sel else "ciega"
    else:
        tipo = _ultimo(e, E["tipo"])
        r["tipo"] = TIPOS.get(tipo, tipo)
        if tipo == 24:
            r["estandar"] = ESTANDAR.get(_ultimo(e, E["estandar"]), _ultimo(e, E["estandar"]))
            r["serie_rosca"] = op.get("serie_rosca")
            r["tamano_tornillo"] = op.get("tamano_tornillo")
            if r["estandar"] == "holgura":
                r["ajuste"] = AJUSTE.get(_ultimo(e, E["ajuste"]), _ultimo(e, E["ajuste"]))
            r["diametro"] = _ultimo(e, E["diametro_broca"])
            pt = _ultimo(e, E["prof_tipo_std"])
            r["profundidad_tipo"] = PROF_STD.get(pt, pt)
            r["profundidad"] = _ultimo(e, E["prof_broca"]) if pt == 29 else None
            if _ultimo(e, E["rosca_anadir"]) == 26:
                rt = _ultimo(e, E["rosca_prof_tipo"])
                rosca = {"diametro": _ultimo(e, E["rosca_diam"]),
                         "profundidad_tipo": ROSCA_PROF.get(rt, rt),
                         "profundidad": _ultimo(e, E["rosca_prof"]) if rt == 33 else None}
                if rosca["diametro"] is None:
                    # Creo no siempre devuelve PRO_E_HLE_THRDDIAM: el nominal sale del tamaño
                    # de tornillo (M4x.7 -> 4), que es la designación de la tabla de Creo.
                    d = diametro_nominal(op.get("tamano_tornillo"))
                    if d is not None:
                        rosca["diametro"] = d
                        rosca["diametro_origen"] = "nominal del tamaño de tornillo"
                r["rosca"] = {k: v for k, v in rosca.items() if v is not None}
            if _ultimo(e, E["cbore"]) == 27:
                r["avellanado_cilindrico"] = {"diametro": _ultimo(e, E["cbore_diam"]),
                                              "profundidad": _ultimo(e, E["cbore_prof"])}
            if _ultimo(e, E["csink"]) == 28:
                r["avellanado_conico"] = {"diametro": _ultimo(e, E["csink_diam"]),
                                          "angulo": _ultimo(e, E["csink_ang"])}
        else:
            r["diametro"] = _ultimo(e, E["diametro"])
            pt = _ultimo(e, 1688, dentro_de=1686)          # PRO_E_HOLE_DEPTH_TO_TYPE
            r["profundidad_tipo"] = PROF_RECTO.get(pt, pt)
            r["profundidad"] = _ultimo(e, 434, dentro_de=1686) if pt in (1, 7) else None
    r["eje"] = _eje(op.get("ejes") or [])
    return {k: v for k, v in r.items() if v is not None}


def resumir_taladros(datos: dict[str, Any]) -> dict[str, Any]:
    modelos = []
    por_tamano: dict[str, int] = {}
    total = 0
    for m in datos.get("modelos", []):
        ops = [resumir_operacion(op) for op in m.get("operaciones", [])]
        inst = m.get("instancias", 1)
        for op in ops:
            clave = op.get("tamano_tornillo") or (
                f"Ø{op['diametro']:g} {op.get('tipo', op.get('clase'))}" if isinstance(op.get("diametro"), (int, float))
                else op.get("clase"))
            if op.get("estandar"):
                clave = f"{clave} ({op['estandar']})"
            por_tamano[clave] = por_tamano.get(clave, 0) + inst
        total += len(ops) * inst
        if ops or m.get("suprimidas"):
            modelos.append({"modelo": nombre_fichero(m.get("nombre", ""), m.get("tipo")), "instancias": inst,
                            "unidad_longitud": m.get("unidad_longitud"), "operaciones": ops,
                            "suprimidas": m.get("suprimidas", 0)})
    return {"modelo": nombre_fichero(datos["modelo"]["nombre"], datos["modelo"]["tipo"]),
            "unidad_longitud": datos.get("unidad_longitud"),
            "modelos_revisados": len(datos.get("modelos", [])),
            "modelos_con_taladros": len(modelos),
            "operaciones_en_conjunto": total,
            "por_tamano": dict(sorted(por_tamano.items(), key=lambda kv: -kv[1])),
            "modelos": modelos}


# ---------------------------------------------------------- interferencias

CAT_INTERF = "interferencia"
CAT_DUPLICADO = "misma pieza (posible duplicado)"
CAT_PASADOR_MAL = "pasador sin taladro o con taladro pequeño"
CAT_PASADOR = "pasador (ajuste o elástico)"
CAT_TORNILLO = "tornillería (probable rosca modelada al diámetro nominal)"
ORDEN_CAT = {CAT_INTERF: 0, CAT_PASADOR_MAL: 1, CAT_DUPLICADO: 2, CAT_PASADOR: 3, CAT_TORNILLO: 4}

# Fracción del volumen del pasador por encima de la cual se considera que le falta el
# taladro: un pasador cilíndrico con su taladro a la medida nominal da 0 de interferencia
# (el ajuste m6 no se modela). Sin medidas en el nombre, se usa el umbral absoluto.
UMBRAL_PASADOR = 0.05
UMBRAL_PASADOR_MM3 = 1.0


def _volumen_pasador(t: dict[str, Any]) -> float | None:
    m = re.match(r"Ø?(\d+(?:[.,]\d+)?)", str(t.get("metrica") or ""))
    if not m or not t.get("longitud"):
        return None
    d = float(m.group(1).replace(",", "."))
    return math.pi / 4 * d * d * float(t["longitud"])


def _categoria(t: dict[str, Any] | None, volumen: float | None) -> tuple[str, str | None]:
    """Categoría de un par en el que una pieza es tornillería (t = su clasificación)."""
    if not t:
        return CAT_INTERF, None
    if t.get("elemento") != "Pasador":
        return CAT_TORNILLO, None
    if "lástico" in str(t.get("tipo") or ""):
        return CAT_PASADOR, "Pasador elástico: la interferencia es esperada."
    if volumen is None:
        return CAT_PASADOR, "Sin volumen (cálculo rápido): no se puede valorar."
    vp = _volumen_pasador(t)
    if vp:
        frac = volumen / vp
        nota = f"{frac:.0%} del volumen del pasador ({vp:.0f} mm³)."
        return (CAT_PASADOR_MAL if frac > UMBRAL_PASADOR else CAT_PASADOR), nota
    return (CAT_PASADOR_MAL if volumen > UMBRAL_PASADOR_MM3 else CAT_PASADOR), \
        "Sin medidas en el nombre del pasador: umbral de 1 mm³."


def resumir_interferencias(datos: dict[str, Any],
                           clasificar: Callable[[str], dict[str, Any] | None]) -> dict[str, Any]:
    """clasificar(nombre) -> clasificación de tornillería (bom_excel.clasificar_tornilleria) o None."""
    filas = []
    for it in datos.get("interferencias", []):
        a = nombre_fichero(it["a"].get("modelo", "?"), it["a"].get("tipo_elemento"))
        b = nombre_fichero(it["b"].get("modelo", "?"), it["b"].get("tipo_elemento"))
        ta, tb = clasificar(a), clasificar(b)
        vol = it.get("volumen")
        nota = None
        if a == b:
            cat = CAT_DUPLICADO
        else:
            # si una de las dos es pasador, manda el pasador
            t = ta if (ta and ta.get("elemento") == "Pasador") else tb if (tb and tb.get("elemento") == "Pasador") \
                else (ta or tb)
            cat, nota = _categoria(t, vol)
        fila = {"categoria": cat, "a": a, "ruta_a": it["a"].get("ruta", []), "b": b,
                "ruta_b": it["b"].get("ruta", []), "volumen": vol}
        if nota:
            fila["nota"] = nota
        filas.append(fila)
    filas.sort(key=lambda f: (ORDEN_CAT.get(f["categoria"], 9), -(f["volumen"] or 0)))
    por_cat: dict[str, int] = {}
    for f in filas:
        por_cat[f["categoria"]] = por_cat.get(f["categoria"], 0) + 1
    return {"conjunto": nombre_fichero(datos["modelo"]["nombre"], datos["modelo"]["tipo"]),
            "unidad_longitud": datos.get("unidad_longitud"),
            "rapido": datos.get("rapido", False),
            "total": len(filas), "por_categoria": dict(sorted(por_cat.items(), key=lambda kv: ORDEN_CAT.get(kv[0], 9))),
            "interferencias": filas}
