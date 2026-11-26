"""Extrusiones y cortes con croquis: planificación (sin Creo) y flujo proponer -> crear."""
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_operaciones import CARAS, CreoSimulado, servidor  # noqa: E402
from test_server import call, fake  # noqa: E402,F401

from creo_mcp_iac.extrusiones import planificar_extrusion  # noqa: E402
from creo_mcp_iac.operaciones import PlanError, plan_valido  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


VACIA = {"modelo": {"nombre": "NUEVA"}, "unidad_longitud": "mm", "cuerpo_por_defecto": None, "caras_planas": [],
         "planos_referencia": [{"nombre": "RIGHT", "id": 1, "normal": [1, 0, 0], "origen": [0, 0, 0]},
                               {"nombre": "TOP", "id": 3, "normal": [0, 1, 0], "origen": [0, 0, 0]},
                               {"nombre": "FRONT", "id": 5, "normal": [0, 0, 1], "origen": [0, 0, 0]}]}


def lineas(plan, prefijo):
    return [x for x in plan.splitlines() if x.startswith(prefijo)]


def test_placa_con_huecos_area_y_plan():
    pet = {"tipo": "anadir", "plano": "FRONT", "profundidad": 10, "sentido": "+Z", "contornos": [
        {"tipo": "rectangulo", "centro": [0, 0], "ancho": 100, "alto": 60, "radio_esquinas": 5},
        {"tipo": "circulo", "centro": [30, 0], "diametro": 10},
        {"tipo": "ranura", "centro1": [-30, -10], "centro2": [-30, 10], "ancho": 8},
        {"tipo": "perfil", "puntos": [[0, -20], [10, -20], {"hasta": [10, 0], "centro": [10, -10], "giro": "antihorario"}, [0, 0]]}]}
    pl = planificar_extrusion(pet, VACIA, "IA_EXT_1")
    esperado = 100 * 60 - (4 - math.pi) * 25 - math.pi * 25 - (8 * 20 + math.pi * 16) - (200 + math.pi * 50)
    assert pl["resumen"]["area_neta_mm2"] == pytest.approx(esperado, abs=1e-3)
    assert [c["hueco"] for c in pl["resumen"]["contornos"]] == [False, True, True, True]
    p = pl["plan"]
    assert plan_valido(p)
    assert "e 386.387 i 917" in p and "e 386.2023 i -1" in p
    assert "e 386.515.429.433 i 262144" in p and "e 386.515.429.434 d 10.0000" in p
    assert "e 386.447.450.451 r 5 5" in p                      # croquis en FRONT
    assert lineas(p, "sk_ref") == ["sk_ref 5 1", "sk_ref 5 3"]
    assert len(lineas(p, "sk_circulo")) == 1 and len(lineas(p, "sk_arco")) == 4 + 2 + 1
    assert "rango Z 0.000000 10.000000" in p and "probar_sentidos" in p and "sentido 1" in p
    assert float(lineas(p, "volumen_max")[0].split()[1]) == pytest.approx(esperado * 10, rel=1e-6)
    # todos los puntos del croquis están en el plano z = 0
    for x in lineas(p, "sk_linea") + lineas(p, "sk_arco"):
        v = [float(t) for t in x.split()[1:]]
        assert all(abs(v[i]) < 1e-9 for i in range(2, len(v), 3))


def test_corte_pasante_en_cara_y_sentido_negativo():
    pet = {"tipo": "quitar", "plano": 962, "extension": "pasante", "sentido": "-Y",
           "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 20}]}
    pl = planificar_extrusion(pet, CARAS, "IA_CORTE_1")
    p = pl["plan"]
    assert "e 386.387 i 916" in p and "e 386.2023 i 916" in p and "e 386.515.429.433 i 1048576" in p
    assert "sk_circulo 0.000000 20.000000 0.000000 10.000000" in p      # (x, z) en el plano y = 20
    assert "rango Y -1000000000.000000 20.000000" in p and "volumen_signo -1" in p and "sentido -1" in p
    assert "e 386.6314.6316 r 98 -5778" in p
    assert pl["ids_caras"] == [962]


def test_simetrica_y_pasante_ambos():
    pl = planificar_extrusion({"tipo": "anadir", "plano": "TOP", "extension": "simetrica", "profundidad": 8,
                               "contornos": [{"tipo": "rectangulo", "esquina": [0, 0], "ancho": 10, "alto": 5}]}, VACIA, "N")
    assert "rango Y -4.000000 4.000000" in pl["plan"] and "sentido" not in pl["plan"]
    assert "e 386.515.429.433 i 268435456" in pl["plan"]
    pl = planificar_extrusion({"tipo": "quitar", "plano": "PLANTA", "extension": "pasante_ambos",
                               "contornos": [{"tipo": "circulo", "centro": [0, 0], "radio": 3}]}, CARAS, "N")
    assert "e 386.515.428.430 i 512" in pl["plan"] and "rango" not in pl["plan"]


def test_arco_por_tres_puntos():
    # media luna: semicírculo de radio 10 cerrado por el diámetro
    pl = planificar_extrusion({"tipo": "anadir", "plano": "FRONT", "profundidad": 1, "sentido": "-Z",
                               "contornos": [{"tipo": "perfil", "puntos": [[-10, 0], [10, 0], {"hasta": [-10, 0], "por": [0, 10]}]}]},
                              VACIA, "N")
    assert pl["resumen"]["area_neta_mm2"] == pytest.approx(math.pi * 50, abs=1e-3)
    assert "rango Z -1.000000 0.000000" in pl["plan"]


@pytest.mark.parametrize("pet,msg", [
    ({"tipo": "anadir", "plano": "FRONT", "extension": "pasante", "sentido": "+Z",
      "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 5}]}, "profundidad"),
    ({"tipo": "anadir", "plano": "FRONT", "profundidad": 5, "sentido": "+X",
      "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 5}]}, "sentido"),
    ({"tipo": "anadir", "plano": "LATERAL", "profundidad": 5, "sentido": "+Z",
      "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 5}]}, "plano de referencia"),
    ({"tipo": "quitar", "plano": "FRONT", "profundidad": 5, "sentido": "+Z",
      "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 5}]}, "no tiene material"),
    ({"tipo": "anadir", "plano": "FRONT", "profundidad": 5, "sentido": "+Z",
      "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 10}, {"tipo": "circulo", "centro": [6, 0], "diametro": 10}]},
     "se cortan"),
    ({"tipo": "anadir", "plano": "FRONT", "profundidad": 5, "sentido": "+Z",
      "contornos": [{"tipo": "perfil", "puntos": [[0, 0], [10, 10], [10, 0], [0, 10]]}]}, "sí mismo"),
    ({"tipo": "anadir", "plano": "FRONT", "profundidad": 5, "sentido": "+Z",
      "contornos": [{"tipo": "perfil", "puntos": [[0, 0], [10, 0], {"hasta": [0, 5], "centro": [5, 5], "giro": "horario"}]}]},
     "misma distancia"),
    ({"tipo": "anadir", "plano": "FRONT", "profundidad": 5, "sentido": "+Z",
      "contornos": [{"tipo": "rectangulo", "centro": [0, 0], "ancho": 10, "alto": 4, "radio_esquinas": 3}]}, "radio_esquinas"),
])
def test_errores(pet, msg):
    with pytest.raises(PlanError, match=msg):
        planificar_extrusion(pet, VACIA, "N")


def test_plano_oblicuo_rechazado():
    caras = json.loads(json.dumps(VACIA))
    caras["planos_referencia"].append({"nombre": "OBLICUO", "id": 9, "normal": [0.6, 0.8, 0], "origen": [0, 0, 0]})
    with pytest.raises(PlanError, match="cara plana"):
        planificar_extrusion({"tipo": "anadir", "plano": "OBLICUO", "profundidad": 5, "sentido": "+Z",
                              "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 5}]}, caras, "N")


@pytest.mark.anyio
async def test_proponer_y_crear_corte(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo)
    err, p = await call(srv, "proponer_extrusion", tipo="quitar", plano="962", extension="ciega", profundidad=5,
                        sentido="-Y", contornos=[{"tipo": "rectangulo", "centro": [0, 0], "ancho": 30, "alto": 20}])
    assert not err, p
    assert p["codigo"].startswith("X") and p["nombre"].startswith("IA_CORTE_") and p["volumen_teorico_mm3"] == 3000
    err, r = await call(srv, "crear_extrusion", codigo=p["codigo"])
    assert not err and r["estado"] == "ok", r
    assert "sk_linea" in creo.planes[0] and "rango Y 15.000000 20.000000" in creo.planes[0]
    err, msg = await call(srv, "crear_extrusion", codigo=p["codigo"])
    assert err and "proponer_extrusion" in msg


@pytest.mark.anyio
async def test_crear_extrusion_pieza_cambiada(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo)
    err, p = await call(srv, "proponer_extrusion", tipo="anadir", plano="962", profundidad=5, sentido="+Y",
                        contornos=[{"tipo": "circulo", "centro": [0, 0], "diametro": 20}])
    assert not err, p
    creo.caras["caras_planas"][1]["punto_plano"] = [0, 25, 0]
    err, msg = await call(srv, "crear_extrusion", codigo=p["codigo"])
    assert err and "ha cambiado" in msg and creo.planes == []


# --- cotas del boceto (0.18.0) ----------------------------------------------------------------

HULL = {"tipo": "perfil", "puntos": [[36.6, -6.81469], {"hasta": [36.6, 6.81469], "por": [42, 0]}, [3.428571, 14.602907],
                                     {"hasta": [-3.428571, 14.602907], "por": [0, 15]}, [-36.6, 6.81469],
                                     {"hasta": [-36.6, -6.81469], "por": [-42, 0]}, [-3.428571, -14.602907],
                                     {"hasta": [3.428571, -14.602907], "por": [0, -15]}]}
VACIA_IAC = {"modelo": {"nombre": "NUEVA"}, "caras_planas": [], "cuerpo_por_defecto": None,
             "planos_referencia": [{"nombre": "PERFIL", "id": 2, "normal": [1, 0, 0], "origen": [0, 0, 0]},
                                   {"nombre": "PLANTA", "id": 4, "normal": [0, 1, 0], "origen": [0, 0, 0]},
                                   {"nombre": "ALZADO", "id": 6, "normal": [0, 0, 1], "origen": [0, 0, 0]}]}


def _entidades(plan):
    return [x.split()[0] for x in plan.splitlines() if x.startswith(("sk_linea", "sk_arco", "sk_circulo"))]


def test_cotas_de_la_brida_como_el_dibujo():
    pl = planificar_extrusion({"tipo": "anadir", "plano": "PLANTA", "profundidad": 10, "sentido": "+Y",
                               "contornos": [HULL, {"tipo": "circulo", "centro": [-35, 0], "diametro": 6},
                                             {"tipo": "circulo", "centro": [35, 0], "diametro": 6}]}, VACIA_IAC, "N")
    # R7, R15 y Ø6 una vez (Creo pone radios iguales y concéntricos); centros de los lóbulos a 35 de PERFIL;
    # sin ángulos ni puntos de tangencia
    assert pl["resumen"]["cotas_boceto"] == ["R7", "centro a 35 de PERFIL", "R15", "centro a 35 de PERFIL", "Ø6"]
    cotas = lineas(pl["plan"], "sk_cota")
    ents = _entidades(pl["plan"])
    for c in cotas:
        t = c.split()
        if t[1] == "diam":
            assert ents[int(t[2])] == "sk_circulo"
        if t[1] == "radio":
            assert ents[int(t[2])] == "sk_arco"
    assert plan_valido(pl["plan"])


def test_cotas_de_rectangulo_ranura_y_perfil():
    pl = planificar_extrusion({"tipo": "anadir", "plano": "ALZADO", "profundidad": 10, "sentido": "+Z", "contornos": [
        {"tipo": "rectangulo", "centro": [0, 0], "ancho": 100, "alto": 60, "radio_esquinas": 5},
        {"tipo": "ranura", "centro1": [-30, -10], "centro2": [-30, 10], "ancho": 8}]}, VACIA_IAC, "N")
    assert pl["resumen"]["cotas_boceto"] == ["R5", "100 entre lados", "60 entre lados", "lado a 50 de PERFIL",
                                             "lado a 30 de PLANTA", "R4", "centro a 30 de PERFIL",
                                             "centro a 10 de PLANTA", "centro a 10 de PLANTA"]
    assert plan_valido(pl["plan"])
    # polígono: vértices que no están en una referencia, cada coordenada una vez
    pl = planificar_extrusion({"tipo": "anadir", "plano": "ALZADO", "profundidad": 5, "sentido": "+Z", "contornos": [
        {"tipo": "perfil", "puntos": [[0, 0], [40, 0], [40, 10], [15, 25], [0, 25]]}]}, VACIA_IAC, "N")
    assert pl["resumen"]["cotas_boceto"] == ["vértice a 40 de PERFIL", "vértice a 10 de PLANTA",
                                             "vértice a 15 de PERFIL", "vértice a 25 de PLANTA"]


def test_arco_por_tres_puntos_redondea_centro_y_radio():
    from creo_mcp_iac.extrusiones import _arco_3p
    _, c, r, _, _ = _arco_3p((3.428571, 14.602907), (0, 15), (-3.428571, 14.602907), "x")
    assert c == (0.0, 0.0) and r == 15.0


# --- revoluciones (0.19.0) ------------------------------------------------------------------------

from creo_mcp_iac.extrusiones import planificar_revolucion  # noqa: E402

EJE_ESCALONADO = {"tipo": "perfil", "puntos": [[0, 0], [10, 0], [10, 30], [6, 30], [6, 50], [0, 50]]}


def test_revolucion_eje_escalonado():
    pl = planificar_revolucion({"tipo": "anadir", "plano": "ALZADO", "eje": "Y", "contornos": [EJE_ESCALONADO]},
                               VACIA_IAC, "IA_REV_1")
    p = pl["plan"]
    assert plan_valido(p)
    assert "e 386.388 i 2" in p and "e 386.3280 i 1" in p           # PRO_REVOLVE, eje interno
    assert "e 386.6.438.440 i 4194304" in p and "e 386.6.438.442 d 360.0000" in p
    ents = _entidades(p) + [x.split()[0] for x in p.splitlines() if x.startswith("sk_eje")]
    assert ents[-1] == "sk_eje" and len(ents) == 7
    assert "sk_eje 0.000000 -10.000000 0.000000 0.000000 60.000000 0.000000" in p
    # Ø20 y Ø12 respecto al eje (como en un plano de torno) y las longitudes desde PLANTA
    assert pl["resumen"]["cotas_boceto"] == ["Ø20 (vértice, al eje)", "vértice a 30 de PLANTA",
                                             "Ø12 (vértice, al eje)", "vértice a 50 de PLANTA"]
    assert all(" 6 " in c for c in lineas(p, "sk_cota diaeje"))     # referencia: la línea de centros (entidad 6)
    vol = math.pi * 10 ** 2 * 30 + math.pi * 6 ** 2 * 20
    assert pl["resumen"]["volumen_teorico_mm3"] == pytest.approx(vol, abs=1e-3)
    assert "rango" not in p and f"volumen_max {vol:.6f}" in p


def test_revolucion_casquillo_con_arcos_y_parcial():
    # casquillo Ø30/Ø20 × 10 con un chaflán redondeado R2 en la arista exterior superior, girado 90°
    pl = planificar_revolucion({"tipo": "anadir", "plano": "ALZADO", "eje": "Y", "angulo": 90, "contornos": [
        {"tipo": "perfil", "puntos": [[10, 0], [15, 0], [15, 8], {"hasta": [13, 10], "centro": [13, 8], "giro": "antihorario"},
                                      [10, 10]]}]}, VACIA_IAC, "N")
    area = 5 * 10 - (4 - math.pi)                                     # rectángulo menos la esquina redondeada
    assert pl["resumen"]["area_neta_mm2"] == pytest.approx(area, abs=1e-3)
    # Pappus con ángulo de 90°: el volumen es una cuarta parte del de la vuelta entera
    entero = planificar_revolucion({"tipo": "anadir", "plano": "ALZADO", "eje": "Y", "contornos": [
        {"tipo": "perfil", "puntos": [[10, 0], [15, 0], [15, 8], {"hasta": [13, 10], "centro": [13, 8], "giro": "antihorario"},
                                      [10, 10]]}]}, VACIA_IAC, "N")["resumen"]["volumen_teorico_mm3"]
    assert pl["resumen"]["volumen_teorico_mm3"] == pytest.approx(entero / 4, rel=1e-6)
    assert "e 386.6.438.440 i 536870912" in pl["plan"] and "e 386.6.438.442 d 90.0000" in pl["plan"]
    assert "Ø20 (vértice, al eje)" in pl["resumen"]["cotas_boceto"] and "R2" in pl["resumen"]["cotas_boceto"]


@pytest.mark.parametrize("pet,msg", [
    ({"tipo": "anadir", "plano": "ALZADO", "eje": "Z", "contornos": [EJE_ESCALONADO]}, "eje"),
    ({"tipo": "anadir", "plano": "ALZADO", "eje": "Y",
      "contornos": [{"tipo": "rectangulo", "centro": [0, 10], "ancho": 20, "alto": 10}]}, "cruza la línea de centros"),
    ({"tipo": "anadir", "plano": "ALZADO", "eje": "Y", "angulo": 400, "contornos": [EJE_ESCALONADO]}, "angulo"),
    ({"tipo": "quitar", "plano": "ALZADO", "eje": "Y", "contornos": [EJE_ESCALONADO]}, "no tiene material"),
])
def test_revolucion_errores(pet, msg):
    with pytest.raises(PlanError, match=msg):
        planificar_revolucion(pet, VACIA_IAC, "N")


@pytest.mark.anyio
async def test_proponer_y_crear_revolucion(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo)
    # garganta en el bloque de prueba (Y de 0 a 20): corte por revolución alrededor de Y, en ALZADO
    err, p = await call(srv, "proponer_revolucion", tipo="quitar", plano="ALZADO", eje="Y",
                        contornos=[{"tipo": "rectangulo", "esquina": [50, 5], "ancho": 10, "alto": 4}])
    assert not err, p
    assert p["codigo"].startswith("R") and p["nombre"].startswith("IA_CORTE_REV_")
    # un código de revolución no vale para crear_extrusion (y no se consume)
    err, msg = await call(srv, "crear_extrusion", codigo=p["codigo"])
    assert err and "extrusión" in msg
    err, r = await call(srv, "crear_revolucion", codigo=p["codigo"])
    assert not err and r["estado"] == "ok", r
    assert "e 386.388 i 2" in creo.planes[0] and "sk_eje" in creo.planes[0]


# --- pieza nueva desde la plantilla (0.20.0) ---------------------------------------------------

@pytest.mark.anyio
async def test_crear_pieza_con_la_plantilla_de_creo(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    srv = servidor(tmp_path, fake, creo)
    err, r = await call(srv, "crear_pieza", nombre="soporte_prueba")
    assert not err and r["estado"] == "ok" and r["modelo"] == "SOPORTE_PRUEBA.PRT", r
    assert creo.pieza_nueva == ("SOPORTE_PRUEBA.PRT", "wtpub://Mistral/Bibliotecas/FORMATOS/plantilla_pieza_generica.prt")
    assert "SIN GUARDAR" in r["aviso"]
    err, msg = await call(srv, "crear_pieza", nombre="mal nombre")
    assert err
    err, msg = await call(srv, "crear_pieza", nombre="X", plantilla="../otra.prt")
    assert err and "plantilla" in msg.lower()


def test_servicio_valida_la_plantilla():
    import importlib.util
    spec = importlib.util.spec_from_file_location("servicio_tk", Path(__file__).parents[1] / "native" / "servicio_tk.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.PERMITE_ESCRITURA = True
    codigo, r = m.ejecutar({"orden": "pieza_nueva", "modelo": "A.PRT", "plantilla": "wtpub://x/y.prt;borrar"},
                           runner=lambda *a, **k: None)
    assert codigo == 400 and "Plantilla" in r["error"]


# --- croquis en caras oblicuas (0.21.0) ---------------------------------------------------------

S, C = math.sin(math.radians(30)), math.cos(math.radians(30))
# Cuña: base 40 (X) × 30 (Z) en Y = 0; cara inclinada 30° alrededor de X que sube hacia +Z: normal (0, C, -S)
CUNA = {"modelo": {"nombre": "CUNA"}, "unidad_longitud": "mm", "cuerpo_por_defecto": -5778,
        "caras_planas": [
            {"id": 900, "normal_exterior": [0, -1, 0], "punto_plano": [0, 0, 15], "caja_min": [-20, 0, 0], "caja_max": [20, 0, 30]},
            {"id": 910, "normal_exterior": [0, C, -S], "punto_plano": [0, 5, 0],
             "caja_min": [-20, 5, 0], "caja_max": [20, 5 + 30 * S / C, 30]},
            {"id": 920, "normal_exterior": [-1, 0, 0], "punto_plano": [-20, 5, 15], "caja_min": [-20, 0, 0], "caja_max": [-20, 22, 30]},
            {"id": 930, "normal_exterior": [1, 0, 0], "punto_plano": [20, 5, 15], "caja_min": [20, 0, 0], "caja_max": [20, 22, 30]}],
        "planos_referencia": [{"nombre": "PERFIL", "id": 2, "normal": [1, 0, 0], "origen": [0, 0, 0]},
                              {"nombre": "PLANTA", "id": 4, "normal": [0, 1, 0], "origen": [0, 0, 0]},
                              {"nombre": "ALZADO", "id": 6, "normal": [0, 0, 1], "origen": [0, 0, 0]}]}
L_INC = 30 / C                                  # longitud de la cara inclinada
CUNA_ARISTAS = {"modelo": {"nombre": "CUNA"}, "aristas": [
    {"id": 5001, "tipo": "recta", "inicio": [-20, 5, 0], "fin": [20, 5, 0], "longitud": 40, "caras": [910, 950]},
    {"id": 5002, "tipo": "recta", "inicio": [-20, 5 + 30 * S / C, 30], "fin": [20, 5 + 30 * S / C, 30], "longitud": 40,
     "caras": [910, 960]},
    {"id": 5003, "tipo": "recta", "inicio": [-20, 5, 0], "fin": [-20, 5 + 30 * S / C, 30], "longitud": L_INC,
     "caras": [910, 920]}]}


def test_corte_en_cara_oblicua():
    pl = planificar_extrusion({"tipo": "quitar", "plano": 910, "profundidad": 4, "sentido": "dentro",
                               "contornos": [{"tipo": "circulo", "centro": [5, 10], "diametro": 6}]},
                              CUNA, "N", aristas=CUNA_ARISTAS)
    p = pl["plan"]
    assert plan_valido(p)
    # referencias: PERFIL (perpendicular a la cara, orienta el croquis) y la arista baja 5001 (paralela a PERFIL… en u)
    assert lineas(p, "sk_ref")[:2] == ["sk_ref 5 2", "sk_ref 6 5001"]
    assert "e 386.447.450.455 r 5 2" in p and "rango" not in p
    assert "sentido -1" in p and "probar_sentidos" in p
    # el centro (u=5, v=10) cae en x = 5 y a 10 mm de la arista 5001 a lo largo de la cara
    t = [float(x) for x in lineas(p, "sk_circulo")[0].split()[1:]]
    esperado = [5, 5 + 10 * S, 10 * C]
    assert t[:3] == pytest.approx(esperado, abs=1e-6) and t[3] == pytest.approx(3)
    assert pl["resumen"]["cotas_boceto"] == ["Ø6", "centro a 5 de PERFIL", "centro a 10 de arista 5001"]
    assert pl["ids_caras"] == [910]


def test_cara_oblicua_errores():
    pet = {"tipo": "quitar", "plano": 910, "profundidad": 4, "sentido": "+Y",
           "contornos": [{"tipo": "circulo", "centro": [5, 10], "diametro": 6}]}
    with pytest.raises(PlanError, match="fuera"):
        planificar_extrusion(pet, CUNA, "N", aristas=CUNA_ARISTAS)
    # cara inclinada en dos ejes y sin nada perpendicular
    doble = json.loads(json.dumps(CUNA))
    doble["caras_planas"][1]["normal_exterior"] = [0.5, 0.70710678, -0.5]
    with pytest.raises(PlanError, match="perpendicular a la cara"):
        planificar_extrusion({**pet, "sentido": "dentro"}, doble, "N", aristas=CUNA_ARISTAS)


def test_fuera_y_dentro_en_cara_normal_a_un_eje():
    pl = planificar_extrusion({"tipo": "quitar", "plano": 962, "profundidad": 5, "sentido": "dentro",
                               "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 6}]}, CARAS, "N")
    assert "rango Y 15.000000 20.000000" in pl["plan"]          # cara +Y en y = 20: dentro = -Y
    with pytest.raises(PlanError, match="cara de la pieza"):
        planificar_extrusion({"tipo": "anadir", "plano": "PLANTA", "profundidad": 5, "sentido": "fuera",
                              "contornos": [{"tipo": "circulo", "centro": [0, 0], "diametro": 6}]}, VACIA_IAC, "N")


@pytest.mark.anyio
async def test_proponer_extrusion_en_cara_oblicua_lee_aristas(tmp_path, fake):
    fake.files = ["PRUEBA_CMD.PRT"]
    fake.active = "PRUEBA_CMD.PRT"
    creo = CreoSimulado()
    creo.caras = json.loads(json.dumps({**CUNA, "ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"}}))
    import test_operaciones as to
    viejas = to.ARISTAS
    to.ARISTAS = {**CUNA_ARISTAS, "ok": True, "modelo": {"nombre": "PRUEBA_CMD", "tipo": "PRT"}}
    try:
        srv = servidor(tmp_path, fake, creo)
        err, p = await call(srv, "proponer_extrusion", tipo="quitar", plano="910", profundidad=4, sentido="dentro",
                            contornos=[{"tipo": "rectangulo", "centro": [0, 12], "ancho": 10, "alto": 6}])
        assert not err, p
        assert any(a[1] == "aristas" for a in creo.llamadas)
        assert "u" in p["coordenadas_croquis"] and "arista 5001" in p["coordenadas_croquis"]["v"]
        err, r = await call(srv, "crear_extrusion", codigo=p["codigo"])
        assert not err and r["estado"] == "ok", r
        assert "sk_ref 6 5001" in creo.planes[0]
    finally:
        to.ARISTAS = viejas
