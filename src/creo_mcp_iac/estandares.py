"""Revisión automática de estándares de los modelos de un conjunto (solo lectura).

Reglas (códigos R01-R10), configurables en `estandares_iac.json` o en el fichero que
indique CREO_MCP_ESTANDARES:

R01 Parámetros obligatorios vacíos (piezas y conjuntos propios).
R02 Formato de CODIGO_PLANO (por proyecto o general).
R03 CODIGO_PLANO repetido en modelos distintos.
R04 PROYECTO distinto del de referencia.
R05 Un mismo valor escrito de varias formas (FABRICACION / FABRICACIÓN).
R06 «Válido para fabricación» con el modelo en un estado de Windchill no liberado.
R07 Material sin asignar o con restricciones para vacío o criogenia.
R08 Unidad de longitud distinta de la del departamento.
R09 Revisión del plano distinta de la del modelo (planos abiertos en la sesión).
R10 Parámetro MASA distinto de la masa calculada.

La tornillería normalizada y el contenido de los conjuntos comerciales no se revisan.
No accede a Creo: recibe los datos ya leídos.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

from . import xlsx
from .bom_excel import celda, normalizar, preparar

REGLAS = OrderedDict([
    ("R01", "Parámetros obligatorios"), ("R02", "Formato de CODIGO_PLANO"), ("R03", "CODIGO_PLANO repetido"),
    ("R04", "PROYECTO distinto"), ("R05", "Valores escritos de varias formas"),
    ("R06", "Válido para fabricación sin liberar"), ("R07", "Material"), ("R08", "Unidades"),
    ("R09", "Revisión plano / modelo"), ("R10", "Parámetro MASA"),
])

# Parámetros que hay que leer para estas reglas
NOMBRES = ["CODIGO_PLANO", "PROYECTO", "SUBSISTEMA", "PIEZA", "PTC_MATERIAL_NAME", "VALIDO_PARA", "TOLERANCIA",
           "PROTECCION_SUPERFICIAL", "TRATAMIENTO", "RUGOSIDAD", "CANTIDAD_FABRICADA", "FUENTE", "MASA",
           "MASA_UNIT", "PTC_UNITS_LENGTH", "PTC_UNITS_MASS", "PTC_WM_REVISION", "PTC_WM_VERSION",
           "PTC_WM_LIFECYCLE_STATE", "DESCRIPCION", "DESCRIPTION", "PTC_COMMON_NAME"]


def cargar_reglas(ruta: str | None = None) -> dict[str, Any]:
    ruta = ruta or os.environ.get("CREO_MCP_ESTANDARES") or str(Path(__file__).with_name("estandares_iac.json"))
    with open(ruta, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_fichero"] = ruta
    return cfg


def _plano(s: Any) -> str:
    """Mayúsculas, sin acentos ni espacios repetidos."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()


def _v(p: dict[str, Any], nombre: str) -> Any:
    return normalizar(p.get(nombre, {}).get("value"))


def revisar(bom: dict[str, Any], parametros: dict[str, dict[str, Any]], reglas: dict[str, Any],
            vacio: bool = True, criogenia: bool = False,
            masas: dict[str, dict[str, Any]] | None = None,
            planos: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """`masas`: CLAVE -> {"kg": masa calculada, "param_kg": MASA en kg, "dif_pct"}.
    `planos`: CLAVE del modelo -> {"plano": nombre, "revision": revisión del plano}."""
    d = preparar(bom, parametros)
    inc: list[dict[str, Any]] = []

    def add(modelo: str, regla: str, gravedad: str, detalle: str) -> None:
        inc.append(OrderedDict([("modelo", modelo), ("regla", regla), ("nombre", REGLAS[regla]),
                                ("gravedad", gravedad), ("detalle", detalle)]))

    comercial = {c.upper() for c in reglas["codigo_plano"]["comercial"]}
    propios = OrderedDict()
    comerciales = OrderedDict()  # solo para la regla de materiales: un comercial también va al vacío
    for k, pz in d["piezas"].items():
        p = parametros.get(k, {})
        cod = str(_v(p, "CODIGO_PLANO")).upper()
        if "comercial" in pz["tipo"] or cod in comercial:
            comerciales[k] = (pz, p)
            continue
        if not any(c in p for c in ("CODIGO_PLANO", "PROYECTO", "PIEZA")):
            add(pz["modelo"], "R01", "info", "Sin parámetros del cajetín IAC: ¿pieza comercial? Si lo es, "
                "poner CODIGO_PLANO = NA en su conjunto o en la pieza.")
            continue
        propios[k] = (pz, p)

    # R01 obligatorios
    for k, (pz, p) in propios.items():
        faltan = [c for c in reglas["obligatorios"]
                  if not (c == "PTC_MATERIAL_NAME" and pz["tipo"].startswith("ASM")) and not _v(p, c)]
        if not faltan:
            continue
        if all(not _v(p, c) for c in ("CODIGO_PLANO", "PROYECTO")):
            # sin código ni proyecto: casi siempre un comercial o una pieza interna de uno
            add(pz["modelo"], "R01", "info", f"Vacío: {', '.join(faltan)}. Si es comercial (o está dentro de un "
                "comercial), poner CODIGO_PLANO = NA en el conjunto comercial y deja de revisarse.")
        else:
            add(pz["modelo"], "R01", "aviso", f"Vacío: {', '.join(faltan)}.")

    # R04 proyecto de referencia (el más repetido) y R02 formato del código
    cuenta = Counter(_v(p, "PROYECTO") for _, p in propios.values())
    cuenta.pop("", None)
    ref = cuenta.most_common(1)[0][0] if cuenta else ""
    patrones = {_plano(kp): re.compile(v) for kp, v in reglas["codigo_plano"]["por_proyecto"].items()}
    general = re.compile(reglas["codigo_plano"]["general"])
    for k, (pz, p) in propios.items():
        pr, cod = _v(p, "PROYECTO"), str(_v(p, "CODIGO_PLANO"))
        if ref and pr and pr != ref:
            add(pz["modelo"], "R04", "aviso", f"PROYECTO «{pr}»; en el resto del conjunto, «{ref}».")
        if cod:
            pat = patrones.get(_plano(pr or ref), general)
            if not pat.match(cod):
                add(pz["modelo"], "R02", "aviso", f"CODIGO_PLANO «{cod}» no sigue el formato {pat.pattern}.")

    # R03 repetidos
    por_cod: dict[str, list[str]] = {}
    for k, (pz, p) in propios.items():
        c = str(_v(p, "CODIGO_PLANO")).upper()
        if c:
            por_cod.setdefault(c, []).append(pz["modelo"])
    for c, mods in por_cod.items():
        if len(mods) > 1:
            add(", ".join(mods), "R03", "aviso", f"Mismo CODIGO_PLANO {c} en {len(mods)} modelos distintos.")

    # R05 valores escritos de varias formas
    for campo in reglas["campos_uniformes"]:
        formas: dict[str, Counter] = {}
        for _, p in propios.values():
            v = _v(p, campo)
            if v:
                formas.setdefault(_plano(v), Counter())[str(v)] += 1
        for _, c in formas.items():
            if len(c) > 1:
                add("(varios)", "R05", "info", f"{campo} escrito de {len(c)} formas: "
                    + "; ".join(f"«{v}» ({n})" for v, n in c.most_common()) + ".")

    # R06 válido para fabricación sin liberar
    fab = {_plano(x) for x in reglas["valido_para_fabricacion"]}
    no_lib = [_plano(x) for x in reglas["estados_no_liberados"]]
    for k, (pz, p) in propios.items():
        est = _plano(_v(p, "PTC_WM_LIFECYCLE_STATE"))
        if _plano(_v(p, "VALIDO_PARA")) in fab and est and any(x in est for x in no_lib):
            add(pz["modelo"], "R06", "aviso", f"VALIDO_PARA «{_v(p, 'VALIDO_PARA')}» pero el modelo está en "
                f"{_v(p, 'PTC_WM_LIFECYCLE_STATE')} (versión {_v(p, 'PTC_WM_VERSION') or '-'}).")

    # R07 materiales
    ambitos = {"vacio"} if vacio else set()
    if criogenia:
        ambitos.add("criogenia")
    restr = [(re.compile(m["patron"], re.I), m["ambito"], m["motivo"]) for m in reglas["materiales"]]
    for k, (pz, p) in list(propios.items()) + list(comerciales.items()):
        if pz["tipo"].startswith("ASM"):
            continue
        mat = str(_v(p, "PTC_MATERIAL_NAME"))
        if not mat:
            continue  # ya en R01
        for rx, amb, motivo in restr:
            if amb in ambitos and rx.search(mat):
                add(pz["modelo"], "R07", "aviso", f"Material {mat} ({'vacío' if amb == 'vacio' else 'criogenia'})"
                    f"{' en un comercial' if k in comerciales else ''}: {motivo}")

    # R08 unidades
    ul = reglas.get("unidad_longitud")
    for k, (pz, p) in propios.items():
        u = str(_v(p, "PTC_UNITS_LENGTH"))
        if ul and u and u.lower() != ul.lower():
            add(pz["modelo"], "R08", "aviso", f"Unidad de longitud {u} (el departamento usa {ul}).")

    # R09 revisión plano / modelo
    for k, info in (planos or {}).items():
        if k not in propios:
            continue
        pz, p = propios[k]
        rm, rp = str(_v(p, "PTC_WM_REVISION")), str(info.get("revision") or "")
        if rm and rp and rm != rp:
            add(pz["modelo"], "R09", "aviso", f"El plano {info['plano']} está en la revisión {rp} y el modelo en la {rm}.")

    # R10 MASA
    for k, m in (masas or {}).items():
        if k in propios and m.get("dif_pct") is not None and abs(m["dif_pct"]) > 0.1:
            add(propios[k][0]["modelo"], "R10", "info", f"Parámetro MASA {m['param_kg']:.4f} kg; calculada "
                f"{m['kg']:.4f} kg ({m['dif_pct']:+.1f} %).")

    orden = {"aviso": 0, "info": 1}
    inc.sort(key=lambda x: (orden[x["gravedad"]], x["regla"], x["modelo"]))
    resumen = OrderedDict((r, sum(1 for x in inc if x["regla"] == r)) for r in REGLAS)
    return {"conjunto": d["conjunto"], "revisados": len(propios), "proyecto_referencia": ref,
            "planos_comparados": len([k for k in (planos or {}) if k in propios]),
            "incumplimientos": inc, "por_regla": resumen,
            "ambitos": sorted(ambitos)}


def exportar(r: dict[str, Any], reglas: dict[str, Any], ruta: Path, origen: dict[str, Any]) -> None:
    cab = lambda cols: [(c, xlsx.CABECERA) for c in cols]  # noqa: E731
    res = [cab(["Regla", "Descripción", "Incumplimientos"])]
    for code, n in r["por_regla"].items():
        res.append([code, REGLAS[code], n])
    res += [[], ["Conjunto", celda(r["conjunto"])], ["Modelos propios revisados", r["revisados"]],
            ["Proyecto de referencia", celda(r["proyecto_referencia"])],
            ["Planos comparados (abiertos en la sesión)", r["planos_comparados"]],
            ["Materiales revisados para", ", ".join(r["ambitos"]) or "-"],
            ["Reglas", celda(reglas.get("_fichero", ""))], ["Generado por", origen.get("servidor", "")],
            ["Nota", "Reglas y listas de materiales de partida: las revisa el departamento."]]
    inc = [cab(["Modelo", "Regla", "Descripción", "Gravedad", "Detalle"])]
    inc += [[celda(x["modelo"]), x["regla"], x["nombre"], x["gravedad"], celda(x["detalle"])]
            for x in r["incumplimientos"]]
    reg = [cab(["Clave", "Valor"])]
    for k, v in reglas.items():
        if not k.startswith("_"):
            reg.append([k, celda(json.dumps(v, ensure_ascii=False))])
    xlsx.escribir(ruta, [
        {"nombre": "Resumen", "filas": res, "anchos": [44, 40, 16]},
        {"nombre": "Incumplimientos", "filas": inc, "fijar_cabecera": True, "autofiltro": len(inc),
         "anchos": [40, 7, 32, 9, 110]},
        {"nombre": "Reglas", "filas": reg, "anchos": [26, 140]},
    ])
