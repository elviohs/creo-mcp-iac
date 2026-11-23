"""Pruebas del puente con creo_iac_tk.exe (lecturas nativas con Creo Toolkit), sin Creo."""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))
from test_server import call, fake, make  # noqa: E402,F401

from creo_mcp_iac import nativo  # noqa: E402
from creo_mcp_iac.server import build_server  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


INTERF = {"ok": True, "version_tk": "0.2.0", "orden": "interferencias",
          "modelo": {"nombre": "TOP", "tipo": "ASM"}, "unidad_longitud": "mm", "rapido": False, "total": 3,
          "interferencias": [
              {"a": {"modelo": "DIN912-M4X10", "tipo_elemento": 2, "ruta": [40]},
               "b": {"modelo": "PLACA", "tipo_elemento": 2, "ruta": [12]}, "volumen": 5.2},
              {"a": {"modelo": "EJE", "tipo_elemento": 2, "ruta": [7]},
               "b": {"modelo": "PLACA", "tipo_elemento": 2, "ruta": [12]}, "volumen": 319.38},
              {"a": {"modelo": "TAPA", "tipo_elemento": 2, "ruta": [3, 4]},
               "b": {"modelo": "TAPA", "tipo_elemento": 2, "ruta": [5]}, "volumen": 1200.0},
              {"a": {"modelo": "PLACA", "tipo_elemento": 2, "ruta": [12]},
               "b": {"modelo": "ISO2338-5M6X20-ST", "tipo_elemento": 2, "ruta": [50]}, "volumen": 194.32},
              {"a": {"modelo": "ISO2338-5M6X20-ST", "tipo_elemento": 2, "ruta": [51]},
               "b": {"modelo": "EJE", "tipo_elemento": 2, "ruta": [7]}, "volumen": 0.4},
              {"a": {"modelo": "ISO8752-4X20", "tipo_elemento": 2, "ruta": [52]},
               "b": {"modelo": "EJE", "tipo_elemento": 2, "ruta": [7]}, "volumen": 30.0}]}

TALADROS = {"ok": True, "version_tk": "0.2.0", "orden": "taladros",
            "modelo": {"nombre": "TOP", "tipo": "ASM"}, "unidad_longitud": "mm", "modelos": [
                {"nombre": "TOP", "tipo": "ASM", "instancias": 1, "operaciones": [], "suprimidas": 0},
                {"nombre": "PLACA", "tipo": "PRT", "instancias": 2, "unidad_longitud": "mm", "suprimidas": 1,
                 "operaciones": [
                     {"id": 41, "nombre": "M6_ROSCADO", "clase": "taladro", "serie_rosca": "ISO",
                      "tamano_tornillo": "M6x1",
                      "elementos": {"386.387": 911, "386.1671.1672": 24, "386.1671.1673": 14,
                                    "386.1671.1347": 5.0, "386.1671.1662": 29, "386.1671.1663": 15.0,
                                    "386.1671.1674": 26, "386.1671.1680": 33, "386.1671.1354": 12.0,
                                    "386.1671.1353": 6.0, "386.1671.1675": -1, "386.1671.1676": -1},
                      "ejes": [[10, 20, 0, 10, 20, -15]]},
                     {"id": 45, "nombre": "M4_SIN_DIAM", "clase": "taladro", "serie_rosca": "ISO",
                      "tamano_tornillo": "M4x.7",
                      "elementos": {"386.1671.1672": 24, "386.1671.1673": 14, "386.1671.1347": 3.3,
                                    "386.1671.1662": 30, "386.1671.1674": 26, "386.1671.1680": 33,
                                    "386.1671.1354": 6.3},
                      "ejes": []},
                     {"id": 52, "nombre": "PASANTE", "clase": "taladro",
                      "elementos": {"386.1671.1672": 16, "386.1671.82": 6.6,
                                    "386.1671.1684.1686.1688": 3, "386.1671.1684.1685.1687": 6},
                      "ejes": [[0, 0, 5, 0, 0, -5]]},
                     {"id": 60, "nombre": "COSMETIC_THREAD", "clase": "rosca_cosmetica",
                      "elementos": {"386.4974": True, "386.4900": 8.0, "386.4903.4904": False,
                                    "386.4903.4906": 10.0}, "ejes": []}]}]}


class Runner:
    def __init__(self, respuestas):
        self.respuestas = respuestas
        self.llamadas = []

    def __call__(self, args, **kw):
        self.llamadas.append(args)
        orden = args[1]
        r = self.respuestas[orden]
        if isinstance(r, BaseException):
            raise r
        return subprocess.CompletedProcess(args, 0, json.dumps(r).encode("utf-8"), b"")


def con_nativo(tmp_path, fake, runner, export=True):
    s, gw, _ = make(tmp_path, fake, export=export)
    exe = tmp_path / "creo_iac_tk.exe"
    exe.write_bytes(b"MZ")
    nat = nativo.Nativo(exe, tmp_path / "Common Files", gw.audit, gw._lock, runner=runner)
    return s, gw, build_server(s, gw, nat)


@pytest.mark.anyio
async def test_interferencias_clasifica_y_excel(tmp_path, fake):
    fake.files = ["TOP.ASM", "PLACA.PRT", "EJE.PRT"]
    fake.active = "TOP.ASM"
    runner = Runner({"interferencias": INTERF})
    s, gw, srv = con_nativo(tmp_path, fake, runner)
    err, r = await call(srv, "interferencias_creo")
    assert not err, r
    assert runner.llamadas[0][1:] == ["interferencias", "--modelo", "TOP.ASM"]
    assert r["total"] == 6
    f = r["interferencias"]
    cats = [x["categoria"] for x in f]
    assert cats[0] == "interferencia" and f[0]["a"] == "EJE.PRT"
    assert cats[1] == nativo.CAT_PASADOR_MAL and "49%" in f[1]["nota"]       # 194 de ~393 mm³
    assert cats[2].startswith("misma pieza")
    assert cats[3] == cats[4] == nativo.CAT_PASADOR                         # ajuste (0,4 mm³) y elástico
    assert cats[5].startswith("tornillería")
    assert list(r["por_categoria"])[:2] == ["interferencia", nativo.CAT_PASADOR_MAL]
    wb = load_workbook(s.export_dir / r["fichero"])
    assert wb.sheetnames == ["Resumen", "Interferencias"]
    assert wb["Interferencias"].max_row == 7
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(a.get("event") == "nativo" and a.get("orden") == "interferencias" and a["ok"] for a in audit)


@pytest.mark.anyio
async def test_interferencias_solo_conjuntos(tmp_path, fake):
    fake.files = ["PLACA.PRT"]
    fake.active = "PLACA.PRT"
    s, gw, srv = con_nativo(tmp_path, fake, Runner({}))
    err, msg = await call(srv, "interferencias_creo")
    assert err and "no es un conjunto" in msg


@pytest.mark.anyio
async def test_taladros_resumen(tmp_path, fake):
    fake.files = ["TOP.ASM", "PLACA.PRT"]
    fake.active = "TOP.ASM"
    s, gw, srv = con_nativo(tmp_path, fake, Runner({"taladros": TALADROS}))
    err, r = await call(srv, "taladros_creo")
    assert not err, r
    assert r["modelos_con_taladros"] == 1 and r["taladros_en_conjunto"] == 8
    ops = {o["id"]: o for o in r["operaciones"]}
    m6 = ops[41]
    assert m6["tipo"] == "normalizado" and m6["estandar"] == "roscado" and m6["tamano_tornillo"] == "M6x1"
    assert m6["diametro"] == 5.0 and m6["profundidad"] == 15.0 and m6["profundidad_tipo"] == "ciego"
    assert m6["rosca"] == {"diametro": 6.0, "profundidad_tipo": "rosca variable", "profundidad": 12.0}
    assert m6["eje"]["direccion"] == [0, 0, -1] and m6["eje"]["punto"] == [10, 20, 0]
    assert ops[52]["tipo"] == "recto" and ops[52]["profundidad_tipo"] == "pasante" and "profundidad" not in ops[52]
    m4 = ops[45]
    assert m4["profundidad_tipo"] == "pasante (hasta el siguiente)" and "profundidad" not in m4
    assert m4["rosca"]["diametro"] == 4.0 and m4["rosca"]["diametro_origen"].startswith("nominal")
    assert m4["rosca"]["profundidad"] == 6.3
    assert "diametro_origen" not in m6["rosca"]
    assert nativo.diametro_nominal("M2.5x.45") == 2.5 and nativo.diametro_nominal("1/4-20 UNC") is None
    assert ops[60]["clase"] == "rosca_cosmetica" and ops[60]["profundidad"] == 10.0
    assert r["por_tamano"]["M6x1 (roscado)"] == 2
    wb = load_workbook(s.export_dir / r["fichero"])
    assert wb["Taladros"].max_row == 5


@pytest.mark.anyio
async def test_sin_programa_nativo(tmp_path, fake):
    fake.files = ["TOP.ASM"]
    fake.active = "TOP.ASM"
    s, gw, _ = make(tmp_path, fake)
    nat = nativo.Nativo(tmp_path / "no_existe.exe", tmp_path, gw.audit, gw._lock, runner=Runner({}))
    srv = build_server(s, gw, nat)
    err, msg = await call(srv, "interferencias_creo")
    assert err and "no están disponibles" in msg
    err, cap = await call(srv, "creo_capacidades")
    assert not err and cap["lecturas_nativas_toolkit"]["disponibles"] is False


@pytest.mark.anyio
async def test_error_de_toolkit_y_tiempo(tmp_path, fake):
    fake.files = ["TOP.ASM"]
    fake.active = "TOP.ASM"
    malo = {"ok": False, "fase": "calculo", "codigo": -1, "error": "Creo no pudo calcular las interferencias"}
    s, gw, srv = con_nativo(tmp_path, fake, Runner({"interferencias": malo,
                                                     "taladros": subprocess.TimeoutExpired("x", 1)}))
    err, msg = await call(srv, "interferencias_creo")
    assert err and "no pudo calcular" in msg
    err, msg = await call(srv, "taladros_creo")
    assert err and "no terminó" in msg


def test_ordenes_permitidas_y_nombres(tmp_path):
    lock = threading.Lock()

    class A:
        def write(self, **k):
            pass

    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    n = nativo.Nativo(exe, tmp_path, A(), lock, runner=Runner({}))
    with pytest.raises(nativo.NativoError):
        n.ejecutar("guardar")
    with pytest.raises(nativo.NativoError):
        n.ejecutar("taladros", "../X.PRT")
    assert set(nativo.ORDENES) == {"estado", "interferencias", "taladros", "posiciones", "caras", "seleccion", "arbol"}
    assert set(nativo.ESCRITURA) == {"crear", "borrar"}
    env = n.entorno()
    assert env["PATH"].startswith(str(tmp_path / "x86e_win64" / "lib"))
    assert nativo.nombre_fichero("placa", 98) == "PLACA.PRT"


@pytest.mark.anyio
async def test_reconecta_si_creoson_se_reinicio(tmp_path, fake):
    """CREOSON reiniciado con el servidor abierto: la sesión guardada no existe y se reconecta."""
    fake.files = ["TOP.ASM"]
    fake.active = "TOP.ASM"
    s, gw, srv = make(tmp_path, fake)
    err, r = await call(srv, "listar_modelos")
    assert not err
    fake.sesion_caducada = 1
    err, r = await call(srv, "listar_modelos")
    assert not err, r
    assert fake.commands.count(("connection", "connect")) == 2


@pytest.mark.anyio
async def test_lectura_nativa_sin_creoson(tmp_path):
    """Las lecturas nativas no dependen de CREOSON: sin él se usa el modelo activo de Creo."""
    from creo_mcp_iac.config import Settings
    from creo_mcp_iac.gateway import CreoGateway

    exp = tmp_path / "export"
    exp.mkdir()
    s = Settings(creoson_host="127.0.0.1", creoson_port=1, export_dir=exp,
                 audit_log=tmp_path / "a.jsonl", timeout_s=2)
    gw = CreoGateway(s)
    exe = tmp_path / "creo_iac_tk.exe"
    exe.write_bytes(b"MZ")
    runner = Runner({"taladros": TALADROS})
    srv = build_server(s, gw, nativo.Nativo(exe, tmp_path, gw.audit, gw._lock, runner=runner))
    err, r = await call(srv, "taladros_creo")
    assert not err, r
    assert runner.llamadas[0][1:] == ["taladros"]
    assert r["modelo"] == "TOP.ASM" and r["fichero"].startswith("TALADROS_TOP")


# --- modo servicio (native/servicio_tk.py, arrancado fuera de Claude) ----------

def _servicio(tmp_path, monkeypatch, respuestas):
    import importlib.util
    from http.server import HTTPServer

    ruta = Path(__file__).resolve().parents[1] / "native" / "servicio_tk.py"
    spec = importlib.util.spec_from_file_location("servicio_tk", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    exe = tmp_path / "creo_iac_tk.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(mod, "EXE", exe)
    monkeypatch.setattr(mod, "REGISTRO", tmp_path / "servicio.log")
    runner = Runner(respuestas)
    original = mod.ejecutar
    monkeypatch.setattr(mod, "ejecutar", lambda p: original(p, runner=runner))
    mod.ejecutar_original = original
    srv = HTTPServer(("127.0.0.1", 0), mod.Manejador)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, runner, mod


@pytest.mark.anyio
async def test_modo_servicio(tmp_path, fake, monkeypatch):
    fake.files = ["TOP.ASM", "PLACA.PRT"]
    fake.active = "TOP.ASM"
    srv, runner, _ = _servicio(tmp_path, monkeypatch, {"taladros": TALADROS, "interferencias": INTERF})
    try:
        s, gw, _ = make(tmp_path, fake)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        nat = nativo.Nativo(None, tmp_path, gw.audit, gw._lock, url=url)
        assert nat.disponible()[0]
        server = build_server(s, gw, nat)
        err, r = await call(server, "taladros_creo")
        assert not err, r
        assert r["taladros_en_conjunto"] == 8
        assert runner.llamadas[0][1:] == ["taladros", "--modelo", "TOP.ASM"]
        err, r = await call(server, "interferencias_creo", rapido=True)
        assert not err and r["total"] == 6
        assert runner.llamadas[1][-1] == "--rapido"
        assert (tmp_path / "servicio.log").read_text(encoding="utf-8").count("ok=True") == 2
    finally:
        srv.shutdown()


def test_servicio_rechaza_ordenes_y_modelos(tmp_path, monkeypatch):
    srv, runner, mod = _servicio(tmp_path, monkeypatch, {})
    srv.shutdown()
    assert mod.ejecutar_original({"orden": "guardar"}, runner=runner)[0] == 400
    assert mod.ejecutar_original({"orden": "taladros", "modelo": "..\\X.PRT"}, runner=runner)[0] == 400
    assert runner.llamadas == []


@pytest.mark.anyio
async def test_servicio_parado(tmp_path, fake):
    fake.files = ["TOP.ASM"]
    fake.active = "TOP.ASM"
    s, gw, _ = make(tmp_path, fake)
    nat = nativo.Nativo(None, tmp_path, gw.audit, gw._lock, url="http://127.0.0.1:1")
    err, msg = await call(build_server(s, gw, nat), "taladros_creo")
    assert err and "iniciar_servicio_tk.cmd" in msg


def test_url_solo_local():
    from creo_mcp_iac.config import ConfigError, Settings

    with pytest.raises(ConfigError):
        Settings.from_env({"CREO_MCP_TOOLKIT_URL": "http://10.0.0.5:9057"})
    assert Settings.from_env({"CREO_MCP_TOOLKIT_URL": "directo"}).toolkit_url is None
    assert Settings.from_env({}).toolkit_url == "http://127.0.0.1:9057"


# --- tareas largas -------------------------------------------------------------

class RunnerLento(Runner):
    def __init__(self, respuestas, segundos):
        super().__init__(respuestas)
        self.segundos = segundos

    def __call__(self, args, **kw):
        import time as _t
        _t.sleep(self.segundos)
        return super().__call__(args, **kw)


@pytest.mark.anyio
async def test_tarea_larga_en_curso_y_resultado(tmp_path, fake):
    from dataclasses import replace

    fake.files = ["TOP.ASM", "PLACA.PRT"]
    fake.active = "TOP.ASM"
    runner = RunnerLento({"interferencias": INTERF}, 1.5)
    s, gw, _ = make(tmp_path, fake)
    s = replace(s, espera_s=0.3)
    exe = tmp_path / "creo_iac_tk.exe"
    exe.write_bytes(b"MZ")
    srv = build_server(s, gw, nativo.Nativo(exe, tmp_path, gw.audit, gw._lock, runner=runner))
    err, r = await call(srv, "interferencias_creo")
    assert not err and r["estado"] == "en_curso" and r["tarea_id"].startswith("T")
    tid = r["tarea_id"]
    # repetir mientras sigue en curso no lanza otra
    err, r2 = await call(srv, "interferencias_creo")
    assert not err and r2["tarea_id"] == tid and "aviso" in r2
    err, lista = await call(srv, "listar_tareas")
    assert lista["tareas"][0]["estado"] == "en_curso"
    import asyncio
    await asyncio.sleep(1.6)
    err, fin = await call(srv, "resultado_tarea", tarea_id=tid)
    assert not err and fin["total"] == 6 and fin["fichero"].startswith("INTERF_")
    assert len(runner.llamadas) == 1
    err, lista = await call(srv, "listar_tareas")
    assert lista["tareas"][0]["estado"] == "terminada"


@pytest.mark.anyio
async def test_tarea_con_error_y_rapida(tmp_path, fake):
    fake.files = ["TOP.ASM"]
    fake.active = "TOP.ASM"
    malo = {"ok": False, "fase": "calculo", "codigo": -1, "error": "Creo no pudo calcular las interferencias"}
    s, gw, srv = con_nativo(tmp_path, fake, Runner({"interferencias": malo, "taladros": TALADROS}))
    err, r = await call(srv, "taladros_creo")          # rápida: resultado directo
    assert not err and r["taladros_en_conjunto"] == 8
    err, msg = await call(srv, "interferencias_creo")   # error: llega como error de la herramienta
    assert err and "no pudo calcular" in msg
    err, msg = await call(srv, "resultado_tarea", tarea_id="T999-000000")
    assert err and "No existe la tarea" in msg


def test_espera_configurable():
    from creo_mcp_iac.config import ConfigError, Settings

    assert Settings.from_env({}).espera_s == 55
    with pytest.raises(ConfigError):
        Settings.from_env({"CREO_MCP_ESPERA": "90"})
