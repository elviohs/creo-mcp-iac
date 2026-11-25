import json
import re
import sys
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

sys.path.insert(0, str(Path(__file__).parent))
from fake_creoson import FakeCreoson  # noqa: E402

from creo_mcp_iac.bom import build_bom  # noqa: E402
from creo_mcp_iac.config import ConfigError, Settings, check_host_allowed  # noqa: E402
from creo_mcp_iac.gateway import ALLOWED, CreoGateway, NotAllowedError, diagnose  # noqa: E402
from creo_mcp_iac.server import build_server  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake():
    with FakeCreoson() as f:
        yield f


def make(tmp_path, fake, export=True, drawings=False, parameters=False):
    exp = tmp_path / "export"
    exp.mkdir(exist_ok=True)
    s = Settings(
        creoson_host="127.0.0.1",
        creoson_port=fake.port,
        export_dir=exp if export else None,
        audit_log=tmp_path / "audit.jsonl",
        timeout_s=5,
        max_items=500,
        allow_drawings=drawings,
        allow_parameters=parameters,
    )
    gw = CreoGateway(s)
    return s, gw, build_server(s, gw)


async def call(server, name, **args):
    async with create_connected_server_and_client_session(server._mcp_server) as c:
        r = await c.call_tool(name, args)
        text = r.content[0].text if r.content else ""
        return r.isError, (text if r.isError else json.loads(text))


# --- seguridad: lista blanca --------------------------------------------------

READ_VERB = re.compile(r"^(get_|list|is_|massprops$|pwd$)")


def test_allowlist_has_only_read_verbs():
    """Toda función permitida (salvo conexión y exportación) es get_/list/is_."""
    for f in ALLOWED - {"connect", "disconnect", "interface_export_file", "interface_export_pdf"}:
        verb = f if f.startswith("is_") else f.split("_", 1)[1]
        assert READ_VERB.search(verb), f


@pytest.mark.parametrize("func", ["parameter_set", "kill_creo", "file_save", "file_erase",
                                  "creo_delete_files", "windchill_authorize", "file_open",
                                  "interface_mapkey", "feature_delete", "dimension_set"])
def test_gateway_blocks_write_functions(tmp_path, fake, func):
    _, gw, _ = make(tmp_path, fake)
    with pytest.raises(NotAllowedError):
        gw.call(func)
    assert fake.requests == []  # nunca llegó a CREOSON
    log = (tmp_path / "audit.jsonl").read_text()
    assert '"blocked"' in log and func in log


# --- seguridad: red -----------------------------------------------------------

def test_public_hosts_rejected():
    with pytest.raises(ConfigError):
        check_host_allowed("8.8.8.8", allow_lan=True)
    check_host_allowed("localhost", allow_lan=False)
    with pytest.raises(ConfigError):
        check_host_allowed("10.0.0.5", allow_lan=False)
    check_host_allowed("10.0.0.5", allow_lan=True)


def test_system_proxy_is_ignored(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    _, gw, _ = make(tmp_path, fake)
    assert gw.call("is_creo_running") is True


def test_transport_only_reaches_configured_url(tmp_path, fake):
    import creopyson.connection as cc
    import requests

    make(tmp_path, fake)
    with pytest.raises(requests.exceptions.InvalidURL):
        cc.requests.post("http://127.0.0.1:1/creoson", data="{}")


# --- herramientas -------------------------------------------------------------

@pytest.mark.anyio
async def test_tool_list_and_annotations(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    async with create_connected_server_and_client_session(srv._mcp_server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"creo_estado", "listar_modelos", "info_modelo", "leer_parametros",
                          "leer_cotas", "lista_materiales", "listar_caracteristicas",
                          "propiedades_masa", "info_plano", "exportar_step", "leer_notas",
                          "config_creo", "listar_plantillas_plano", "vistas_modelo", "exportar_pdf", "creo_capacidades",
                          "exportar_bom_excel", "revisar_parametros_excel", "presupuesto_masas", "revisar_estandares",
                          "interferencias_creo", "taladros_creo", "taladros_conjugados", "holguras_creo", "resultado_tarea", "listar_tareas",
                          "caras_creo", "seleccion_creo", "proponer_taladros", "operaciones_creo", "proponer_patron", "aristas_creo", "proponer_redondeos",
                          "proponer_chaflanes", "proponer_extrusion"}
    for n, t in tools.items():
        assert t.annotations.openWorldHint is False
        assert t.annotations.readOnlyHint is (n not in ("exportar_step", "exportar_pdf", "exportar_bom_excel",
                                                        "presupuesto_masas", "revisar_estandares",
                                                        "interferencias_creo", "taladros_creo",
                                                        "taladros_conjugados", "holguras_creo"))
        assert t.annotations.destructiveHint is not True or n not in ("interferencias_creo", "taladros_creo")


@pytest.mark.anyio
async def test_estado(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "creo_estado")
    assert not err and r["creo_en_marcha"] and r["modelo_activo"] == "CONJUNTO.ASM"


@pytest.mark.anyio
async def test_estado_without_creoson(tmp_path):
    s = Settings(creoson_host="127.0.0.1", creoson_port=1, audit_log=tmp_path / "a.jsonl", timeout_s=2)
    err, r = await call(build_server(s), "creo_estado")
    assert not err and r["creo_en_marcha"] is False and "CREOSON" in r["error"]


@pytest.mark.anyio
async def test_parametros_decode_base64(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "leer_parametros", modelo="soporte.prt")
    assert not err and r["modelo"] == "SOPORTE.PRT"
    desc = next(p for p in r["items"] if p["name"] == "DESCRIPCION")
    assert desc["value"] == "Soporte Ø20 ±0,1"


@pytest.mark.anyio
async def test_cotas_never_select(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "leer_cotas", modelo="SOPORTE.DRW")
    assert not err and r["items"][0]["tol_plus"] == 0.1
    req = [q for q in fake.requests if q["command"] == "dimension"][-1]
    assert req["data"].get("select") is False


@pytest.mark.anyio
async def test_bom_quantities(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "lista_materiales")  # modelo activo
    assert not err
    flat = {x["file"]: x["qty_total"] for x in r["agregada"]["items"]}
    assert flat == {"SOPORTE.PRT": 2, "SUBCONJ.ASM": 2, "TORNILLO.PRT": 8}
    lvl2 = [x for x in r["indentada"]["items"] if x["level"] == 2]
    assert lvl2 == [{"level": 2, "file": "TORNILLO.PRT", "qty": 4, "type": "PRT",
                     "seq_paths": ["root.3.1", "root.3.2", "root.3.3", "root.3.4"]}]


@pytest.mark.anyio
async def test_bom_rejects_part(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, msg = await call(srv, "lista_materiales", conjunto="SOPORTE.PRT")
    assert err and "no es un conjunto" in msg


@pytest.mark.anyio
async def test_features_masa_info_plano(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "listar_caracteristicas", modelo="SOPORTE.PRT", tipo="HOLE")
    assert not err and r["count"] == 2
    err, r = await call(srv, "propiedades_masa", modelo="SOPORTE.PRT")
    assert not err and r["unidades_masa"] == "kg" and r["propiedades"]["volume"] == 1000.0
    err, r = await call(srv, "info_modelo", modelo="SOPORTE.PRT")
    assert not err and r["material"] == "AL6061"
    err, r = await call(srv, "info_plano", plano="SOPORTE.DRW")
    assert not err and r["num_hojas"] == 2 and r["hojas"][0]["tamano"] == "A3"


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["NOEXISTE.PRT", "*.PRT", "../x.prt", "C:\\a.prt"])
async def test_model_must_be_in_session(tmp_path, fake, bad):
    _, _, srv = make(tmp_path, fake)
    err, msg = await call(srv, "leer_parametros", modelo=bad)
    assert err


@pytest.mark.anyio
async def test_export_disabled_by_default(tmp_path, fake):
    _, _, srv = make(tmp_path, fake, export=False)
    err, msg = await call(srv, "exportar_step", modelo="SOPORTE.PRT")
    assert err and "desactivada" in msg
    assert ("interface", "export_file") not in fake.commands


@pytest.mark.anyio
async def test_export_step_ok_and_no_overwrite(tmp_path, fake):
    s, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "exportar_step", modelo="SOPORTE.PRT", nombre_fichero="soporte_v1")
    assert not err and r["fichero"] == "soporte_v1.stp" and r["bytes"] > 0 and len(r["sha256"]) == 64
    req = [q for q in fake.requests if q["command"] == "interface"][-1]["data"]
    assert req["type"] == "STEP" and req["dirname"] == str(s.export_dir)
    err, msg = await call(srv, "exportar_step", modelo="SOPORTE.PRT", nombre_fichero="soporte_v1.stp")
    assert err and "Ya existe" in msg


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["../fuera.stp", "C:\\x.stp", "a/b.stp", ".oculto.stp"])
async def test_export_rejects_paths(tmp_path, fake, bad):
    _, _, srv = make(tmp_path, fake)
    err, _ = await call(srv, "exportar_step", modelo="SOPORTE.PRT", nombre_fichero=bad)
    assert err


@pytest.mark.anyio
async def test_export_rejects_drawing(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, _ = await call(srv, "exportar_step", modelo="SOPORTE.DRW")
    assert err


@pytest.mark.anyio
async def test_full_session_sends_only_read_commands(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    for name, args in [("creo_estado", {}), ("listar_modelos", {}), ("info_modelo", {"modelo": "SOPORTE.PRT"}),
                       ("leer_parametros", {}), ("leer_cotas", {"modelo": "SOPORTE.PRT"}),
                       ("lista_materiales", {}), ("listar_caracteristicas", {}),
                       ("propiedades_masa", {}), ("info_plano", {"plano": "SOPORTE.DRW"}),
                       ("leer_notas", {"modelo": "SOPORTE.DRW"})]:
        err, _ = await call(srv, name, **args)
        assert not err, name
    assert fake.mutating == []
    assert ("interface", "export_file") not in fake.commands
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) > 10 and all(json.loads(x)["event"] == "call" for x in lines)


def test_bom_flat_structure_variant():
    raw = {"file": "A.ASM", "children": [{"file": "P.PRT"}, {"file": "P.PRT"}]}
    assert build_bom(raw)["flat"] == [{"file": "P.PRT", "type": "PRT", "qty_total": 2}]


# --- formato (datos con la forma real de CREOSON 3.0.2 + Creo 12.4.1.0) ------

from creo_mcp_iac.formato import clean_params, dim_text, massprops_si  # noqa: E402


def test_clean_params_hides_pdm_and_names():
    raw = [
        {"name": "TOLERANCIA", "value": "FINA", "owner_name": "p.prt"},
        {"name": "DISEÑADO_POR", "value": "usuario1"},
        {"name": "REVISADO_POR", "value": ""},
        {"name": "PROI_CREATED_BY", "value": "usuario2"},
        {"name": "PTC_WM_LOCATION", "value": "/Productos/x"},
        {"name": "PDMREV", "value": "A.16"},
        {"name": "PTC_MATERIAL_NAME", "value": "AL_6082_T6"},
    ]
    out = {p["name"]: p["value"] for p in clean_params(raw, include_pdm=False, redact=True)}
    assert out == {"TOLERANCIA": "FINA", "DISEÑADO_POR": "[rellenado]", "REVISADO_POR": "",
                   "PTC_MATERIAL_NAME": "AL_6082_T6"}
    full = {p["name"]: p["value"] for p in clean_params(raw, include_pdm=True, redact=True)}
    assert full["PROI_CREATED_BY"] == "[rellenado]" and full["PDMREV"] == "A.16"


def test_dim_text_real_format():
    assert dim_text({"value": 34.0, "text": ["{0:\u0001n\u0002}{1:@D}\n"]}) == "Ø34"
    assert dim_text({"value": 164.14, "text": ["{0:@D}\n"]}) == "164,14"
    assert dim_text({"value": 1.0, "text": ["{0:\u0001z\u0002}{1:@D}\n"]}) == "[símbolo:z]1"


def test_massprops_si_real_values():
    props = {"mass": 0.011793133931959538, "volume": 4351709.93799245, "density": 2.71e-09,
             "surface_area": 169170.41386309185, "ctr_grav": {"x": -84.87, "y": 81.32, "z": -79.75}}
    si = massprops_si(props, "mm", "tonne")
    assert abs(si["masa_kg"] - 11.7931) < 1e-3
    assert abs(si["densidad_kg_m3"] - 2710) < 1e-6
    assert abs(si["volumen_m3"] - 0.0043517) < 1e-6
    assert abs(si["cdg_mm"]["x"] + 84.87) < 1e-9
    assert massprops_si(props, "mm", "desconocida") is None


@pytest.mark.anyio
async def test_tools_apply_formatting(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "leer_parametros", modelo="SOPORTE.PRT")
    names = {p["name"]: p["value"] for p in r["items"]}
    assert names["DISEÑADO_POR"] == "[rellenado]" and "PDMREV" not in names and r["omitidos_pdm"] == 2
    err, r = await call(srv, "leer_cotas", modelo="SOPORTE.PRT")
    assert r["tolerancia_general"] == "FINA"
    d48 = next(d for d in r["items"] if d["name"] == "d48")
    assert d48["texto_legible"] == "Ø34" and d48["tiene_tolerancia"] is False
    err, r = await call(srv, "propiedades_masa", modelo="SOPORTE.PRT")
    assert abs(r["si"]["masa_kg"] - 0.0027) < 1e-9  # fake: kg


# --- casos reales de la segunda prueba (13 modelos, 2026-11-20) ------------------

from creo_mcp_iac.formato import check_mass_param, tolerance  # noqa: E402


def test_tolerance_types_from_real_drawing():
    assert tolerance({"tolerance_type": "symmetric", "tol_symmetric_value": 0.09999999999999999}) == \
        {"tipo": "explicita", "texto": "±0,1", "mas": 0.1, "menos": 0.1}
    assert tolerance({"tolerance_type": "isodin", "tol_table_type": "general",
                      "tol_table_name": "NONE", "tol_table_column": -1})["tipo"] == "general"
    assert tolerance({"tolerance_type": "isodin", "tol_table_type": "hole",
                      "tol_table_name": "H7"}) == {"tipo": "ajuste_iso", "texto": "H7", "tabla": "hole"}
    assert tolerance({"tolerance_type": "plus_minus", "tol_plus": 0.1, "tol_minus": 0.05})["texto"] == "+0,1/-0,05"
    assert tolerance({})["tipo"] == "sin_datos"


def test_chamfer_degree_symbol():
    d = {"value": 5.0, "text": ["{0:Ch}{1:@D}{2:x45\u0001$\u0002}\n", "{3:(x4)}\n"]}
    assert dim_text(d) == "Ch5x45° / (x4)"


@pytest.mark.parametrize("params,calc,unit,estado,pct", [
    ({"MASA": 130.9750372351249, "MASA_UNIT": "kg"}, 0.1176414693447006, "tonne", "difiere", 11.33),
    ({"MASA": 224.98434916059222, "MASA_UNIT": "gr"}, 0.00022498434916059222, "tonne", "coincide", 0.0),
    ({"MASA": 0.0005517007799227784}, 0.0005517007799227784, "tonne", "coincide", 0.0),
    ({"MASA": 0.0, "MASA_UNIT": "gr"}, 0.011793133931959538, "tonne", "difiere", -100.0),
    ({"MASA": 5.502858851726314, "MASA_UNIT": "kg"}, 0.005477317592352762, "tonne", "difiere", 0.47),
])
def test_mass_param_check_real_cases(params, calc, unit, estado, pct):
    r = check_mass_param(params, calc, unit)
    assert r["estado"] == estado and r["diferencia_pct"] == pct
    assert ("nota" in r) == ("MASA_UNIT" not in params)


@pytest.mark.anyio
async def test_drawing_dims_and_mass_check_tools(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "leer_cotas", modelo="SOPORTE.DRW")
    by = {d["name"]: d for d in r["items"]}
    assert by["ad149"]["tolerancia"]["texto"] == "±15,2" and by["ad149"]["texto_legible"] == "R1519,17"
    assert by["d173"]["tolerancia"]["tipo"] == "general" and by["d173"]["tiene_tolerancia"] is False
    err, r = await call(srv, "propiedades_masa", modelo="SOPORTE.PRT")
    assert r["comprobacion_parametro_masa"]["estado"] == "difiere"  # 3,5 g frente a 2,7 g


@pytest.mark.anyio
async def test_format_file_handled(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "info_modelo", modelo="FORM_A3.FRM")
    assert not err and "unidades_longitud" not in r
    err, msg = await call(srv, "leer_cotas", modelo="FORM_A3.FRM")
    assert err and "no es una pieza" in msg


@pytest.mark.anyio
async def test_features_include_default_named(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    await call(srv, "listar_caracteristicas", modelo="SOPORTE.PRT")
    req = [q for q in fake.requests if q["command"] == "feature"][-1]["data"]
    assert req["inc_unnamed"] is True


# --- 0.4.0: diagnóstico, exportación con Creo 12, notas ----------------------

@pytest.mark.parametrize("msg, code", [
    ("[WinError 10061] No se puede establecer una conexión ya que el equipo de destino "
     "denegó expresamente dicha conexión", "creoson_parado"),
    ("('Connection aborted.', ConnectionResetError(10054, 'Se ha forzado la interrupción'))",
     "creoson_cortado"),
    ("HTTPConnectionPool(host='localhost', port=9056): Read timed out. (read timeout=60.0)",
     "creo_ocupado"),
    ("algo raro", "desconocido"),
])
def test_diagnose_real_errors(msg, code):
    c, hint = diagnose(msg)
    assert c == code and hint


@pytest.mark.anyio
async def test_estado_without_creoson_explains(tmp_path):
    s = Settings(creoson_host="127.0.0.1", creoson_port=1, audit_log=tmp_path / "a.jsonl", timeout_s=2)
    err, r = await call(build_server(s), "creo_estado")
    assert not err and r["creoson_responde"] is False
    assert r["diagnostico"] in ("creoson_parado", "creoson_inaccesible") and "CREOSON" in r["que_hacer"]


@pytest.mark.anyio
async def test_estado_creo_not_linked(tmp_path, fake):
    fake.creo_running = False
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "creo_estado")
    assert not err and r["creoson_responde"] and r["diagnostico"] == "creo_no_enlazado"
    assert "Creo" in r["que_hacer"] and "modelo_activo" not in r


@pytest.mark.anyio
async def test_estado_ok_has_diagnostico(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "creo_estado")
    assert not err and r["diagnostico"] == "ok"


@pytest.mark.anyio
async def test_export_uses_advanced_first(tmp_path, fake):
    fake.classic_export_aborts = True  # comportamiento visto en Creo 12.4.1.0
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "exportar_step", modelo="SOPORTE.PRT", nombre_fichero="soporte_v2")
    assert not err and r["metodo"] == "creo4_advanced" and "aviso" not in r
    reqs = [q["data"] for q in fake.requests if q["command"] == "interface"]
    assert len(reqs) == 1 and reqs[0]["advanced"] is True and reqs[0]["geom_flags"] == "solids"


@pytest.mark.anyio
async def test_export_falls_back_to_classic(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    orig = fake.respond

    def advanced_fails(cmd, fn, data):
        if (cmd, fn) == ("interface", "export_file") and data.get("advanced"):
            return {"status": {"error": True, "message": "advanced no disponible"}}
        return orig(cmd, fn, data)

    fake.respond = advanced_fails
    err, r = await call(srv, "exportar_step", modelo="SOPORTE.PRT", nombre_fichero="soporte_v3")
    assert not err and r["metodo"] == "clasico" and r["intentos_fallidos"][0]["metodo"] == "creo4_advanced"


@pytest.mark.anyio
async def test_export_abort_gives_hints(tmp_path, fake):
    fake.export_aborts = True
    _, _, srv = make(tmp_path, fake)
    err, msg = await call(srv, "exportar_step", modelo="SOPORTE.PRT", nombre_fichero="soporte_v4")
    assert err and "Abort" in msg and "escribir" in msg and "xtop.exe" in msg and "regenera" in msg


@pytest.mark.anyio
async def test_export_name_limit(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    # Caso real del 2026-11-20: 36 caracteres antes de la extensión.
    err, msg = await call(srv, "exportar_step", modelo="SOPORTE.PRT",
                          nombre_fichero="pieza_prueba1_validacion_20261120.stp")
    assert err and "31" in msg
    assert ("interface", "export_file") not in fake.commands


def test_default_step_name_fits_creo():
    from creo_mcp_iac.server import CREO_STEM_MAX, _step_filename
    name = _step_filename("prj110_soporte_entrada_espejo_nombre_largo.prt", None)
    stem = name.rsplit(".", 1)[0]
    assert len(stem) <= CREO_STEM_MAX and name.endswith(".stp") and stem.startswith("prj110")


@pytest.mark.anyio
async def test_leer_notas_decodes_and_never_selects(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "leer_notas", modelo="SOPORTE.DRW")
    assert not err and r["count"] == 4
    n2 = r["items"][1]
    assert r["items"][2]["value_expanded"] == "[rellenado]"  # caso real: cajetín de un proyecto
    assert r["items"][3]["value_expanded"] == "[símbolo:l]|0.06|A"  # marco GD&T real
    assert n2["value_expanded"] == "TOLERANCIA GENERAL ISO 2768-f / Ø4 H7" and "encoded" not in n2
    req = [q["data"] for q in fake.requests if q["command"] == "note"][-1]
    assert req["select"] is False and req["get_expanded"] is True


@pytest.mark.anyio
async def test_info_plano_lists_symbols(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "info_plano", plano="SOPORTE.DRW")
    assert not err and r["simbolos"]["items"][0]["symbol_name"] == "RUGOSIDAD"


# --- 0.5.0: planos nativos de Creo -------------------------------------------

from creo_mcp_iac.gateway import CAJETIN_PARAMS, WRITE_DRAWING, ScopeError  # noqa: E402

DRAW_TOOLS = {"crear_plano", "anadir_vista", "ajustar_vista", "borrar_vista", "configurar_hoja",
              "mostrar_cotas", "poner_nota", "poner_simbolo", "rellenar_cajetin", "regenerar_plano"}


async def tool_names(srv):
    async with create_connected_server_and_client_session(srv._mcp_server) as c:
        return {t.name: t for t in (await c.list_tools()).tools}


def test_write_functions_never_include_save_or_windchill():
    for f in WRITE_DRAWING:
        assert not f.startswith(("file_", "windchill", "creo_", "interface", "familytable", "feature"))
    assert "file_save" not in WRITE_DRAWING | ALLOWED
    assert not (WRITE_DRAWING & ALLOWED)


@pytest.mark.anyio
async def test_drawing_tools_hidden_by_default(tmp_path, fake):
    _, gw, srv = make(tmp_path, fake)
    assert not (DRAW_TOOLS & set(await tool_names(srv)))
    with pytest.raises(NotAllowedError, match="CREO_MCP_ALLOW_DRAWINGS"):
        gw.call("drawing_create", template="x", model="SOPORTE.PRT", drawing="SOPORTE_IA")
    assert fake.requests == []


@pytest.mark.anyio
async def test_drawing_tools_annotations(tmp_path, fake):
    _, _, srv = make(tmp_path, fake, drawings=True)
    tools = await tool_names(srv)
    assert DRAW_TOOLS <= set(tools)
    for n in DRAW_TOOLS:
        assert tools[n].annotations.readOnlyHint is False
    assert tools["rellenar_cajetin"].annotations.destructiveHint is True


def test_scope_existing_drawing_blocked(tmp_path, fake):
    _, gw, _ = make(tmp_path, fake, drawings=True)
    for func, kw in [("drawing_create_gen_view", {"drawing": "SOPORTE.DRW", "model_view": "FRONT",
                                                  "point": {"x": 0, "y": 0}}),
                     ("drawing_delete_view", {"drawing": "SOPORTE.DRW", "view": "FRONT"}),
                     ("note_set", {"file_": "SOPORTE.DRW", "name": "N1", "value": "x"}),
                     ("note_set", {"file_": "SOPORTE.PRT", "name": "N1", "value": "x"}),
                     ("parameter_set", {"file_": "SOPORTE.PRT", "name": "CODIGO_PLANO", "value": "x"}),
                     ("dimension_show", {"file_": "SOPORTE.PRT", "name": "d0"}),
                     ("drawing_regenerate", {})]:
        with pytest.raises(ScopeError):
            gw.call(func, **kw)
    assert fake.requests == []
    assert '"blocked_scope"' in (tmp_path / "audit.jsonl").read_text()


def test_parameter_set_only_cajetin_and_no_create(tmp_path, fake):
    _, gw, _ = make(tmp_path, fake, drawings=True)
    gw.register_drawing("soporte_ia.drw", "SOPORTE.PRT")
    for bad in ("PTC_MATERIAL_NAME", "DENSIDAD", "CODIGO*", "PTC_WM_REVISION"):
        with pytest.raises(ScopeError):
            gw.call("parameter_set", file_="SOPORTE.PRT", name=bad, value="x")
    gw.call("parameter_set", file_="SOPORTE.PRT", name="CODIGO_PLANO", value="X-1",
            designate=False, description="y")
    req = [q["data"] for q in fake.requests if q["command"] == "parameter"][-1]
    assert req["no_create"] is True and "designate" not in req and "description" not in req
    assert "DISEÑADO_POR" in CAJETIN_PARAMS


@pytest.mark.anyio
async def test_create_drawing_flow(tmp_path, fake):
    _, gw, srv = make(tmp_path, fake, drawings=True)
    async with create_connected_server_and_client_session(srv._mcp_server) as c:
        async def run(name, **a):
            r = await c.call_tool(name, a)
            t = r.content[0].text if r.content else ""
            return r.isError, (t if r.isError else json.loads(t))

        err, r = await run("crear_plano", modelo="SOPORTE.PRT", plantilla="iac_a3_pieza.drw",
                           nombre="soporte_ia", escala=0.5)
        assert not err, r
        assert r["plano"] == "soporte_ia.drw" and r["guardado"] is False
        req = [q["data"] for q in fake.requests if q["function"] == "create"][-1]
        assert req == {"template": "iac_a3_pieza", "model": "SOPORTE.PRT", "drawing": "soporte_ia",
                       "scale": 0.5, "display": True, "activate": True, "new_window": True}
        err, r = await run("anadir_vista", plano="soporte_ia", vista_modelo="FRONT", x=120, y=180,
                           nombre="ALZADO")
        assert not err and r["vistas"] == ["ALZADO"]
        err, r = await run("anadir_vista", plano="soporte_ia.drw", vista_padre="ALZADO", x=120, y=80,
                           nombre="PLANTA")
        assert not err and r["vistas"] == ["ALZADO", "PLANTA"]
        err, _ = await run("anadir_vista", plano="soporte_ia", x=1, y=1)
        assert err
        err, r = await run("mostrar_cotas", plano="soporte_ia", cotas=["d0", "d1", "mal nombre"])
        assert not err and r["mostradas"] == ["d0", "d1"] and "mal nombre" in r["errores"]
        shows = [q["data"] for q in fake.requests if q["command"] == "dimension"
                 and q["function"] == "show"]
        assert all(q["file"] == "SOPORTE.PRT" for q in shows)
        err, _ = await run("poner_nota", plano="soporte_ia", nombre="NOTA_IA_1",
                           texto="1- Anodizado negro", x=300, y=60)
        assert not err
        err, r = await run("rellenar_cajetin", plano="soporte_ia",
                           parametros={"TOLERANCIA": "FINA", "DISEÑADO_POR": "x"})
        assert not err and r["cambios"]["TOLERANCIA"]["ahora"] == "FINA"
        err, msg = await run("rellenar_cajetin", plano="soporte_ia", parametros={"CODIGO_PLANO": "X"})
        assert err and "No existen" in msg  # el asistente no crea parámetros
        err, msg = await run("rellenar_cajetin", plano="soporte_ia", parametros={"PTC_MATERIAL_NAME": "X"})
        assert err and "Fuera del cajetín" in msg
        # Un plano existente no se toca.
        err, msg = await run("poner_nota", plano="SOPORTE.DRW", nombre="N", texto="x", x=0, y=0)
        assert err and "no es un plano creado" in msg
        err, msg = await run("crear_plano", modelo="SOPORTE.PRT", plantilla="iac_a3_pieza",
                             nombre="soporte_ia")
        assert err and "Ya hay" in msg
        err, msg = await run("crear_plano", modelo="SOPORTE.PRT", plantilla="..\\x")
        assert err
    cmds = fake.commands
    assert ("file", "save") not in cmds and not any(c[0] == "windchill" for c in cmds)
    mutated = {q["data"].get("drawing") or q["data"].get("file") for q in fake.requests
               if (q["command"], q["function"]) in {("drawing", "create_gen_view"), ("note", "set"),
                                                      ("drawing", "create_proj_view")}}
    assert mutated == {"soporte_ia.drw"}


@pytest.mark.anyio
async def test_export_pdf(tmp_path, fake):
    s, _, srv = make(tmp_path, fake)
    err, r = await call(srv, "exportar_pdf", plano="SOPORTE.DRW", nombre_fichero="soporte_plano")
    assert not err and r["fichero"] == "soporte_plano.pdf" and r["bytes"] > 0 and "aviso" not in r
    req = [q["data"] for q in fake.requests if q["function"] == "export_pdf"][-1]
    assert req["use_drawing_settings"] is True and req["dirname"] == str(s.export_dir)
    err, msg = await call(srv, "exportar_pdf", plano="SOPORTE.PRT")
    assert err


@pytest.mark.anyio
async def test_config_only_allowed_options(tmp_path, fake):
    fake.template_dir = tmp_path
    (tmp_path / "iac_a3_pieza.drw.2").write_text("x")
    (tmp_path / "form_iac_a-3.frm.1").write_text("x")
    _, gw, srv = make(tmp_path, fake)
    with pytest.raises(NotAllowedError):
        gw.call("creo_get_config", name="license_server")
    err, r = await call(srv, "listar_plantillas_plano")
    assert not err and r["plantillas"][str(tmp_path)] == ["iac_a3_pieza.drw"]
    assert r["formatos"][str(tmp_path)] == ["form_iac_a-3.frm"]


# --- 0.5.1: versión de Creo y diálogos al crear planos ------------------------

@pytest.mark.anyio
async def test_connect_sets_creo_version(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    err, _ = await call(srv, "listar_modelos")
    assert not err and fake.creo_version == 12
    reqs = [q for q in fake.requests if q["function"] == "set_creo_version"]
    assert len(reqs) == 1 and fake.commands.index(("creo", "set_creo_version")) == 1


@pytest.mark.anyio
async def test_windchill_template_dir(tmp_path, fake):
    _, _, srv = make(tmp_path, fake)
    orig = fake.respond

    def wt(cmd, fn, data):
        if (cmd, fn) == ("creo", "get_config"):
            v = {"template_drawing": ["wtpub://Servidor/Bibliotecas/FORMATOS/plantilla_plano.drw"],
                 "pro_format_dir": ["wtpub://Servidor/Bibliotecas/FORMATOS/"]}
            return {"status": {"error": False}, "sessionId": "S1",
                    "data": {"values": v.get(data["name"], [])}}
        return orig(cmd, fn, data)

    fake.respond = wt
    err, r = await call(srv, "listar_plantillas_plano")
    assert not err and r["plantilla_por_defecto"] == ["plantilla_plano"]
    assert "Windchill" in r["plantillas"]["wtpub://Servidor/Bibliotecas/FORMATOS"]


@pytest.mark.anyio
async def test_create_drawing_timeout_then_adopt(tmp_path, fake):
    s, gw, srv = make(tmp_path, fake, drawings=True)
    orig = fake.respond

    def dialog(cmd, fn, data):
        r = orig(cmd, fn, data)
        if (cmd, fn) == ("drawing", "create"):
            import time
            time.sleep(6)  # Creo bloqueado por un diálogo: el servidor agota su espera (5 s)
        return r

    fake.respond = dialog
    async with create_connected_server_and_client_session(srv._mcp_server) as c:
        r = await c.call_tool("crear_plano", {"modelo": "SOPORTE.PRT", "plantilla": "p", "nombre": "sop_ia"})
        assert r.isError and "diálogo" in r.content[0].text
        fake.respond = orig
        r = await c.call_tool("crear_plano", {"modelo": "SOPORTE.PRT", "plantilla": "p", "nombre": "sop_ia"})
        assert not r.isError and "intento anterior" in r.content[0].text
        r = await c.call_tool("poner_nota", {"plano": "sop_ia", "nombre": "N1", "texto": "x", "x": 1, "y": 1})
        assert not r.isError
        # Otro modelo con el mismo nombre de plano no se adopta.
        r = await c.call_tool("crear_plano", {"modelo": "TORNILLO.PRT", "plantilla": "p", "nombre": "sop_ia"})
        assert r.isError and "Ya hay" in r.content[0].text


# --- formatos IAC -------------------------------------------------------------

@pytest.mark.anyio
async def test_create_with_iac_format_and_fallback_dir(tmp_path, fake):
    _, _, srv = make(tmp_path, fake, drawings=True)
    orig = fake.respond

    def fmt(cmd, fn, data):
        if (cmd, fn) == ("drawing", "set_sheet_format") and not data.get("dirname"):
            return {"status": {"error": True, "message": "Could not open file 'form_iac_a-2.frm'"}}
        if (cmd, fn) == ("creo", "get_config") and data["name"] == "pro_format_dir":
            return {"status": {"error": False}, "sessionId": "S1",
                    "data": {"values": ["wtpub://Servidor/Bibliotecas/FORMATOS/"]}}
        return orig(cmd, fn, data)

    fake.respond = fmt
    err, r = await call(srv, "crear_plano", modelo="SOPORTE.PRT", plantilla="plantilla_plano",
                        nombre="sop_a2", formato="a2")
    assert not err and r["formato"] == "form_iac_a-2"
    reqs = [q["data"] for q in fake.requests if q["function"] == "set_sheet_format"]
    assert reqs[0]["file"] == "form_iac_a-2" and reqs[-1]["dirname"] == "wtpub://Servidor/Bibliotecas/FORMATOS/"
    err, r = await call(srv, "crear_plano", modelo="SOPORTE.PRT", plantilla="plantilla_plano",
                        nombre="sop_x", formato="../malo")
    assert not err and r["formato"].startswith("no se pudo poner")


def test_export_dir_auto(tmp_path):
    s = Settings.from_env({"CREO_MCP_EXPORT_DIR": "auto", "LOCALAPPDATA": str(tmp_path),
                           "CREO_MCP_AUDIT_LOG": str(tmp_path / "a.jsonl")})
    assert s.export_dir == (tmp_path / "creo_mcp_iac" / "export").resolve() and s.export_dir.is_dir()
