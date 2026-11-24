"""Holguras mínimas y en frío (sin Creo)."""

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))
from test_nativo import Runner, con_nativo  # noqa: E402
from test_server import call, fake  # noqa: E402,F401

from creo_mcp_iac import holguras as hg  # noqa: E402

IDENT = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def mover(x=0.0, y=0.0, z=0.0):
    return IDENT[:12] + [x, y, z, 1]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_tabla_y_contraccion():
    t = hg.cargar_tabla()
    nombre = lambda n: (hg.buscar_material(t, n) or {}).get("nombre")  # noqa: E731
    assert nombre("AL6061") == nombre("AL_6082_T6") == "Aluminio 6061-T6"
    assert nombre("SS304") == nombre("AISI_316L") == "Acero inoxidable AISI 304/316"
    assert nombre("CUBE2") == "Cobre-berilio" and nombre("CU_OFHC") == "Cobre OFHC/ETP"
    assert nombre("TI6AL4V") == "Ti-6Al-4V" and nombre("G10") == "G-10 (en el plano del laminado)"
    assert nombre("G10_NORMAL") == "G-10 (normal al laminado)"
    assert nombre("Ti-6Al-4V") == "Ti-6Al-4V" and nombre("STEEL") is None and nombre(None) is None
    al = hg.buscar_material(t, "AL6061")
    assert hg.contraccion(al, 293) == pytest.approx(0, abs=1e-9)
    assert hg.contraccion(al, 77) == pytest.approx(-388.7e-5, abs=0.2e-5)
    assert hg.contraccion(al, 4) == hg.contraccion(al, 18)                       # constante bajo el rango
    cu = hg.buscar_material(t, "OFHC")
    assert hg.contraccion(cu, 77) == pytest.approx(-304e-5) and hg.contraccion(cu, 100) == pytest.approx(
        (-304 + 56 * 23 / 53) * 1e-5)
    ss = hg.buscar_material(t, "SS304")
    assert hg.contraccion(ss, 4) == pytest.approx(-297.0e-5, abs=0.3e-5)


# A (aluminio) es un cubo de 100 mm en el origen; B (inoxidable) empieza en x = 101 (1 mm de holgura);
# C (aluminio) a 0,05 mm de B por arriba; D es un tornillo pegado a A; E está lejos.
POS = {"ok": True, "version_tk": "0.6.0", "modelo": {"nombre": "TOP", "tipo": "ASM"}, "unidad_longitud": "mm",
       "componentes": [
           {"modelo": "SUB", "tipo": "ASM", "ruta": [9], "matriz": IDENT},
           {"modelo": "A", "tipo": "PRT", "ruta": [1], "matriz": IDENT, "material": "AL6061", "caja": [0, 0, 0, 100, 100, 100]},
           {"modelo": "B", "tipo": "PRT", "ruta": [9, 2], "matriz": mover(101), "material": "SS304",
            "caja": [0, 0, 0, 50, 50, 50]},
           {"modelo": "C", "tipo": "PRT", "ruta": [3], "matriz": mover(101, 0, 50.05), "material": None,
            "caja": [0, 0, 0, 50, 50, 10]},
           {"modelo": "DIN912-M6X20", "tipo": "PRT", "ruta": [4], "matriz": mover(0, 0, 100), "material": "SS304",
            "caja": [-5, -5, 0, 5, 5, 20]},
           {"modelo": "E", "tipo": "PRT", "ruta": [5], "matriz": mover(500), "material": "AL6061",
            "caja": [0, 0, 0, 10, 10, 10]}]}

# respuesta de Creo para los pares candidatos (en el orden en que se piden)
def respuesta(pares):
    out = []
    for n, (a, b) in enumerate(pares):
        clave = (tuple(a), tuple(b))
        if clave == ((1,), (9, 2)):
            out.append({"n": n, "distancia": 1.0, "interferencia": False, "puntos": [[100, 20, 20], [101, 20, 20]]})
        elif clave == ((9, 2), (3,)):
            out.append({"n": n, "distancia": 0.05, "interferencia": False, "puntos": [[120, 20, 50], [120, 20, 50.05]]})
        elif clave == ((1,), (3,)):
            out.append({"n": n, "distancia": 1.0, "interferencia": False, "puntos": [[100, 20, 60], [101, 20, 60]]})
        else:
            out.append({"n": n, "error": -1})
    return {"ok": True, "pares": out, "calculados": len(out), "fallidos": 0, "truncado": False, "segundos": 1.0}


def test_candidatos_y_frio():
    lista = hg.piezas(POS)
    assert [p["pieza"] for p in lista] == ["A.PRT #1", "B.PRT #1", "C.PRT #1", "DIN912-M6X20.PRT #1", "E.PRT #1"]
    assert lista[1]["caja"] == ((101, 0, 0), (151, 50, 50))
    cand, total = hg.candidatos(lista, 2.0, excluir=lambda n: n.startswith("DIN"))
    assert [(i, j) for i, j, _ in cand] == [(1, 2), (0, 1), (0, 2)] and total == 3
    cand2, _ = hg.candidatos(lista, 2.0)
    assert len(cand2) == 4                                       # con el tornillo (toca A)
    res = respuesta([[lista[i]["ruta"], lista[j]["ruta"]] for i, j, _ in cand])
    t = hg.cargar_tabla()
    # sin temperatura: B-C insuficiente (0,05 < 0,5)
    r = hg.revisar_holguras(lista, cand, res, 2.0, 0.5)
    assert [f["estado"] for f in r["filas"]] == ["insuficiente", "correcta", "correcta"]
    assert "estado_frio" not in r["filas"][0]
    # a 77 K, punto fijo en el origen: el aluminio (A) se contrae más que el inoxidable (B): la holgura A-B crece
    r = hg.revisar_holguras(lista, cand, res, 2.0, 0.5, temperatura=77, materiales={"C.PRT": "Aluminio 6061-T6"},
                            tabla=t)
    f = {(x["a"], x["b"]): x for x in r["filas"]}
    ab = f[("A.PRT #1", "B.PRT #1")]
    e_al, e_ss = ab["contraccion_a"], ab["contraccion_b"]
    assert ab["distancia_frio"] == pytest.approx(1 + e_ss * 101 - e_al * 100, abs=1e-4) and ab["cambio"] > 0.1
    # B-C: C (Al) encima de B (SS) en z: C baja más que B: 0,05 + (-0,00389·50,05 + 0,0028·50) < 0
    bc = f[("B.PRT #1", "C.PRT #1")]
    assert bc["estado_frio"] == "se cierra en frío" and r["filas"][0] is bc
    # mismo material: holgura·(1+ε), sea cual sea el punto fijo
    ac = f[("A.PRT #1", "C.PRT #1")]
    assert ac["distancia_frio"] == pytest.approx(1 * (1 + e_al), abs=1e-4)
    r2 = hg.revisar_holguras(lista, cand, res, 2.0, 0.5, temperatura=77, materiales={"C.PRT": "Aluminio 6061-T6"},
                             punto_fijo=(500, 500, 500), tabla=t)
    ac2 = next(x for x in r2["filas"] if x["b"] == "C.PRT #1" and x["a"] == "A.PRT #1")
    assert ac2["distancia_frio"] == pytest.approx(ac["distancia_frio"], abs=1e-6)
    # sin material en C
    r3 = hg.revisar_holguras(lista, cand, res, 2.0, 0.5, temperatura=77, tabla=t)
    assert any("C.PRT (sin material)" in a for a in r3["avisos"])
    assert sum(1 for x in r3["filas"] if x["estado_frio"] == "sin datos de contracción") == 2


@pytest.mark.anyio
async def test_herramienta_holguras(tmp_path, fake):
    fake.files = ["TOP.ASM"]
    fake.active = "TOP.ASM"

    class R(Runner):
        def __call__(self, args, **kw):
            if args[1] == "holguras":
                lineas = Path(args[args.index("--pares") + 1]).read_text(encoding="ascii").splitlines()
                self.respuestas["holguras"] = respuesta(
                    [[list(map(int, a.split("/"))) for a in x.split()] for x in lineas])
            return super().__call__(args, **kw)

    runner = R({"posiciones": POS})
    s, gw, srv = con_nativo(tmp_path, fake, runner)
    err, r = await call(srv, "holguras_creo", temperatura=77, materiales={"C.PRT": "Aluminio 6061-T6"})
    assert not err, r
    assert [x[1] for x in runner.llamadas] == ["posiciones", "holguras"]
    assert r["pares_candidatos"] == 3 and r["por_estado"] == {"insuficiente": 1, "correcta": 2}
    assert r["problemas"][0]["estado_frio"] == "se cierra en frío"
    assert r["materiales"]["B.PRT"]["material_tabla"].startswith("Acero inoxidable")
    wb = load_workbook(s.export_dir / r["fichero"])
    assert wb.sheetnames == ["Resumen", "Holguras", "Materiales"] and wb["Holguras"].max_row == 4
    err, msg = await call(srv, "holguras_creo", temperatura=77, materiales={"C.PRT": "Unobtainium"})
    assert err and "no está en la tabla" in msg


def test_punto_fijo_de_la_seleccion():
    sel = {"seleccion": [{"tipo_elemento": 5, "id": 926, "modelo": "B", "tipo_modelo": "PRT", "ruta": [9, 2],
                          "punto": [1, 2, 3], "cara": {"caja_min": [0, 0, 0], "caja_max": [50, 0, 50]}}]}
    p, que = hg.punto_de_seleccion(sel, POS)
    assert p == (126, 0, 25) and "B.PRT" in que and "cara" in que
    sel["seleccion"][0].pop("cara")
    assert hg.punto_de_seleccion(sel, POS)[0] == (102, 2, 3)
    with pytest.raises(ValueError, match="selecciona"):
        hg.punto_de_seleccion({"seleccion": [{"ruta": [77], "punto": [0, 0, 0]}]}, POS)


def test_ajuste_concentrico():
    # eje de inoxidable (R 3) en un separador de aluminio (R 3,025), lejos del punto fijo
    lista = [{"pieza": "EJE.PRT #1", "modelo": "EJE.PRT", "ruta": [1], "material": "SS304", "caja": None},
             {"pieza": "SEP.PRT #1", "modelo": "SEP.PRT", "ruta": [2], "material": "AL6061", "caja": None}]
    res = {"pares": [{"n": 0, "distancia": 0.025, "interferencia": False,
                      "puntos": [[103, 0, 50], [103.025, 0, 50]],
                      "cilindro_a": {"radio": 3.0, "eje": [100, 0, 0, 0, 0, 1]},
                      "cilindro_b": {"radio": 3.025, "eje": [100, 0, 10, 0, 0, -1]}}]}
    t = hg.cargar_tabla()
    f = hg.revisar_holguras(lista, [(0, 1, 0.0)], res, 1.0, 0.01, temperatura=130, tabla=t)["filas"][0]
    e_ss, e_al = f["contraccion_a"], f["contraccion_b"]
    assert f["ajuste"] == "concéntrico" and f["radio_exterior"] == 3.025
    assert f["distancia_frio"] == pytest.approx(0.025 + e_al * 3.025 - e_ss * 3.0, abs=1e-4)
    assert f["estado_frio"] == "correcta"
    # el mismo par visto como caras planas daría otra cosa (depende del punto fijo)
    res["pares"][0].pop("cilindro_a")
    res["pares"][0].pop("cilindro_b")
    g = hg.revisar_holguras(lista, [(0, 1, 0.0)], res, 1.0, 0.01, temperatura=130, tabla=t)["filas"][0]
    assert "ajuste" not in g and g["distancia_frio"] != f["distancia_frio"]
    # dos ejes paralelos uno al lado del otro no son un ajuste concéntrico
    assert hg.concentrico({"cilindro_a": {"radio": 3, "eje": [0, 0, 0, 0, 0, 1]},
                           "cilindro_b": {"radio": 3, "eje": [6.5, 0, 0, 0, 0, 1]}}, 0.5) is None


def test_concentrico_con_un_solo_cilindro():
    # el punto del separador cae en su arista: solo hay cilindro del eje (R 3)
    r = {"puntos": [[103, 0, 21.4], [103.025, 0, 21.4]], "cilindro_a": {"radio": 3.0, "eje": [100, 0, 0, 0, 0, 1]}}
    assert hg.concentrico(r, 0.025) == pytest.approx(("b", 3.025, 3.0))
    r2 = {"puntos": [[103.025, 0, 5], [103, 0, 5]], "cilindro_a": {"radio": 3.025, "eje": [100, 0, 0, 0, 0, 1]}}
    assert hg.concentrico(r2, 0.025) == pytest.approx(("a", 3.025, 3.0))
    # el otro punto no está a la distancia que explica la holgura: no es concéntrico
    r3 = {"puntos": [[103, 0, 5], [103, 0.5, 5.4]], "cilindro_a": {"radio": 3.0, "eje": [100, 0, 0, 0, 0, 1]}}
    assert hg.concentrico(r3, 0.4) is None
