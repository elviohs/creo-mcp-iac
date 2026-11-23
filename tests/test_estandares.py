"""Pruebas de la revisión de estándares."""

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))
from test_bom_excel import P  # noqa: E402
from test_server import call, fake, make  # noqa: E402,F401

from creo_mcp_iac import estandares  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


BOM = {"file": "TOP.ASM", "children": {"seq_path": "root", "file": "TOP.ASM", "children": [
    {"seq_path": "root.1", "file": "PLACA.PRT"},
    {"seq_path": "root.2", "file": "EJE.PRT"},
    {"seq_path": "root.3", "file": "TAPA.PRT"},
    {"seq_path": "root.4", "file": "JUNTA.PRT"},
    {"seq_path": "root.5", "file": "ROD.ASM", "children": [{"seq_path": "root.5.1", "file": "BOLA.PRT"}]},
    {"seq_path": "root.6", "file": "DIN912-M4X10.PRT"},
    {"seq_path": "root.7", "file": "SOPORTE.PRT"},
    {"seq_path": "root.8", "file": "ENCODER.PRT"},
]}}


def base(cod, pieza="P", mat="AL_6061_T6", proy="EJEMPLO", **extra):
    ps = [P("CODIGO_PLANO", cod), P("PROYECTO", proy), P("PIEZA", pieza), P("PTC_MATERIAL_NAME", mat),
          P("PTC_UNITS_LENGTH", "mm"), P("PTC_UNITS_MASS", "tonne")]
    ps += [P(k, v) for k, v in extra.items()]
    return ps


def setup(fake):
    fake.files = ["TOP.ASM", "PLACA.PRT", "EJE.PRT", "TAPA.PRT", "JUNTA.PRT", "ROD.ASM", "BOLA.PRT",
                  "DIN912-M4X10.PRT", "SOPORTE.PRT", "PLACA.DRW", "ENCODER.PRT"]
    fake.active = "TOP.ASM"
    fake.bom = BOM
    fake.model_params = {
        "TOP.ASM": base("EJM-DWG-421100-0000-00", "CONJUNTO", mat=""),
        "PLACA.PRT": base("EJM-DWG-421100-0001-00", VALIDO_PARA="FABRICACIÓN", PTC_WM_REVISION="B",
                          PTC_WM_LIFECYCLE_STATE="D_Preliminar", PTC_WM_VERSION="B.3")
        + [P("MASA", 500.0, "DOUBLE"), P("MASA_UNIT", "gr")],
        "EJE.PRT": base("EJM-DWG-421100-0001-00", proy="EJEMPLA", VALIDO_PARA="FABRICACION"),   # R03, R04, R05
        "TAPA.PRT": base("EJM-DW-4211-1", mat="", PTC_UNITS_LENGTH="inch"),                        # R02, R01, R08
        "JUNTA.PRT": base("EJM-DWG-421100-0004-00", mat="PVC"),                                   # R07 vacío
        "ROD.ASM": [P("CODIGO_PLANO", "NA")],
        "BOLA.PRT": [P("PTC_MATERIAL_NAME", "PVC")],                                             # comercial: no
        "SOPORTE.PRT": base("EJM-DWG-421100-0005-00", mat="ACERO_42_2", PTC_WM_REVISION="C"),    # R07 criogenia, R09
        "PLACA.DRW": [P("PTC_WM_REVISION", "A")],
        "ENCODER.PRT": [P("CODIGO_PLANO", "NA"), P("PTC_MATERIAL_NAME", "PVC")],                # comercial: solo R07
    }
    fake.drawing_models = {"PLACA.DRW": ["PLACA.PRT"]}
    fake.massprops = {"PLACA.PRT": {"mass": 0.0004}}   # 400 g frente a 500 g -> R10


@pytest.mark.anyio
async def test_revisar_estandares(tmp_path, fake):
    setup(fake)
    s, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "revisar_estandares", criogenia=True, nombre_fichero="e1")
    assert not err, r
    por = {k.split()[0]: v for k, v in r["por_regla"].items()}
    det = " | ".join(f"{x['modelo']}:{x['regla']}:{x['detalle']}" for x in r["incumplimientos"])
    assert por["R01"] == 1 and "TAPA.PRT:R01:Vacío: PTC_MATERIAL_NAME" in det, det     # TOP.ASM sin material: no
    assert por["R02"] == 1 and "TAPA.PRT:R02" in det
    assert por["R03"] == 1 and "PLACA.PRT, EJE.PRT:R03" in det
    assert por["R04"] == 1 and "EJE.PRT:R04" in det
    assert por["R05"] == 1 and "VALIDO_PARA escrito de 2 formas" in det
    assert por["R06"] == 1 and "PLACA.PRT:R06" in det
    assert por["R07"] == 5 and "ENCODER.PRT:R07:Material PVC (vacío) en un comercial" in det and "JUNTA.PRT:R07:Material PVC (vacío)" in det and "SOPORTE.PRT:R07:Material ACERO_42_2 (criogenia)" in det
    assert por["R08"] == 1 and "TAPA.PRT:R08" in det
    assert por["R09"] == 1 and "PLACA.PRT:R09:El plano PLACA.DRW está en la revisión A y el modelo en la B" in det
    assert por["R10"] == 1 and "PLACA.PRT:R10" in det
    assert "BOLA" not in det and "DIN912" not in det
    assert fake.mutating == []
    wb = load_workbook(s.export_dir / "e1.xlsx")
    assert wb.sheetnames == ["Resumen", "Incumplimientos", "Reglas"]
    # sin criogenia no se marca el acero
    err, r2 = await call(srv, "revisar_estandares", nombre_fichero="e2")
    assert not any(x["regla"] == "R07" and "ACERO" in x["detalle"] for x in r2["incumplimientos"])


def test_reglas_cargan():
    cfg = estandares.cargar_reglas()
    assert "EJEMPLO" in cfg["codigo_plano"]["por_proyecto"]
    import re
    for m in cfg["materiales"]:
        re.compile(m["patron"])
    rx = re.compile(next(m["patron"] for m in cfg["materiales"] if m["ambito"] == "criogenia" and "ACERO" in m["patron"]), re.I)
    assert rx.search("ACERO_42_2") and not rx.search("ACERO_INOX_316") and not rx.search("AISI_316")
