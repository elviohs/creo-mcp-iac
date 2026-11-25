"""Servicio local de lecturas nativas de Creo (creo_iac_tk.exe) para creo-mcp-iac.

Por qué existe: Claude Desktop (versión de la Tienda de Microsoft) lanza los programas
dentro de su paquete y Windows virtualiza parte del entorno; desde ahí creo_iac_tk.exe
no encuentra la sesión de Creo (visto el 2026-11-22), aunque desde una ventana de
comandos normal sí. Este servicio se arranca fuera de Claude, como CREOSON, y lanza el
programa por encargo del servidor MCP.

Seguridad:
  - escucha solo en 127.0.0.1;
  - solo acepta las órdenes de LECTURA de ORDENES y nombres de modelo válidos;
  - atiende una petición cada vez;
  - no guarda nada salvo una línea de registro por petición (servicio_tk.log).

Uso: native\\iniciar_servicio_tk.cmd [--escritura]  (o: python servicio_tk.py [puerto] [--escritura])
Con --escritura acepta además crear y borrar operaciones del asistente (nunca guarda).
Sin dependencias: solo la biblioteca estándar de Python.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

VERSION = "0.7.0"
PUERTO = 9057
ORDENES = {"estado": 30, "interferencias": 900, "taladros": 600, "posiciones": 300, "caras": 60, "seleccion": 30,
           "arbol": 60, "aristas": 60, "holguras": 600}
MAX_PARES = 20000
# Escritura en Creo (nunca guarda): solo si el servicio se arranca con --escritura.
ESCRITURA = {"crear": 120, "borrar": 60}
PERMITE_ESCRITURA = "--escritura" in sys.argv
MODELO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,79}\.(PRT|ASM)$", re.IGNORECASE)
AQUI = Path(__file__).resolve().parent
EXE = Path(os.environ.get("CREO_TK_EXE", AQUI / "build" / "creo_iac_tk.exe"))
COMMON = Path(os.environ.get("CREO_MCP_CREO_COMMON", r"C:\Program Files\PTC\Creo 12.4.1.0\Common Files"))
REGISTRO = AQUI / "build" / "servicio_tk.log"


def entorno() -> dict[str, str]:
    env = dict(os.environ)
    base = COMMON / "x86e_win64"
    env.setdefault("PRO_COMM_MSG_EXE", str(base / "obj" / "pro_comm_msg.exe"))
    env["PATH"] = f"{base / 'lib'}{os.pathsep}{env.get('PATH', '')}"
    return env


PLAN_RE = re.compile(r"^(modelo [A-Za-z0-9_\-]{1,80}\.PRT|serie [A-Za-z0-9_]{1,30}|tamano [A-Za-z0-9_.]{1,30}|"
                     r"e [0-9.]{3,150} (i -?\d+|d -?\d+(\.\d+)?|b [01]|r \d+ -?\d+|w [A-Z0-9_]{1,31}|c|k( \d+){1,64})|"
                     r"verificar( -?\d+(\.\d+)?){6}|alternar(_i)? [0-9.]+ [0-9.]+|patron \d+|"
                     r"esperado( -?\d+(\.\d+)?){3}|sin_aristas( \d+){1,64}|"
                     r"sk_ref \d+ \d+|sk_linea( -?\d+(\.\d+)?){6}|sk_arco( -?\d+(\.\d+)?){12}|sk_circulo( -?\d+(\.\d+)?){4}|"
                     r"sk_cota ((diam|radio) \d+|ref \d+ [cse] \d+|refl \d+ \d+|entre \d+ \d+)( -?\d+(\.\d+)?){4}|"
                     r"sentido (-1|0|1)|probar_sentidos|lado_material [0-3]|rango [XYZ]( -?\d+(\.\d+)?){2}|"
                     r"volumen(_max)? -?\d+(\.\d+)?|volumen_signo -?1)$")


def plan_valido(texto) -> bool:
    if not isinstance(texto, str) or len(texto) > 150000:
        return False
    lineas = [x for x in texto.splitlines() if x.strip()]
    return 0 < len(lineas) <= 600 and all(PLAN_RE.match(x) for x in lineas)


def pares_validos(pares) -> bool:
    def ruta(r):
        return isinstance(r, list) and 0 < len(r) <= 25 and all(isinstance(i, int) and not isinstance(i, bool)
                                                                 and i > 0 for i in r)
    return (isinstance(pares, list) and 0 < len(pares) <= MAX_PARES
            and all(isinstance(p, list) and len(p) == 2 and ruta(p[0]) and ruta(p[1]) for p in pares))


def texto_pares(pares) -> str:
    return "".join(f"{'/'.join(map(str, a))} {'/'.join(map(str, b))}\n" for a, b in pares)


def ejecutar(peticion: dict, runner=subprocess.run) -> tuple[int, dict]:
    """Valida la petición y lanza creo_iac_tk.exe. Devuelve (código HTTP, respuesta)."""
    orden = peticion.get("orden")
    if orden in ESCRITURA and not PERMITE_ESCRITURA:
        return 403, {"ok": False, "fase": "servicio", "codigo": -1,
                     "error": "El servicio no permite escribir en Creo: arráncalo con iniciar_servicio_tk.cmd --escritura"}
    if orden not in ORDENES and orden not in ESCRITURA:
        return 400, {"ok": False, "fase": "servicio", "codigo": -1, "error": f"Orden no permitida: {orden}"}
    args = [str(EXE), orden]
    limite = ORDENES.get(orden) or ESCRITURA[orden]
    if orden == "crear":
        plan = peticion.get("plan")
        if not plan_valido(plan):
            return 400, {"ok": False, "fase": "servicio", "codigo": -1, "error": "Plan no válido"}
        carpeta = EXE.parent / "planes"
        carpeta.mkdir(exist_ok=True)
        fichero = carpeta / f"plan_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{int(time.time() * 1000) % 1000:03d}.txt"
        fichero.write_text(plan, encoding="ascii")
        args += ["--plan", str(fichero)]
    if orden == "holguras":
        pares = peticion.get("pares")
        if not pares_validos(pares):
            return 400, {"ok": False, "fase": "servicio", "codigo": -1, "error": "Pares no válidos"}
        carpeta = EXE.parent / "planes"
        carpeta.mkdir(exist_ok=True)
        fichero = carpeta / f"pares_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.txt"
        fichero.write_text(texto_pares(pares), encoding="ascii")
        args += ["--pares", str(fichero)]
    if orden == "arbol" and peticion.get("ids") is not None:
        ids = peticion.get("ids")
        if not (isinstance(ids, list) and 0 < len(ids) <= 64 and all(isinstance(i, int) and i > 0 for i in ids)):
            return 400, {"ok": False, "fase": "servicio", "codigo": -1, "error": "ids no válidos"}
        args += ["--ids", ",".join(map(str, ids))]
    if orden == "borrar":
        ids = peticion.get("ids")
        if not (isinstance(ids, list) and 0 < len(ids) <= 64 and all(isinstance(i, int) and i > 0 for i in ids)):
            return 400, {"ok": False, "fase": "servicio", "codigo": -1, "error": "ids no válidos"}
        args += ["--ids", ",".join(map(str, ids))]
    modelo = peticion.get("modelo")
    if modelo:
        if not isinstance(modelo, str) or not MODELO_RE.match(modelo):
            return 400, {"ok": False, "fase": "servicio", "codigo": -1, "error": "Nombre de modelo no válido"}
        args += ["--modelo", modelo.upper()]
    if peticion.get("rapido") is True:
        args.append("--rapido")
    if not EXE.is_file():
        return 503, {"ok": False, "fase": "servicio", "codigo": -1,
                     "error": f"No se encuentra {EXE}; compílalo con native\\compilar.cmd"}
    try:
        p = runner(args, capture_output=True, timeout=limite, env=entorno(),
                   stdin=subprocess.DEVNULL, cwd=str(EXE.parent))
    except subprocess.TimeoutExpired:
        # En una escritura no se sabe si Creo llegó a crear o borrar: el servidor lo trata
        # como «resultado desconocido».
        return 504, {"ok": False, "fase": "sin_respuesta", "codigo": -1,
                     "error": f"Sin respuesta de Creo en {limite} s"}
    salida = (p.stdout or b"").decode("utf-8", "replace").strip()
    try:
        r = json.loads(salida.splitlines()[-1])
        if not isinstance(r, dict):
            raise ValueError
    except (ValueError, IndexError):
        err = (p.stderr or b"").decode("utf-8", "replace").strip()[:300]
        return 502, {"ok": False, "fase": "servicio", "codigo": p.returncode,
                     "error": f"creo_iac_tk.exe terminó sin resultado válido. {err}".strip()}
    return 200, r


class Manejador(BaseHTTPRequestHandler):
    server_version = f"servicio_tk/{VERSION}"

    def _responder(self, codigo: int, datos: dict) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/estado":
            self._responder(200, {"ok": True, "servicio": VERSION, "exe": str(EXE), "exe_existe": EXE.is_file(),
                                  "escritura": PERMITE_ESCRITURA})
        else:
            self._responder(404, {"ok": False, "error": "Ruta desconocida"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/orden":
            self._responder(404, {"ok": False, "error": "Ruta desconocida"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 10000:
                raise ValueError
            peticion = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(peticion, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._responder(400, {"ok": False, "fase": "servicio", "codigo": -1, "error": "Petición no válida"})
            return
        t0 = time.perf_counter()
        codigo, r = ejecutar(peticion)
        self._responder(codigo, r)
        linea = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {peticion.get('orden')} {peticion.get('modelo') or '-'} "
                 f"ok={r.get('ok')} {round((time.perf_counter() - t0) * 1000)} ms "
                 f"{'' if r.get('ok') else r.get('error', '')}")
        print(linea, flush=True)
        try:
            with REGISTRO.open("a", encoding="utf-8") as f:
                f.write(linea + "\n")
        except OSError:
            pass

    def log_message(self, *a) -> None:  # silencio el registro por defecto de http.server
        pass


def main() -> None:
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    puerto = int(nums[0]) if nums else PUERTO
    srv = HTTPServer(("127.0.0.1", puerto), Manejador)
    print(f"Servicio de lecturas nativas de Creo {VERSION} en http://127.0.0.1:{puerto}")
    print(f"Programa: {EXE} ({'existe' if EXE.is_file() else 'NO EXISTE: compila con compilar.cmd'})")
    print("Escritura en Creo: " + ("PERMITIDA (crear/borrar operaciones del asistente, sin guardar)"
                                   if PERMITE_ESCRITURA else "no (solo lecturas)"))
    print("Deja esta ventana abierta mientras uses el asistente. Ctrl+C para parar.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
