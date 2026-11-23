"""Pruebas de la lista de materiales a Excel y de la vuelta de parámetros."""

import json
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))
from test_server import call, fake, make  # noqa: E402,F401

from creo_mcp_iac import bom_excel  # noqa: E402
from creo_mcp_iac.bom_excel import clasificar_tornilleria  # noqa: E402
from creo_mcp_iac.gateway import NotAllowedError, ScopeError  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


def P(name, value, type_="STRING"):
    return {"name": name, "type": type_, "value": value}


BOM = {
    "file": "CONJ.ASM",
    "children": {"seq_path": "root", "file": "CONJ.ASM", "children": [
        {"seq_path": "root.1", "file": "PLACA.PRT"},
        {"seq_path": "root.2", "file": "SUB.ASM", "children": [
            {"seq_path": "root.2.1", "file": "EJE.PRT"},
            {"seq_path": "root.2.2", "file": "ISO4762_M5X16.PRT"},
            {"seq_path": "root.2.3", "file": "ISO4762_M5X16.PRT"},
        ]},
        {"seq_path": "root.3", "file": "SUB.ASM", "children": [
            {"seq_path": "root.3.1", "file": "EJE.PRT"},
            {"seq_path": "root.3.2", "file": "ISO4762_M5X16.PRT"},
            {"seq_path": "root.3.3", "file": "ISO4762_M5X16.PRT"},
        ]},
        {"seq_path": "root.4", "file": "ARANDELA_DIN125_M5.PRT"},
        {"seq_path": "root.5", "file": "RODAMIENTO_SKF.PRT"},
    ]},
}


def setup(fake):
    fake.files = ["CONJ.ASM", "SUB.ASM", "PLACA.PRT", "EJE.PRT", "ISO4762_M5X16.PRT",
                  "ARANDELA_DIN125_M5.PRT", "RODAMIENTO_SKF.PRT"]
    fake.active = "CONJ.ASM"
    fake.bom = BOM
    fake.model_params = {
        "CONJ.ASM": [P("PROYECTO", "EJEMPLO"), P("CODIGO_PLANO", "EJM-ASM-1"), P("PIEZA", "CONJUNTO")],
        "SUB.ASM": [P("PROYECTO", "EJEMPLO"), P("CODIGO_PLANO", "EJM-ASM-2"), P("PIEZA", "SUB")],
        "PLACA.PRT": [P("PROYECTO", "EJEMPLO"), P("CODIGO_PLANO", "EJM-DWG-1"), P("PIEZA", "PLACA"),
                      P("PTC_MATERIAL_NAME", "AL6061"), P("TOLERANCIA", " "),
                      P("CANTIDAD_FABRICADA", 4, "INTEGER"), P("DISEÑADO_POR", "persona1"),
                      P("PTC_WM_VERSION", "B.1"), P("PTC_WM_LIFECYCLE_STATE", "D_Detallado")],
        "EJE.PRT": [P("PROYECTO", "OTRO"), P("CODIGO_PLANO", "EJM-DWG-1"), P("PIEZA", " ")],
    }


# --- clasificación ---------------------------------------------------------------

@pytest.mark.parametrize("nombre,esperado", [
    ("ISO4762_M5X16.PRT", ("Tornillo", "M5", 16)),
    ("din912-m6x20.prt", ("Tornillo", "M6", 20)),
    ("ISO_10642_M4x12.prt", ("Tornillo", "M4", 12)),
    ("DIN934_M8.PRT", ("Tuerca", "M8", None)),
    ("ARANDELA_DIN125_M5.PRT", ("Arandela", "M5", None)),
    ("TORNILLO_ALLEN_M3X8.PRT", ("Tornillo", "M3", 8)),
    ("PASADOR_ISO2338_6X20.PRT", ("Pasador", "", None)),
    # nombres de la biblioteca de Creo vistos en un conjunto real (2026-11-21)
    ("DIN912-M2_5X25-6_5.PRT", ("Tornillo", "M2.5", 25)),
    ("DIN127-B2_5-6_5.PRT", ("Arandela", "M2.5", None)),
    ("DIN125-1-A6-I.PRT", ("Arandela", "M6", None)),
    ("DINENISO7092-2.PRT", ("Arandela", "M2", None)),
    ("ISO4034-M2_5-14H.PRT", ("Tuerca", "M2.5", None)),
    ("ISO2338-2_5H8X6-ST.PRT", ("Pasador", "Ø2.5", 6)),
    ("ISO2338-4M6X10-ST.PRT", ("Pasador", "Ø4", 10)),
    ("DIN7991-M3X8-8_8_WG.PRT", ("Tornillo", "M3", 8)),
    ("CAP_SCREWS_M2_5X8_WG_.PRT", ("Tornillo", "M2.5", 8)),
    ("ISO2338-4M6X12-ST.PRT", ("Pasador", "Ø4", 12)),
])
def test_clasificar(nombre, esperado):
    t = clasificar_tornilleria(nombre)
    assert (t["elemento"], t["metrica"], t["longitud"]) == esperado


@pytest.mark.parametrize("nombre", ["SOPORTE_M5.PRT", "PRJ_SOPORTE_LENTE.PRT", "PLACA_ISO.PRT",
                                    "RODAMIENTO_SKF.PRT", "PINZA.PRT", "PRJ_PINON_V2.PRT",
                                    "PRJ_SEPARADOR_P1320_030-15.PRT"])
def test_no_es_tornilleria(nombre):
    assert clasificar_tornilleria(nombre) is None


def test_clasificar_por_descripcion():
    t = clasificar_tornilleria("PRT0042.PRT", ("Tornillo DIN 912 M4x10",))
    assert (t["elemento"], t["norma"], t["metrica"], t["longitud"]) == ("Tornillo", "DIN 912", "M4", 10)


# --- exportación ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_exportar_bom_excel(tmp_path, fake):
    setup(fake)
    s, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "exportar_bom_excel", nombre_fichero="bom1")
    assert not err, r
    assert r["fichero"] == "bom1.xlsx"
    assert r["elementos_tornilleria"] == 5  # 4 tornillos + 1 arandela
    torn = {(g["elemento"], g["metrica"], g["longitud"]): g["cantidad"] for g in r["tornilleria"]}
    assert torn == {("Tornillo", "M5", 16): 4, ("Arandela", "M5", None): 1}
    avisos = " | ".join(a["aviso"] for a in r["avisos_detalle"])
    assert "PROYECTO «OTRO»" in avisos and "Mismo CODIGO_PLANO" in avisos
    assert "Vacío: PIEZA" in avisos and "Sin parámetros del cajetín" in avisos
    assert fake.mutating == []

    wb = load_workbook(s.export_dir / "bom1.xlsx")
    assert wb.sheetnames == ["BOM indentado", "Lista de piezas", "Tornilleria", "Avisos", "_control"]
    assert wb["_control"].sheet_state == "hidden"
    lista = [r for r in wb["Lista de piezas"].iter_rows(values_only=True)]
    modelos = [x[0] for x in lista[1:] if x[0] and str(x[0]).endswith((".PRT", ".ASM"))]
    assert modelos == ["PLACA.PRT", "SUB.ASM", "EJE.PRT", "RODAMIENTO_SKF.PRT"]
    eje = next(x for x in lista if x[0] == "EJE.PRT")
    assert eje[2] == 2  # cantidad total
    # sin nombres de personas en ningún sitio
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            assert not any("persona1" in str(v) for v in row if v)
    bomi = [x for x in wb["BOM indentado"].iter_rows(values_only=True)]
    assert [x[0] for x in bomi[1:]] == ["1", "2", "2.1", "2.2", "3", "4"]  # SUB.ASM x2 agrupado

    err, r = await call(srv, "exportar_bom_excel", nombre_fichero="bom1.xlsx")
    assert err and "Ya existe" in r


@pytest.mark.anyio
async def test_exportar_rechaza_pieza_y_rutas(tmp_path, fake):
    setup(fake)
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "exportar_bom_excel", conjunto="PLACA.PRT")
    assert err and "no es un conjunto" in r
    err, r = await call(srv, "exportar_bom_excel", nombre_fichero="..\\x.xlsx")
    assert err


# --- vuelta de parámetros -------------------------------------------------------------

def editar(ruta, cambios):
    wb = load_workbook(ruta)
    ws = wb["Lista de piezas"]
    cab = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        for (modelo, col), v in cambios.items():
            if row[0].value == modelo:
                row[cab.index(col)].value = v
    wb.save(ruta)


@pytest.mark.anyio
async def test_revisar_y_aplicar(tmp_path, fake):
    setup(fake)
    s, _, srv = make(tmp_path, fake, parameters=True)
    await call(srv, "exportar_bom_excel", nombre_fichero="b.xlsx")
    ruta = s.export_dir / "b.xlsx"
    editar(ruta, {("PLACA.PRT", "TOLERANCIA"): "ISO 2768-mK",
                  ("PLACA.PRT", "CANTIDAD_FABRICADA"): 6,
                  ("PLACA.PRT", "PTC_MATERIAL_NAME"): "ACERO",       # no editable: se ignora
                  ("EJE.PRT", "PIEZA"): "EJE MOTOR",
                  ("EJE.PRT", "RUGOSIDAD"): "Ra 1,6",               # no existe: no se crea
                  ("SUB.ASM", "PROYECTO"): "EJEMPLO"})               # sin cambio
    # alguien cambia la pieza en Creo después de exportar -> conflicto
    fake.model_params["EJE.PRT"][2]["value"] = "EJE B"
    err, r = await call(srv, "revisar_parametros_excel", fichero="b.xlsx")
    assert not err, r
    cambios = {(c["modelo"], c["parametro"]): c["despues"] for c in r["cambios"]}
    assert cambios == {("PLACA.PRT", "TOLERANCIA"): "ISO 2768-mK", ("PLACA.PRT", "CANTIDAD_FABRICADA"): 6}
    assert [(c["modelo"], c["parametro"]) for c in r["conflictos"]] == [("EJE.PRT", "PIEZA")]
    assert any(i.get("parametro") == "RUGOSIDAD" for i in r["ignorados"])
    assert r["windchill"]["PLACA.PRT"]["version_windchill"] == "B.1"
    assert fake.mutating == []

    err, msg = await call(srv, "aplicar_parametros_excel", fichero="b.xlsx", codigo="00000000")
    assert err and "no coinciden" in msg
    assert fake.mutating == []

    err, a = await call(srv, "aplicar_parametros_excel", fichero="b.xlsx", codigo=r["codigo"])
    assert not err, a
    assert len(a["aplicados"]) == 2 and not a["fallidos"]
    sets = [x for x in fake.requests if (x["command"], x["function"]) == ("parameter", "set")]
    assert {(x["data"]["file"], x["data"]["name"], x["data"]["value"]) for x in sets} == {
        ("PLACA.PRT", "TOLERANCIA", "ISO 2768-mK"), ("PLACA.PRT", "CANTIDAD_FABRICADA", 6)}
    assert all(x["data"].get("no_create") for x in sets)
    assert not any(c[0] == "file" and c[1] in ("save", "save_as") for c in fake.commands)
    # segunda revisión: ya no hay cambios
    err, r2 = await call(srv, "revisar_parametros_excel", fichero="b.xlsx")
    assert not r2["cambios"]


@pytest.mark.anyio
async def test_aplicar_no_existe_sin_flag(tmp_path, fake):
    setup(fake)
    _, gw, srv = make(tmp_path, fake)
    err, _ = await call(srv, "aplicar_parametros_excel", fichero="b.xlsx", codigo="x")
    assert err
    with pytest.raises(NotAllowedError):
        gw.call("parameter_set", file_="PLACA.PRT", name="PIEZA", value="X")


def test_pasarela_limita_parametros(tmp_path, fake):
    setup(fake)
    _, gw, _ = make(tmp_path, fake, parameters=True)
    with pytest.raises(ScopeError):
        gw.call("parameter_set", file_="PLACA.PRT", name="DISEÑADO_POR", value="X")
    with pytest.raises(ScopeError):
        gw.call("parameter_set", file_="PLACA.PRT", name="PTC_MATERIAL_NAME", value="X")
    with pytest.raises(ScopeError):
        gw.call("parameter_set", file_="NOABIERTO.PRT", name="PIEZA", value="X")
    with pytest.raises(ScopeError):
        gw.call("parameter_set", file_="*.PRT", name="PIEZA", value="X")
    with pytest.raises(NotAllowedError):
        gw.call("file_save", file_="PLACA.PRT")
    gw.call("parameter_set", file_="PLACA.PRT", name="PIEZA", value="X")
    assert fake.mutating == [("parameter", "set")]


def test_libro_ajeno(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Hoja1"
    ruta = tmp_path / "x.xlsx"
    wb.save(ruta)
    with pytest.raises(ValueError):
        bom_excel.leer_libro(ruta)


def test_tipo_entero_invalido():
    libro = {"filas": {"A.PRT": {"modelo": "A.PRT", "valores": {"CANTIDAD_FABRICADA": "seis"}}},
             "control": {"A.PRT": {"CANTIDAD_FABRICADA": {"type": "INTEGER", "value": 4}}},
             "duplicados": []}
    r = bom_excel.comparar(libro, {"A.PRT": {"CANTIDAD_FABRICADA": {"type": "INTEGER", "value": 4}}})
    assert r["errores"] and not r["cambios"] and r["codigo"] is None
    json.dumps(r)


def test_celda_limpia():
    assert bom_excel.celda("\x01n\x0220 H7\x07") == "Ø20 H7"
    assert bom_excel.celda("=HYPERLINK(1)") == "=HYPERLINK(1)"
    assert bom_excel.celda("-0,1") == "-0,1"
    assert bom_excel.celda("  ") == ""


@pytest.mark.anyio
async def test_lectura_en_bloque(tmp_path, fake):
    setup(fake)
    _, _, srv = make(tmp_path, fake)
    await call(srv, "exportar_bom_excel", nombre_fichero="b.xlsx")
    pl = [r["data"].get("file") for r in fake.requests if (r["command"], r["function"]) == ("parameter", "list")]
    assert pl == ["*"]  # una sola petición con comodín


def test_simbolos_no_son_cambios():
    v = "PLACA \x01n\x0220"
    libro = {"filas": {"A.PRT": {"modelo": "A.PRT", "valores": {"PIEZA": "PLACA Ø20"}}},
             "control": {"A.PRT": {"PIEZA": {"type": "STRING", "value": v}}}, "duplicados": []}
    r = bom_excel.comparar(libro, {"A.PRT": {"PIEZA": {"type": "STRING", "value": v}}})
    assert not r["cambios"] and not r["conflictos"]


def test_relectura_tras_libreoffice(tmp_path):
    """El libro sigue siendo legible después de que otro programa lo guarde."""
    import shutil
    import subprocess

    if not shutil.which("soffice"):
        pytest.skip("sin LibreOffice")
    from creo_mcp_iac.bom import build_bom

    params = {"PLACA.PRT": {"PIEZA": {"type": "STRING", "value": "PLACA"},
                            "CANTIDAD_FABRICADA": {"type": "INTEGER", "value": 4}}}
    datos = bom_excel.preparar(build_bom(BOM), params)
    bom_excel.exportar(datos, tmp_path / "b.xlsx", {})
    editar(tmp_path / "b.xlsx", {("PLACA.PRT", "PIEZA"): "PLACA BASE"})
    out = tmp_path / "out"
    subprocess.run(["soffice", "--headless", "--convert-to", "xlsx", "--outdir", str(out),
                    str(tmp_path / "b.xlsx")], check=True, capture_output=True, timeout=120)
    libro = bom_excel.leer_libro(out / "b.xlsx")
    r = bom_excel.comparar(libro, params)
    assert [(c["parametro"], c["despues"]) for c in r["cambios"]] == [("PIEZA", "PLACA BASE")]


def test_comerciales_y_proyecto_de_referencia():
    from creo_mcp_iac.bom import build_bom

    raw = {"file": "TOP.ASM", "children": {"seq_path": "root", "file": "TOP.ASM", "children": [
        {"seq_path": "root.1", "file": "A.PRT"},
        {"seq_path": "root.2", "file": "B.PRT"},
        {"seq_path": "root.3", "file": "C.PRT"},
        {"seq_path": "root.4", "file": "ROD.ASM", "children": [
            {"seq_path": "root.4.1", "file": "BOLA.PRT"},
            {"seq_path": "root.4.2", "file": "C.PRT"}]},
    ]}}
    pp = lambda proy, cod="X": {"PROYECTO": {"type": "STRING", "value": proy},  # noqa: E731
                                "CODIGO_PLANO": {"type": "STRING", "value": cod},
                                "PIEZA": {"type": "STRING", "value": "P"}}
    params = {"A.PRT": pp("EJEMPLO", "1"), "B.PRT": pp("EJEMPLA", "2"), "C.PRT": pp("EJEMPLO", "3"),
              "ROD.ASM": pp("", "NA"), "BOLA.PRT": pp("")}
    d = bom_excel.preparar(build_bom(raw), params)
    assert list(d["piezas"]) == ["A.PRT", "B.PRT", "C.PRT", "ROD.ASM"]  # BOLA omitida; C sigue (también fuera)
    assert d["piezas"]["ROD.ASM"]["tipo"] == "ASM comercial"
    avisos = " | ".join(a["modelo"] + ": " + a["aviso"] for a in d["avisos"])
    assert "B.PRT: PROYECTO «EJEMPLA»" in avisos
    assert "ROD.ASM" not in avisos and "BOLA" not in avisos


@pytest.mark.anyio
async def test_resultado_desconocido(tmp_path, fake):
    """Si Creo deja de responder a mitad de aplicar, no se sigue escribiendo y se exige revisar."""
    setup(fake)
    s, gw, srv = make(tmp_path, fake, parameters=True)
    await call(srv, "exportar_bom_excel", nombre_fichero="u.xlsx")
    editar(s.export_dir / "u.xlsx", {("PLACA.PRT", "TOLERANCIA"): "ISO 2768-mK", ("PLACA.PRT", "CANTIDAD_FABRICADA"): 6,
                                    ("EJE.PRT", "PROYECTO"): "EJEMPLO"})
    err, r = await call(srv, "revisar_parametros_excel", fichero="u.xlsx")
    assert len(r["cambios"]) == 3
    fake.cortar_en_set = 2
    err, a = await call(srv, "aplicar_parametros_excel", fichero="u.xlsx", codigo=r["codigo"])
    assert not err, a
    assert a["estado"] == "resultado_desconocido"
    assert len(a["aplicados_confirmados"]) == 1 and len(a["resultado_desconocido"]) == 1 and len(a["no_intentados"]) == 1
    sets = [x for x in fake.requests if (x["command"], x["function"]) == ("parameter", "set")]
    assert len(sets) == 2   # el tercero no se intenta
    err, msg = await call(srv, "aplicar_parametros_excel", fichero="u.xlsx", codigo=r["codigo"])
    assert err and "resultado desconocido" in msg
    err, r2 = await call(srv, "revisar_parametros_excel", fichero="u.xlsx")
    assert not err and r2["resultado_desconocido_anterior"]
    assert len(r2["cambios"]) == 2        # el primero ya está en Creo
    err, a2 = await call(srv, "aplicar_parametros_excel", fichero="u.xlsx", codigo=r2["codigo"])
    assert not err and a2["estado"] == "ok" and len(a2["aplicados"]) == 2, a2


@pytest.mark.anyio
async def test_capacidades(tmp_path, fake):
    setup(fake)
    _, _, srv = make(tmp_path, fake, parameters=True)
    err, c = await call(srv, "creo_capacidades")
    assert not err, c
    assert "aplicar_parametros_excel" in c["herramientas"]["escritura"]
    assert "exportar_bom_excel" in c["herramientas"]["exportacion"]
    assert "leer_parametros" in c["herramientas"]["lectura"]
    assert c["escritura_en_creo"]["parametros_del_cajetin_desde_excel"] is True
    assert c["escritura_en_creo"]["guardar_modelos"] is False
    assert c["creo"] == "responde" and c["resultado_desconocido"] == []
    assert fake.mutating == []
