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
    with pytest.raises(PlanError, match="perpendicular a un eje"):
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
