"""Taladros conjugados en un conjunto (sin Creo)."""

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))
from test_nativo import Runner, con_nativo  # noqa: E402
from test_server import call, fake  # noqa: E402,F401

from creo_mcp_iac.conjugados import revisar_conjugados  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


def roscado(i, x, y, tam="M6x1", broca=5.0, largo=15):
    return {"id": i, "nombre": f"R{i}", "clase": "taladro", "serie_rosca": "ISO", "tamano_tornillo": tam,
            "elementos": {"386.1671.1672": 24, "386.1671.1673": 14, "386.1671.1347": broca,
                          "386.1671.1662": 29, "386.1671.1663": largo, "386.1671.1674": 26},
            "ejes": [[x, y, 0, x, y, -largo]]}


def holgura(i, x, y, tam="M6x1", d=6.6):
    return {"id": i, "nombre": f"H{i}", "clase": "taladro", "serie_rosca": "ISO", "tamano_tornillo": tam,
            "elementos": {"386.1671.1672": 24, "386.1671.1673": 15, "386.1671.1347": d, "386.1671.1662": 31},
            "ejes": [[x, y, 0, x, y, -10]]}


def recto(i, x, y, d):
    return {"id": i, "nombre": f"D{i}", "clase": "taladro",
            "elementos": {"386.1671.1672": 16, "386.1671.82": d, "386.1671.1684.1686.1688": 3},
            "ejes": [[x, y, 0, x, y, -10]]}


# TAPA (10 mm) girada 90° alrededor de Z y subida 10 mm: su X local va al +Y del conjunto.
TALADROS = {"ok": True, "version_tk": "0.5.1", "modelo": {"nombre": "TOP", "tipo": "ASM"}, "unidad_longitud": "mm",
            "modelos": [
                {"nombre": "TOP", "tipo": "ASM", "instancias": 1, "operaciones": []},
                {"nombre": "TAPA", "tipo": "PRT", "instancias": 1, "unidad_longitud": "mm", "operaciones": [
                    holgura(11, 0, 0), holgura(12, 30, 0), recto(13, 60, 0, 5.0), holgura(14, 90, 0, "M5x.8", 5.5),
                    holgura(15, 120, 0), recto(16, 150, 0, 6.0)]},
                {"nombre": "BASE", "tipo": "PRT", "instancias": 1, "unidad_longitud": "mm", "operaciones": [
                    roscado(21, 0, 0), roscado(22, 0, 30.5), roscado(23, 0, 60), roscado(24, 0, 90),
                    roscado(25, 0, 120.2), recto(26, 0, 150, 6.0), roscado(27, 0, 300, "M8x1.25", 6.8)]}]}

GIRO = [0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 10, 1]
IDENT = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
POSICIONES = {"ok": True, "version_tk": "0.5.1", "modelo": {"nombre": "TOP", "tipo": "ASM"}, "unidad_longitud": "mm",
              "componentes": [{"modelo": "TAPA", "tipo": "PRT", "ruta": [5], "matriz": GIRO},
                              {"modelo": "BASE", "tipo": "PRT", "ruta": [6], "matriz": IDENT},
                              {"modelo": "DIN912-M6X20", "tipo": "PRT", "ruta": [7], "matriz": IDENT}]}


def test_revision_de_conjugados():
    r = revisar_conjugados(TALADROS, POSICIONES)
    assert r["taladros_revisados"] == 13 and len(r["pares"]) == 6
    pares = {(p["a"]["id"], p["b"]["id"]): p for p in r["pares"]}
    assert pares[(11, 21)]["estado"] == "correcto" and pares[(11, 21)]["descentrado"] < 1e-6
    assert pares[(11, 21)]["punto"] == [0, 0, 0]
    mal = pares[(12, 22)]
    assert mal["estado"] == "error" and "Desalineados 0.500" in mal["nota"] and "0.300" in mal["nota"]
    assert pares[(13, 23)]["estado"] == "error" and "no pasa" in pares[(13, 23)]["nota"]
    assert pares[(14, 24)]["estado"] == "error" and "Tamaños distintos" in pares[(14, 24)]["nota"]
    dentro = pares[(15, 25)]
    assert dentro["estado"] == "aviso" and "dentro de la holgura" in dentro["nota"]
    assert pares[(16, 26)]["estado"] == "correcto"
    assert [p["estado"] for p in r["pares"]][:3] == ["error"] * 3       # errores primero
    assert r["por_estado"] == {"error": 3, "aviso": 1, "correcto": 2}
    assert len(r["sin_pareja"]) == 1 and r["roscados_sin_pareja"] == 1 and r["sin_pareja"][0]["id"] == 27


def test_sin_tornilleria():
    t = {**TALADROS, "modelos": TALADROS["modelos"] + [
        {"nombre": "DIN912-M6X20", "tipo": "PRT", "instancias": 1, "unidad_longitud": "mm", "operaciones": [
            {"id": 72, "clase": "rosca_cosmetica", "elementos": {"386.4900": 4.9}, "ejes": [[0, 0, 0, 0, 0, -20]]}]}]}
    pos = {**POSICIONES, "componentes": POSICIONES["componentes"][:2] + [
        {"modelo": "DIN912-M6X20", "tipo": "PRT", "ruta": [7], "matriz": GIRO}]}
    assert len(revisar_conjugados(t, pos)["pares"]) > 6
    r = revisar_conjugados(t, pos, excluir=lambda n: n.startswith("DIN"))
    assert len(r["pares"]) == 6 and r["taladros_revisados"] == 13


def test_separacion_e_inclinacion():
    pos = {**POSICIONES, "componentes": [dict(POSICIONES["componentes"][0], matriz=GIRO[:14] + [12, 1]),
                                         POSICIONES["componentes"][1]]}
    assert revisar_conjugados(TALADROS, pos)["pares"] == []                     # 2 mm de separación
    r = revisar_conjugados(TALADROS, pos, separacion_max=2.5)
    assert len(r["pares"]) == 6 and "Separados 2.00 mm" in r["pares"][-1]["nota"]
    # una pieza con un eje inclinado 2°
    t = {**TALADROS, "modelos": [TALADROS["modelos"][2],
                                 {"nombre": "TAPA", "tipo": "PRT", "instancias": 1, "unidad_longitud": "mm",
                                  "operaciones": [dict(holgura(11, 0, 0), ejes=[[0, 0, 0, 0.349, 0, -10]])]}]}
    p = revisar_conjugados(t, POSICIONES)["pares"]
    assert len(p) == 1 and p[0]["estado"] == "error" and "no paralelos" in p[0]["nota"]


@pytest.mark.anyio
async def test_herramienta_taladros_conjugados(tmp_path, fake):
    fake.files = ["TOP.ASM", "TAPA.PRT", "BASE.PRT"]
    fake.active = "TOP.ASM"
    runner = Runner({"taladros": TALADROS, "posiciones": POSICIONES})
    s, gw, srv = con_nativo(tmp_path, fake, runner)
    err, r = await call(srv, "taladros_conjugados")
    assert not err, r
    assert [x[1] for x in runner.llamadas] == ["taladros", "posiciones"]
    assert r["parejas"] == 6 and r["por_estado"]["error"] == 3 and len(r["problemas"]) == 4
    assert r["problemas"][0]["a"]["pieza"] == "TAPA.PRT #1"
    wb = load_workbook(s.export_dir / r["fichero"])
    assert wb.sheetnames == ["Resumen", "Parejas", "Sin pareja"] and wb["Parejas"].max_row == 7
    fake.active = "TAPA.PRT"
    err, msg = await call(srv, "taladros_conjugados", conjunto="TAPA.PRT")
    assert err and "no es un conjunto" in msg
