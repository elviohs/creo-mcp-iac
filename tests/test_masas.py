"""Pruebas del presupuesto de masas."""

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))
from test_bom_excel import P  # noqa: E402
from test_server import call, fake, make  # noqa: E402,F401

from creo_mcp_iac import masas  # noqa: E402
from creo_mcp_iac.bom import build_bom  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


# TOP: 2 x SUB (cada uno: 1 PLACA + 4 tornillos) + 1 ROD (comercial, con bolas dentro) + 1 EJE
BOM = {"file": "TOP.ASM", "children": {"seq_path": "root", "file": "TOP.ASM", "children": [
    {"seq_path": "root.1", "file": "SUB.ASM", "children": [
        {"seq_path": "root.1.1", "file": "PLACA.PRT"},
        *[{"seq_path": f"root.1.{i}", "file": "DIN912-M4X10.PRT"} for i in range(2, 6)]]},
    {"seq_path": "root.2", "file": "SUB.ASM", "children": [
        {"seq_path": "root.2.1", "file": "PLACA.PRT"},
        *[{"seq_path": f"root.2.{i}", "file": "DIN912-M4X10.PRT"} for i in range(2, 6)]]},
    {"seq_path": "root.3", "file": "ROD.ASM", "children": [
        {"seq_path": "root.3.1", "file": "BOLA.PRT"}, {"seq_path": "root.3.2", "file": "BOLA.PRT"}]},
    {"seq_path": "root.4", "file": "EJE.PRT"},
]}}

PARAMS = {
    "TOP.ASM": [P("CODIGO_PLANO", "T-0"), P("PTC_UNITS_MASS", "tonne")],
    "SUB.ASM": [P("CODIGO_PLANO", "T-1"), P("PTC_UNITS_MASS", "tonne")],
    "PLACA.PRT": [P("CODIGO_PLANO", "T-2"), P("PIEZA", "PLACA"), P("PTC_MATERIAL_NAME", "AL_6061_T6"),
                  P("MASA", 500.0, "DOUBLE"), P("MASA_UNIT", "gr"), P("PTC_UNITS_MASS", "tonne")],
    "DIN912-M4X10.PRT": [P("PTC_UNITS_MASS", "tonne"), P("PTC_MATERIAL_NAME", "A2")],
    "ROD.ASM": [P("CODIGO_PLANO", "NA"), P("PTC_UNITS_MASS", "tonne")],
    "BOLA.PRT": [P("PTC_UNITS_MASS", "tonne")],
    "EJE.PRT": [P("CODIGO_PLANO", "T-3"), P("PTC_UNITS_MASS", "tonne"),  # sin material
                P("MASA", 12.0, "DOUBLE"), P("MASA_UNIT", "gr")],  # MASA con masa calculada cero
}
MASAS = {  # en toneladas (sistema mmNs, como los modelos del IAC)
    "PLACA.PRT": 0.0004, "DIN912-M4X10.PRT": 0.000002, "ROD.ASM": 0.00005, "BOLA.PRT": 0.000001,
    "EJE.PRT": 0.0,
}


def setup(fake):
    fake.files = list(PARAMS)
    fake.active = "TOP.ASM"
    fake.bom = BOM
    fake.model_params = {k: [dict(p) for p in v] for k, v in PARAMS.items()}
    fake.massprops = {k: {"mass": v, "volume": 1.0, "density": 2.7e-9} for k, v in MASAS.items()}
    fake.massprops["TOP.ASM"] = {"mass": 2 * (0.0004 + 4 * 0.000002) + 0.00005,
                                 "ctr_grav": {"x": 1.0, "y": 2.0, "z": 3.0}}


def test_hojas_y_cantidades():
    params = {k: {p["name"]: {"type": p["type"], "value": p["value"]} for p in v} for k, v in PARAMS.items()}
    h = masas.hojas(build_bom(BOM), params)
    cant = {k: v["cantidad"] for k, v in h["hojas"].items()}
    assert cant == {"PLACA.PRT": 2, "DIN912-M4X10.PRT": 8, "ROD.ASM": 1, "EJE.PRT": 1}  # sin BOLA
    assert h["grupos"] == ["SUB.ASM", "ROD.ASM", masas.SUELTAS]
    assert h["hojas"]["ROD.ASM"]["tipo"] == "ASM comercial"


@pytest.mark.anyio
async def test_presupuesto_masas(tmp_path, fake):
    setup(fake)
    s, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "presupuesto_masas", nombre_fichero="m1")
    assert not err, r
    assert r["fichero"] == "m1.xlsx"
    assert r["masa_suma_kg"] == pytest.approx(2 * 0.4 + 8 * 0.002 + 0.05)  # 0,866 kg
    assert r["diferencia_pct"] == pytest.approx(0.0, abs=1e-6)
    assert r["subtotales_kg"]["SUB.ASM"] == pytest.approx(0.816)
    avisos = " | ".join(a["modelo"] + ": " + a["aviso"] for a in r["avisos_detalle"])
    assert "EJE.PRT: Masa cero" in avisos and "EJE.PRT: Sin material" in avisos
    assert "EJE.PRT: Parámetro MASA = 0.012 kg, pero la masa calculada es cero" in avisos
    assert "PLACA.PRT: Parámetro MASA distinto" in avisos  # 500 g frente a 400 g
    assert "BOLA" not in avisos
    assert fake.mutating == []
    wb = load_workbook(s.export_dir / "m1.xlsx")
    assert wb.sheetnames == ["Resumen", "Por subconjunto", "Piezas", "Avisos"]
    ws = wb["Piezas"]
    filas = {row[0].value: row for row in ws.iter_rows(min_row=2) if row[0].value}
    assert filas["PLACA.PRT"][1].value == "Fabricada" and filas["PLACA.PRT"][7].value == 2
    assert filas["PLACA.PRT"][9].value == "=H2*I2" or str(filas["PLACA.PRT"][9].value).startswith("=H")
    assert filas["DIN912-M4X10.PRT"][1].value == "Tornillería"
    assert filas["ROD.ASM"][1].value == "Comercial"
    assert str(filas["TOTAL"][9].value).startswith("=SUM(J2:J")


def test_formulas_calculadas_por_libreoffice(tmp_path):
    """Las fórmulas del Excel dan el mismo total que el cálculo en Python."""
    import shutil
    import subprocess

    if not shutil.which("soffice"):
        pytest.skip("sin LibreOffice")
    params = {k: {p["name"]: {"type": p["type"], "value": p["value"]} for p in v} for k, v in PARAMS.items()}
    datos = {k: {"props": {"mass": v}, "unidad_masa": "tonne"} for k, v in MASAS.items()}
    d = masas.preparar(build_bom(BOM), params, datos, {"si": {"masa_kg": 0.866}, "unidad_longitud": "mm"})
    masas.exportar(d, tmp_path / "m.xlsx", {})
    subprocess.run(["soffice", "--headless", "--convert-to", "xlsx", "--outdir", str(tmp_path / "o"),
                    str(tmp_path / "m.xlsx")], check=True, capture_output=True, timeout=120)
    wb = load_workbook(tmp_path / "o" / "m.xlsx", data_only=True)
    tot = next(r for r in wb["Piezas"].iter_rows(values_only=True) if r[0] == "TOTAL")
    assert tot[9] == pytest.approx(0.866) and tot[7] == 12


@pytest.mark.anyio
async def test_rechaza_pieza(tmp_path, fake):
    setup(fake)
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "presupuesto_masas", conjunto="PLACA.PRT")
    assert err and "propiedades_masa" in r


def test_funciones_lentas_con_mas_tiempo(tmp_path, fake):
    """bom_get_paths y massprops usan un tiempo máximo mayor y luego se restablece."""
    from creo_mcp_iac.gateway import SLOW_TIMEOUT_S

    setup(fake)
    _, gw, _ = make(tmp_path, fake)
    vistos = []
    orig = gw._transport._session.post

    def espia(url, **kw):
        vistos.append(kw.get("timeout"))
        return orig(url, **kw)

    gw._transport._session.post = espia
    gw.call("bom_get_paths", file_="TOP.ASM")
    gw.call("parameter_list", file_="*", name=["PIEZA"])
    gw.call("creo_pwd")
    assert vistos[-3:] == [SLOW_TIMEOUT_S, SLOW_TIMEOUT_S, 5]
    assert gw._transport.timeout_actual is None
