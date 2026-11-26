"""Pruebas de los taladros (operaciones sencillas): planes, propuesta, creación y seguridad, sin Creo."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_server import call, fake, make  # noqa: E402,F401

from creo_mcp_iac import nativo, operaciones as ops  # noqa: E402
from creo_mcp_iac.config import Settings  # noqa: E402
from creo_mcp_iac.gateway import CreoGateway  # noqa: E402
from creo_mcp_iac.server import build_server  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


HOL = """TABLE_DATA
THREAD_SERIES    ISO
TABLE_UNITS      mm

THREAD_DATA
FASTENER_ID  BASIC_DIAM  THREAD  TAP_DR  TAP_DEC  PERCENT_THREAD  CLEAR_DR_CLOSE  CLOSE_DEC  CLEAR_DR_FREE  FREE_DEC  CLEAR_DR_MED  MED_DEC  CBOREDIAM   CBOREDEPTH   CSINKDIAM   CSINKANGLE   BOTCSINKDIAM   BOTCSINKANGLE
M2.5x.45     2.500       0.45    2.05    2.050    65%             2.7             2.7        3.1            3.1       2.9           2.9      5.4         2.5          0           90           0              90
M5x.5        5.000       0.5     4.5     4.500    65%             5.3             5.3        5.8            5.8       5.5           5.5      9.75        5.0          11.20       90           11.20          90
M5x.8        5.000       0.8     4.2     4.200    65%             5.3             5.3        5.8            5.8       5.5           5.5      9.75        5.0          11.20       90           11.20          90
M6x1         6.000       1       5       5.000    65%             6.4             6.4        7.0            7.0       6.6           6.6      11.2        6.0          13.44       90           13.44          90
"""

# PRUEBA_CMD.PRT (bloque 120 x 20 x 80), leída con creo_iac_tk el 2026-11-23
CARAS = {"ok": True, "version_tk": "0.3.1", "orden": "caras", "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"},
         "unidad_longitud": "mm", "cuerpo_por_defecto": -5778,
         "caras_planas": [
             {"id": 957, "area": 9535.88, "normal_exterior": [0, -1, 0], "punto_plano": [3e-14, 0, 3e-15],
              "caja_min": [-60, 0, -40], "caja_max": [60, 0, 40]},
             {"id": 962, "area": 9516.25, "normal_exterior": [0, 1, 0], "punto_plano": [3e-14, 20, 3e-15],
              "caja_min": [-60, 20, -40], "caja_max": [60, 20, 40]},
             {"id": 967, "area": 2400, "normal_exterior": [0, 0, -1], "punto_plano": [2e-14, 10, -40],
              "caja_min": [-60, 0, -40], "caja_max": [60, 20, -40]},
             {"id": 969, "area": 1600, "normal_exterior": [-1, 0, 0], "punto_plano": [-60, 10, 0],
              "caja_min": [-60, 0, -40], "caja_max": [-60, 20, 40]},
             {"id": 971, "area": 2400, "normal_exterior": [0, 0, 1], "punto_plano": [3e-14, 10, 40],
              "caja_min": [-60, 0, 40], "caja_max": [60, 20, 40]},
             {"id": 973, "area": 1600, "normal_exterior": [1, 0, 0], "punto_plano": [60, 10, 7e-15],
              "caja_min": [60, 0, -40], "caja_max": [60, 20, 40]}],
         "planos_referencia": [
             {"nombre": "PERFIL", "id": 2, "id_operacion": 1, "normal": [1, 0, 0], "origen": [0, 0, 0]},
             {"nombre": "PLANTA", "id": 4, "id_operacion": 3, "normal": [0, 1, 0], "origen": [0, 0, 0]},
             {"nombre": "ALZADO", "id": 6, "id_operacion": 5, "normal": [0, 0, 1], "origen": [0, 0, 0]},
             {"nombre": "no_name", "id": 956, "id_operacion": 955, "normal": [0, 1, 0], "origen": [0, 0, 0]}]}


def tabla(tmp_path):
    f = tmp_path / "ISO.hol"
    f.write_text(HOL, encoding="latin-1")
    return ops.leer_hol(f)


# --------------------------------------------------------------- planes

def test_tabla_y_metricas(tmp_path):
    t = tabla(tmp_path)
    assert ops.buscar_tamano(t, "M6").tamano == "M6x1"
    assert ops.buscar_tamano(t, "m5").tamano == "M5x.8"            # paso normal
    assert ops.buscar_tamano(t, "M5x0.5").tamano == "M5x.5"
    assert ops.buscar_tamano(t, "M2,5").broca_rosca == 2.05
    assert ops.buscar_tamano(t, "M6x1").holgura == {"fino": 6.4, "libre": 7.0, "medio": 6.6}
    for malo in ("6", "M7", "M6x2"):
        with pytest.raises(ops.PlanError):
            ops.buscar_tamano(t, malo)


def test_plan_recto_cara_superior(tmp_path):
    r = ops.planificar_taladro({"tipo": "recto", "diametro": 8, "cara": "+Y", "punto": [10, 0, 5]},
                               CARAS, None, "IA_D8_1")
    plan = r["plan"].splitlines()
    assert plan[0] == "modelo PRUEBA_CMD.PRT"
    assert "e 386.1671.82 d 8.0000" in plan and "e 386.1671.1684.1686.1688 i 3" in plan      # pasante
    # cara 962, PERFIL (x) y ALZADO (z): distancias con signo a lo largo de la normal del plano
    assert "e 386.1340.1341 r 5 962" in plan
    assert "e 386.1340.704 r 5 2" in plan and "e 386.1340.706 d 10.0000" in plan
    assert "e 386.1340.707 r 5 6" in plan and "e 386.1340.708 d 5.0000" in plan
    assert "e 386.6314.6316 r 98 -5778" in plan
    assert plan[-2].startswith("verificar 10.000000 20.000000 5.000000 0.000000 1.000000")    # proyectado a y=20
    assert ops.plan_valido(r["plan"])
    assert r["resumen"]["referencias"] == ["PERFIL a +10.000", "ALZADO a +5.000"]
    assert r["ids_caras"] == [962] and r["ids_planos"] == [2, 6]


def test_plan_roscado_y_holgura(tmp_path):
    t = tabla(tmp_path)
    r = ops.planificar_taladro({"tipo": "roscado", "metrica": "M6", "profundidad": 15, "cara": 962,
                                "punto": [-20, 20, 10]}, CARAS, t, "IA_M6X1_1")
    p = r["plan"]
    assert "serie ISO" in p and "tamano M6x1" in p
    assert "e 386.1671.1673 i 14" in p and "e 386.1671.1674 i 26" in p
    assert "e 386.1671.1347 d 5.0000" in p and "e 386.1671.1663 d 15.0000" in p
    assert "e 386.1671.1354 d 9.0000" in p                                   # 1,5·d por defecto
    assert r["avisos"] and "1,5·d" in r["avisos"][0]
    assert "e 386.1340.706 d -20.0000" in p
    h = ops.planificar_taladro({"tipo": "holgura", "metrica": "M5", "ajuste": "medio", "cara": "+Y",
                                "punto": [30, 20, -10]}, CARAS, t, "IA_M5X_8_2")
    assert "e 386.1671.1673 i 15" in h["plan"] and "e 386.1671.1661 i 2" in h["plan"]
    assert "e 386.1671.1347 d 5.5000" in h["plan"] and "e 386.1671.1662 i 31" in h["plan"]
    assert ops.plan_valido(p) and ops.plan_valido(h["plan"])


@pytest.mark.parametrize("pet,texto", [
    ({"tipo": "recto", "diametro": 8, "cara": "+Y", "punto": [100, 20, 0]}, "fuera de la cara"),
    ({"tipo": "recto", "cara": "+Y", "punto": [0, 20, 0]}, "diametro"),
    ({"tipo": "holgura", "metrica": "M6", "profundidad": 10, "cara": "+Y", "punto": [0, 20, 0]}, "pasantes"),
    ({"tipo": "roscado", "metrica": "M6", "profundidad": 10, "rosca": 12, "cara": "+Y", "punto": [0, 20, 0]},
     "no más profunda"),
    ({"tipo": "recto", "diametro": 8, "cara": 999, "punto": [0, 20, 0]}, "no es una cara plana"),
    ({"tipo": "recto", "diametro": 8, "cara": "+Y", "punto": [0, 20, 0], "referencias": ["PLANTA", "ALZADO"]},
     "PLANTA"),
])
def test_plan_errores(tmp_path, pet, texto):
    with pytest.raises(ops.PlanError) as e:
        ops.planificar_taladro(pet, CARAS, tabla(tmp_path), "IA_X")
    assert texto in str(e.value)


def test_plan_valido_rechaza_rutas_y_ordenes():
    assert not ops.plan_valido("modelo ../X.PRT\n")
    assert not ops.plan_valido("modelo X.PRT\nguardar\n")
    assert not ops.plan_valido("e 386.1964 w ia_minusculas\n")
    assert not ops.plan_valido("")


def test_nombre_operacion_y_huella():
    assert ops.nombre_operacion("M2.5x.45", "143022_1") == "IA_M2_5X_45_143022_1"
    assert len(ops.nombre_operacion("X" * 40, "1")) == 31
    h = ops.huella(CARAS, [962], [2, 6])
    otra = json.loads(json.dumps(CARAS))
    otra["caras_planas"][1]["area"] = 9000           # el propio taladro cambia el área: no cuenta
    assert ops.huella(otra, [962], [2, 6]) == h
    otra["caras_planas"][1]["punto_plano"] = [0, 25, 0]   # la cara se ha movido
    assert ops.huella(otra, [962], [2, 6]) != h


def test_resumen_caras_y_seleccion():
    r = ops.resumir_caras(CARAS)
    assert r["modelo"] == "PRUEBA_CMD.PRT" and r["caras_planas"][0]["orientacion"] == "-Y"
    assert {p["nombre"]: p["orientacion"] for p in r["planos_referencia"]}["ALZADO"] == "+Z"
    sel = ops.resumir_seleccion({"seleccion": [
        {"tipo_elemento": 5, "id": 962, "modelo": "PRUEBA_CMD", "tipo_modelo": "PRT", "punto": [12.3456, 20, 7],
         "cara": CARAS["caras_planas"][1]},
        {"tipo_elemento": 66, "id": 40, "modelo": "PRUEBA_CMD", "tipo_modelo": "PRT", "coordenadas": [1, 2, 3]}]})
    assert sel[0]["tipo"] == "cara" and sel[0]["cara"]["orientacion"] == "+Y" and sel[0]["punto_pinchado"][0] == 12.346
    assert sel[1]["tipo"] == "punto" and sel[1]["coordenadas"] == [1, 2, 3]


# --------------------------------------------------------------- servidor

class CreoSimulado:
    """Hace de creo_iac_tk.exe: caras, selección, crear y borrar."""

    def __init__(self, crear=None):
        self.llamadas = []
        self.planes = []
        self.crear = crear or []
        self.caras = json.loads(json.dumps(CARAS))

    def __call__(self, args, **kw):
        self.llamadas.append(args)
        orden = args[1]
        if orden == "caras":
            r = self.caras
        elif orden == "seleccion":
            r = {"ok": True, "seleccion": [{"tipo_elemento": 5, "id": 962, "modelo": "PRUEBA_CMD", "tipo_modelo": "PRT",
                                            "punto": [10, 20, 5], "cara": CARAS["caras_planas"][1]}], "total": 1}
        elif orden == "crear":
            plan = Path(args[args.index("--plan") + 1]).read_text(encoding="ascii")
            self.planes.append(plan)
            r = self.crear.pop(0) if self.crear else None
            if isinstance(r, BaseException):
                raise r
            if r is None and ("e 386.387 i 913" in plan or "e 386.387 i 914" in plan):
                self.aristas_creadas = True
                r = {"ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"},
                     "operacion": {"id": 1600, "nombre": "IA_RED", "intentos": 1}, "guardado": False}
            if r is None and "\npatron " in plan:
                nombre = next(x for x in plan.splitlines() if x.startswith("e 604.1964 w ")).split()[-1]
                r = {"ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"},
                     "operacion": {"id": 1500, "nombre": nombre, "guia": 1323, "intentos": 2, "desviacion": 0.0,
                                   "miembros": [{"id": 1501}, {"id": 1502}]}, "guardado": False}
            if r is None:
                nombre = next(x for x in plan.splitlines() if x.startswith("e 386.1964 w ")).split()[-1]
                r = {"ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"},
                     "operacion": {"id": 1200 + len(self.planes), "nombre": nombre, "intentos": 1, "signos": [1, 1],
                                   "desviacion": 0.0}, "guardado": False}
        elif orden == "aristas":
            r = ARISTAS
            if getattr(self, "aristas_creadas", False) and not getattr(self, "redondeo_falla", False):
                r = {**ARISTAS, "aristas": [a for a in ARISTAS["aristas"] if a["id"] not in (2001, 2002)]}
        elif orden == "taladros":
            r = {"ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"}, "unidad_longitud": "mm", "modelos": [
                {"nombre": "PRUEBA_CMD", "tipo": "PRT", "instancias": 1, "operaciones": [
                    {"id": 1323, "nombre": "IA_M6X1_CB_115916_1", "clase": "taladro", "serie_rosca": "ISO",
                     "tamano_tornillo": "M6x1",
                     "elementos": {"386.387": 911, "386.1671.1672": 24, "386.1671.1673": 15, "386.1671.1347": 6.6,
                                   "386.1671.1662": 31, "386.1671.1675": 27, "386.1671.1349": 6.0,
                                   "386.1671.1350": 11.2, "386.1340.1341": "ref:5:962"},
                     "ejes": [[15, 0, 25, 15, 20, 25]]}]}]}
        elif orden == "arbol":
            if "--ids" in args:
                r = {"ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"},
                     "arboles": [{"id": 1323, "tipo": 911, "patron": 1, "elementos": {"386.387": 911}}]}
            else:
                r = {"ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"}, "operaciones": [
                    {"id": 1, "nombre": "PERFIL", "tipo": 923, "estado": 0, "patron": 0},
                    {"id": 1323, "nombre": "IA_M6X1_CB_115916_1", "tipo": 911, "estado": 0, "patron": 1}]}
        elif orden == "pieza_nueva":
            nombre = args[args.index("--modelo") + 1]
            self.pieza_nueva = (nombre, args[args.index("--plantilla") + 1])
            r = {"ok": True, "modelo": {"nombre": nombre[:-4], "tipo": "PRT"}, "plantilla_cargada": True,
                 "ventana": 3, "guardado": False}
        elif orden == "borrar":
            r = {"ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"},
                 "borradas": [int(x) for x in args[args.index("--ids") + 1].split(",")], "guardado": False}
        else:
            raise AssertionError(orden)
        return subprocess.CompletedProcess(args, 0, json.dumps(r).encode("utf-8"), b"")


def servidor(tmp_path, fake, creo, escritura=True):
    _, gw, _ = make(tmp_path, fake)
    hol = tmp_path / "hole"
    hol.mkdir(exist_ok=True)
    (hol / "ISO.hol").write_text(HOL, encoding="latin-1")
    s = Settings(creoson_host="127.0.0.1", creoson_port=fake.port, export_dir=None,
                 audit_log=tmp_path / "audit.jsonl", timeout_s=5, allow_operaciones=escritura, hol_dir=hol)
    gw = CreoGateway(s)
    exe = tmp_path / "creo_iac_tk.exe"
    exe.write_bytes(b"MZ")
    nat = nativo.Nativo(exe, tmp_path, gw.audit, gw._lock, runner=creo, escritura=escritura)
    return build_server(s, gw, nat)


TALADROS = [{"tipo": "recto", "diametro": 8, "cara": 962, "punto": [10, 20, 5]},
            {"tipo": "roscado", "metrica": "M6", "profundidad": 15, "rosca": 12, "cara": "+Y", "punto": [-20, 20, 10]}]


@pytest.mark.anyio
async def test_proponer_y_crear(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo)
    err, sel = await call(srv, "seleccion_creo")
    assert not err and sel["seleccion"][0]["cara"]["orientacion"] == "+Y"
    err, p = await call(srv, "proponer_taladros", taladros=TALADROS)
    assert not err, p
    assert p["crear_activo"] and len(p["codigo"]) == 8
    assert [t["tipo"] for t in p["taladros"]] == ["recto", "normalizado ISO M6x1 roscado"]
    assert p["taladros"][0]["nombre"].startswith("IA_D8_") and p["taladros"][1]["rosca"] == 12
    assert not creo.planes                                       # proponer no escribe
    err, r = await call(srv, "crear_taladros", codigo=p["codigo"].lower())
    assert not err, r
    assert r["estado"] == "ok" and r["ids_creados"] == [1201, 1202]
    assert "SIN GUARDAR" in r["aviso"] and len(creo.planes) == 2
    assert all(ops.plan_valido(x) for x in creo.planes)
    err, msg = await call(srv, "crear_taladros", codigo=p["codigo"])          # una sola vez
    assert err and "No hay ninguna propuesta" in msg
    err, b = await call(srv, "borrar_operaciones_ia", ids=[1201, 1202])
    assert not err and b["borradas"] == [1201, 1202]
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(a.get("event") == "operacion_creada" for a in audit) == 2


@pytest.mark.anyio
async def test_no_crea_si_la_pieza_cambia(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo)
    err, p = await call(srv, "proponer_taladros", taladros=TALADROS[:1])
    assert not err
    creo.caras["caras_planas"][1]["punto_plano"] = [0, 25, 0]
    err, msg = await call(srv, "crear_taladros", codigo=p["codigo"])
    assert err and "ha cambiado" in msg and not creo.planes


@pytest.mark.anyio
async def test_resultado_desconocido_bloquea(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado(crear=[subprocess.TimeoutExpired("x", 1)])
    srv = servidor(tmp_path, fake, creo)
    err, p = await call(srv, "proponer_taladros", taladros=TALADROS)
    err, r = await call(srv, "crear_taladros", codigo=p["codigo"])
    assert not err and r["estado"] == "resultado_desconocido"
    assert r["creados"] == [] and len(r["no_intentados"]) == 1 and len(creo.planes) == 1
    err, cap = await call(srv, "creo_capacidades")
    assert cap["resultado_desconocido"] and cap["escritura_en_creo"]["taladros_en_piezas"] is True
    err, p2 = await call(srv, "proponer_taladros", taladros=TALADROS[:1])
    assert not err and "resultado_desconocido_anterior" in p2     # se avisa al volver a proponer


@pytest.mark.anyio
async def test_fallo_de_creo_sigue_con_los_demas(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    malo = {"ok": False, "fase": "verificacion", "codigo": 12, "error": "La operacion no quedaba donde se pedia"}
    creo = CreoSimulado(crear=[malo])
    srv = servidor(tmp_path, fake, creo)
    err, p = await call(srv, "proponer_taladros", taladros=TALADROS)
    err, r = await call(srv, "crear_taladros", codigo=p["codigo"])
    assert not err and r["estado"] == "con_incidencias"
    assert len(r["fallidos"]) == 1 and "no quedaba" in r["fallidos"][0]["error"] and r["ids_creados"] == [1202]


@pytest.mark.anyio
async def test_sin_escritura_solo_propone(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT", "TOP.ASM"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo, escritura=False)
    nombres = {t.name for t in srv._tool_manager.list_tools()}
    assert {"caras_creo", "seleccion_creo", "proponer_taladros"} <= nombres
    assert not {"crear_taladros", "borrar_operaciones_ia", "crear_patron", "crear_redondeos",
                "crear_chaflanes", "crear_extrusion", "crear_revolucion", "crear_pieza"} & nombres
    err, p = await call(srv, "proponer_taladros", taladros=TALADROS[:1])
    assert not err and p["crear_activo"] is False and "desactivada" in p["siguiente"]
    err, msg = await call(srv, "caras_creo", modelo="TOP.ASM")
    assert err and "no es una pieza" in msg


def test_nativo_rechaza_escritura_sin_permiso(tmp_path):
    import threading

    class A:
        def write(self, **k):
            pass

    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    n = nativo.Nativo(exe, tmp_path, A(), threading.Lock(), runner=CreoSimulado())
    with pytest.raises(nativo.NativoError, match="desactivada"):
        n.ejecutar("crear", plan="modelo X.PRT\n")
    n.escritura = True
    with pytest.raises(nativo.NativoError, match="no válido"):
        n.ejecutar("crear", plan="modelo X.PRT\nguardar\n")
    with pytest.raises(nativo.NativoError, match="no válidos"):
        n.ejecutar("borrar", ids=[0])


def test_servicio_escritura_y_plan(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
    import servicio_tk

    monkeypatch.setattr(servicio_tk, "PERMITE_ESCRITURA", False)
    codigo, r = servicio_tk.ejecutar({"orden": "crear", "plan": "modelo X.PRT\n"})
    assert codigo == 403 and "--escritura" in r["error"]
    monkeypatch.setattr(servicio_tk, "PERMITE_ESCRITURA", True)
    codigo, r = servicio_tk.ejecutar({"orden": "crear", "plan": "modelo ../X.PRT\n"})
    assert codigo == 400
    codigo, r = servicio_tk.ejecutar({"orden": "borrar", "ids": ["1;del"]})
    assert codigo == 400
    assert servicio_tk.PLAN_RE.pattern == ops.PLAN_RE.pattern


# --------------------------------------------------------------- cajeado y avellanado

def test_cajeado_y_avellanado(tmp_path):
    t = tabla(tmp_path)
    assert t["M6X1"].cajeado == (11.2, 6.0) and t["M6X1"].avellanado == (13.44, 90.0)
    cb = ops.planificar_taladro({"tipo": "holgura", "metrica": "M6", "cara": "+Y", "punto": [0, 20, 0],
                                 "cajeado": True}, CARAS, t, "IA_M6X1_CB_1")
    p = cb["plan"].splitlines()
    assert "e 386.1671.1675 i 27" in p and "e 386.1671.1676 i -1" in p
    assert p.index("e 386.1671.1349 d 6.0000") < p.index("e 386.1671.1350 d 11.2000")
    assert p.index("e 386.1671.1662 i 31") < p.index("e 386.1671.1349 d 6.0000")
    assert cb["resumen"]["cajeado"] == {"diametro": 11.2, "profundidad": 6.0} and cb["base_nombre"] == "M6x1_CB"
    cs = ops.planificar_taladro({"tipo": "holgura", "metrica": "M5", "cara": "+Y", "punto": [0, 20, 0],
                                 "avellanado": True, "avellanado_diametro": 10.4}, CARAS, t, "IA_X")
    p = cs["plan"].splitlines()
    assert "e 386.1671.1676 i 28" in p and "e 386.1671.1352 d 90.0000" in p and "e 386.1671.1351 d 10.4000" in p
    assert p.index("e 386.1671.1352 d 90.0000") < p.index("e 386.1671.1351 d 10.4000")
    assert ops.plan_valido(cb["plan"]) and ops.plan_valido(cs["plan"])


@pytest.mark.parametrize("pet,texto", [
    ({"tipo": "recto", "diametro": 6.6, "cajeado": True}, "normalizados"),
    ({"tipo": "holgura", "metrica": "M6", "cajeado": True, "avellanado": True}, "no los dos"),
    ({"tipo": "holgura", "metrica": "M6", "cajeado": True, "cajeado_diametro": 6}, "mayor que el taladro"),
    ({"tipo": "taladrado", "metrica": "M6", "profundidad": 5, "cajeado": True}, "menos profundo"),
    ({"tipo": "holgura", "metrica": "M2.5", "avellanado": True}, "no da avellanado"),
])
def test_rebajes_errores(tmp_path, pet, texto):
    pet = {"cara": "+Y", "punto": [0, 20, 0], **pet}
    with pytest.raises(ops.PlanError) as e:
        ops.planificar_taladro(pet, CARAS, tabla(tmp_path), "IA_X")
    assert texto in str(e.value)


@pytest.mark.anyio
async def test_operaciones_creo(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo, escritura=False)
    err, r = await call(srv, "operaciones_creo")
    assert not err, r
    assert r["operaciones"][1] == {"id": 1323, "nombre": "IA_M6X1_CB_115916_1", "tipo": "taladro", "activa": True,
                                   "patron": "guía de patrón"}
    err, r = await call(srv, "operaciones_creo", ids=[1323])
    assert not err and r["arboles"][0]["elementos"] == {"386.387": 911}
    assert creo.llamadas[-1][-2:] == ["--ids", "1323"]


# --------------------------------------------------------------- patrones

CARAS_EJES = {**CARAS, "ejes_referencia": [
    {"nombre": "X", "id": 24, "id_operacion": 21, "punto": [0, 0, 0], "direccion": [1, 0, 0]},
    {"nombre": "Z", "id": 28, "id_operacion": 25, "punto": [0, 0, 0], "direccion": [0, 0, 1]},
    {"nombre": "Y", "id": 32, "id_operacion": 29, "punto": [0, 0, 0], "direccion": [0, 1, 0]}]}
GUIA_CB = {"id": 1323, "nombre": "IA_M6X1_CB_115916_1", "clase": "taladro", "diametro": 6.6, "cara": 962,
           "avellanado_cilindrico": {"diametro": 11.2, "profundidad": 6},
           "eje": {"punto": [15, 0, 25], "direccion": [0, 1, 0]}}
GUIA_CS = {"id": 1391, "nombre": "IA_M5X_8_CS_115916_2", "clase": "taladro", "diametro": 5.5, "cara": 962,
           "avellanado_conico": {"diametro": 11.2, "angulo": 90}, "eje": {"punto": [-10, 0, 5], "direccion": [0, 1, 0]}}
OTRO = {"id": 1009, "clase": "taladro", "diametro": 5, "eje": {"punto": [30.21, 5, -29.94], "direccion": [0, 1, 0]}}


def test_patron_lineal_y_fuera_de_pieza():
    r = ops.planificar_patron({"tipo": "lineal", "direccion1": "+X", "separacion1": 15, "cantidad1": 3,
                               "direccion2": "-Z", "separacion2": 45, "cantidad2": 2},
                              CARAS_EJES, GUIA_CB, [OTRO], "IA_PAT_1")
    p = r["plan"].splitlines()
    assert p[1] == "patron 1323" and "e 604.3124 i 5" in p
    assert "e 604.3129.3291.3252.3253 r 21 24" in p and "e 604.3129.3292.3252.3253 r 21 28" in p
    assert "e 604.3129.3274 d 15.0000" in p and "e 604.3129.3275 d 45.0000" in p
    assert "e 604.3129.3134 i 3" in p and "e 604.3129.3136 i 2" in p
    assert sum(x.startswith("esperado") for x in p) == 6 and "esperado 45.000000 20.000000 -20.000000" in p
    assert ops.plan_valido(r["plan"]) and r["resumen"]["miembros"] == 6
    # lo que pasó a mano (2.ª fila hacia +Z, a z = 70): no se propone
    with pytest.raises(ops.PlanError, match="fuera de la pieza"):
        ops.planificar_patron({"tipo": "lineal", "direccion1": "+X", "separacion1": 15, "cantidad1": 3,
                               "direccion2": "+Z", "separacion2": 45, "cantidad2": 2}, CARAS_EJES, GUIA_CB, [], "X")


def test_patron_circular_y_solapes():
    r = ops.planificar_patron({"tipo": "circular", "eje": "Y", "cantidad": 3}, CARAS_EJES, GUIA_CS, [], "IA_PAT_2")
    pos = r["resumen"]["posiciones"]
    assert any(abs(q[0] - 0.6699) < 1e-3 and abs(q[2] + 11.1603) < 1e-3 for q in pos)   # lo que hizo Creo a mano
    assert any(abs(q[0] - 9.3301) < 1e-3 and abs(q[2] - 6.1603) < 1e-3 for q in pos)
    assert "e 604.3130.3273 r 21 32" in r["plan"] and "e 604.3130.3276 d 120.0000" in r["plan"]
    assert ops.plan_valido(r["plan"])
    with pytest.raises(ops.PlanError, match="se pisan"):
        ops.planificar_patron({"tipo": "lineal", "direccion1": "+X", "separacion1": 8, "cantidad1": 2},
                              CARAS_EJES, GUIA_CB, [], "X")
    a = ops.planificar_patron({"tipo": "lineal", "direccion1": "-Z", "separacion1": 25, "cantidad1": 2},
                              CARAS_EJES, {**GUIA_CB, "eje": {"punto": [30, 0, -5], "direccion": [0, 1, 0]}}, [OTRO], "X")
    assert any("se pisa con el taladro 1009" in x for x in a["avisos"])
    with pytest.raises(ops.PlanError, match="eje de referencia"):
        ops.planificar_patron({"tipo": "circular", "eje": "A_99", "cantidad": 3}, CARAS_EJES, GUIA_CS, [], "X")


@pytest.mark.anyio
async def test_proponer_y_crear_patron(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    creo.caras = json.loads(json.dumps(CARAS_EJES))
    srv = servidor(tmp_path, fake, creo)
    err, p = await call(srv, "proponer_patron", guia=1323, tipo="lineal", direccion1="+X", separacion1=15,
                        cantidad1=3, direccion2="-Z", separacion2=45, cantidad2=2)
    assert not err, p
    assert p["codigo"].startswith("P") and p["miembros"] == 6 and p["nombre"].startswith("IA_PAT_")
    assert not creo.planes
    err, msg = await call(srv, "crear_taladros", codigo=p["codigo"])          # código de otra herramienta
    assert err and "No hay ninguna propuesta" in msg
    err, r = await call(srv, "crear_patron", codigo=p["codigo"])
    assert not err, r
    assert r["estado"] == "ok" and r["patron"]["id"] == 1500 and "SIN GUARDAR" in r["aviso"]
    assert "patron 1323" in creo.planes[0] and ops.plan_valido(creo.planes[0])
    err, msg = await call(srv, "proponer_patron", guia=1323, tipo="lineal", direccion1="+X", separacion1=15,
                          cantidad1=3, direccion2="+Z", separacion2=45, cantidad2=2)
    assert err and "fuera de la pieza" in msg


# --------------------------------------------------------------- redondeos

ARISTAS = {"ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"}, "unidad_longitud": "mm", "total": 3,
           "aristas": [
               {"id": 2001, "tipo": "recta", "longitud": 8, "inicio": [10, 12, 10], "fin": [10, 20, 10],
                "medio": [10, 16, 10], "tangente": [0, 1, 0], "caras": [3001, 3002], "forma": "concava"},
               {"id": 2002, "tipo": "recta", "longitud": 120, "inicio": [-60, 20, 40], "fin": [60, 20, 40],
                "medio": [0, 20, 40], "tangente": [1, 0, 0], "caras": [962, 971], "forma": "convexa"},
               {"id": 2003, "tipo": "arco", "longitud": 20.7, "medio": [15, 20, 28.3], "caras": [962, 4000],
                "forma": "tangente"}]}


def test_resumen_y_plan_de_redondeo():
    r = ops.resumir_aristas(ARISTAS, forma="concava")
    assert [a["id"] for a in r["aristas"]] == [2001] and r["aristas"][0]["direccion"] == "Y"
    assert [a["id"] for a in ops.resumir_aristas(ARISTAS, forma="concava", direccion="Y")["aristas"]] == [2001]
    assert ops.resumir_aristas(ARISTAS, forma="concava", direccion="X")["aristas"] == []
    pl = ops.planificar_redondeo({"aristas": [2001], "radio": 3}, ARISTAS, "IA_RED_R3_1")
    p = pl["plan"].splitlines()
    assert "e 386.387 i 913" in p and "e 386.2101.2102.2121.2124 k 2001" in p
    assert "e 386.2101.2102.2123.2122.2106.2109 d 3.0000" in p and not any(x.startswith("sin_aristas") for x in p)
    assert ops.plan_valido(pl["plan"])
    assert ops.planificar_redondeo({"aristas": [2001], "radio": 10}, ARISTAS, "X")["avisos"]
    for pet, txt in [({"aristas": [9], "radio": 2}, "no existe"), ({"aristas": [2003], "radio": 2}, "tangentes"),
                     ({"aristas": [2001], "radio": 0}, "radio")]:
        with pytest.raises(ops.PlanError, match=txt):
            ops.planificar_redondeo(pet, ARISTAS, "X")


@pytest.mark.anyio
async def test_proponer_y_crear_redondeo(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo)
    err, a = await call(srv, "aristas_creo", forma="concava")
    assert not err and a["aristas"][0]["id"] == 2001
    err, p = await call(srv, "proponer_redondeos", aristas=[2001, 2002], radio=2)
    assert not err, p
    assert p["codigo"].startswith("R") and p["formas"] == ["concava", "convexa"] and p["nombre"].startswith("IA_RED_R2_")
    err, r = await call(srv, "crear_redondeos", codigo=p["codigo"])
    assert not err and r["estado"] == "ok" and r["redondeo"]["id"] == 1600
    assert "k 2001 2002" in creo.planes[0] and ops.plan_valido(creo.planes[0]) and r["aristas_comprobadas"]
    # si las aristas siguen, se borra el redondeo
    creo.redondeo_falla = True
    creo.aristas_creadas = False
    err, p = await call(srv, "proponer_redondeos", aristas=[2001], radio=2)
    err, msg = await call(srv, "crear_redondeos", codigo=p["codigo"])
    assert err and "siguen en la pieza" in msg and "se ha borrado" in msg
    assert creo.llamadas[-1][1] == "borrar"


def test_plan_de_chaflan():
    pl = ops.planificar_chaflan({"aristas": [2002], "distancia": 1.5}, ARISTAS, "IA_CHF_1_5_1")
    p = pl["plan"].splitlines()
    assert "e 386.387 i 914" in p and "e 386.2101.2102.2103 i 601" in p and "e 386.2101.2102.3184 i 1" in p
    assert "e 386.2101.2102.2121.2124 k 2002" in p and "e 386.2101.2102.2123.2122.2106.2109 d 1.5000" in p
    assert ops.plan_valido(pl["plan"]) and not pl["avisos"] and pl["resumen"]["tipo"] == "chaflan"
    assert "radio" not in pl["resumen"] and pl["resumen"]["distancia"] == 1.5
    dxd = ops.planificar_chaflan({"aristas": [2001], "distancia": 1, "esquema": "DxD"}, ARISTAS, "X")
    assert "e 386.2101.2102.2103 i 602" in dxd["plan"] and any("interiores" in a for a in dxd["avisos"])
    for pet, txt in [({"aristas": [2002], "distancia": 0}, "distancia"),
                     ({"aristas": [2002], "distancia": 1, "esquema": "D1xD2"}, "esquema"),
                     ({"aristas": [2003], "distancia": 1}, "tangentes")]:
        with pytest.raises(ops.PlanError, match=txt):
            ops.planificar_chaflan(pet, ARISTAS, "X")


@pytest.mark.anyio
async def test_proponer_y_crear_chaflan(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo)
    err, p = await call(srv, "proponer_chaflanes", aristas=[2002], distancia=1)
    assert not err, p
    assert p["codigo"].startswith("C") and p["nombre"].startswith("IA_CHF_1_") and p["esquema"] == "45xD"
    # un código de chaflán no vale para crear_redondeos (y no se consume)
    err, msg = await call(srv, "crear_redondeos", codigo=p["codigo"])
    assert err and "redondeo" in msg
    err, r = await call(srv, "crear_chaflanes", codigo=p["codigo"])
    assert not err and r["estado"] == "ok" and r["chaflan"]["id"] == 1600 and r["aristas_comprobadas"]
    assert "e 386.387 i 914" in creo.planes[0] and "k 2002" in creo.planes[0]
    err, msg = await call(srv, "crear_chaflanes", codigo=p["codigo"])
    assert err and "proponer_chaflanes" in msg


def test_forma_invertida_en_tk_050():
    viejo = {**ARISTAS, "version_tk": "0.5.0"}
    assert [a["id"] for a in ops.resumir_aristas(viejo, forma="concava")["aristas"]] == [2002]
