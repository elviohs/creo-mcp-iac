"""Presupuesto de masas de un conjunto (solo lectura).

Recorre el árbol del BOM y toma como «hojas de masa» las piezas y los conjuntos
comerciales (CODIGO_PLANO = NA, cuya masa se toma entera). Con la masa de cada
hoja calculada por Creo (`file_massprops`) arma un Excel con:

- Resumen: masa total según Creo y según la suma de piezas, CdG e inercias del conjunto.
- Por subconjunto: subtotales de cada componente de primer nivel.
- Piezas: cantidad, masa unitaria y total, margen editable y masa con margen (fórmulas).
- Avisos: sin material o sin densidad, parámetro MASA distinto de la masa calculada.

No accede a Creo: recibe los datos ya leídos.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import xlsx
from .bom_excel import celda, clasificar_tornilleria, normalizar
from .formato import _MASS_TO_KG, check_mass_param

COMERCIAL = ("NA", "N/A", "N.A.", "COMERCIAL")
SUELTAS = "(piezas sueltas del conjunto principal)"


def _codigo(p: dict[str, Any]) -> str:
    return str(normalizar(p.get("CODIGO_PLANO", {}).get("value"))).upper()


def hojas(bom: dict[str, Any], parametros: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Hojas de masa con su cantidad total y su reparto por componente de primer nivel.

    Devuelve {"hojas": {CLAVE: {modelo, tipo, cantidad, grupos: {grupo: cant}}},
              "grupos": [modelos de primer nivel en orden]}.
    """
    out: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    grupos: list[str] = []
    pila: list[tuple[int, int, bool]] = []  # (nivel, multiplicador acumulado, saltar_hijos)
    grupo_actual = None
    for f in bom["indented"]:
        nivel, k = f["level"], f["file"].upper()
        while pila and pila[-1][0] >= nivel:
            pila.pop()
        if any(s for _, _, s in pila):
            continue  # dentro de un comercial o de una hoja
        mult = (pila[-1][1] if pila else 1) * f["qty"]
        if nivel == 1:
            # Las piezas sueltas del conjunto principal (tornillos, cables...) van en un solo grupo
            grupo_actual = f["file"] if f["type"] == "ASM" else SUELTAS
            if grupo_actual not in grupos:
                grupos.append(grupo_actual)
        comercial = f["type"] == "ASM" and _codigo(parametros.get(k, {})) in COMERCIAL
        es_hoja = f["type"] == "PRT" or comercial
        pila.append((nivel, mult, es_hoja))
        if not es_hoja:
            continue
        h = out.setdefault(k, {"modelo": f["file"], "tipo": f["type"] + (" comercial" if comercial else ""),
                               "cantidad": 0, "grupos": OrderedDict()})
        h["cantidad"] += mult
        h["grupos"][grupo_actual] = h["grupos"].get(grupo_actual, 0) + mult
    return {"hojas": out, "grupos": grupos}


def _kg(masa: Any, unidad: Any) -> float | None:
    u = str(unidad or "").strip().lower()
    if masa is None or u not in _MASS_TO_KG:
        return None
    return float(masa) * _MASS_TO_KG[u]


def preparar(bom: dict[str, Any], parametros: dict[str, dict[str, Any]],
             masas: dict[str, dict[str, Any]], conjunto: dict[str, Any]) -> dict[str, Any]:
    """`masas`: CLAVE -> {"props": file_massprops, "unidad_masa": str, "error": str?}.
    `conjunto`: {"props", "unidad_masa", "unidad_longitud", "si"} del conjunto completo."""
    h = hojas(bom, parametros)
    filas, avisos = [], []
    sub: "OrderedDict[str, float]" = OrderedDict((g, 0.0) for g in h["grupos"])
    suma = 0.0
    for k, hoja in h["hojas"].items():
        p = parametros.get(k, {})
        m = masas.get(k, {})
        props = m.get("props") or {}
        unidad = m.get("unidad_masa")
        kg = _kg(props.get("mass"), unidad)
        dens = props.get("density")
        material = normalizar(p.get("PTC_MATERIAL_NAME", {}).get("value"))
        if hoja["tipo"] == "PRT" and clasificar_tornilleria(hoja["modelo"]):
            categoria = "Tornillería"
        elif "comercial" in hoja["tipo"] or _codigo(p) in COMERCIAL:
            categoria = "Comercial"
        else:
            categoria = "Fabricada"
        aviso = []
        if m.get("error"):
            aviso.append(f"Creo no dio la masa: {m['error']}")
        elif kg is None:
            aviso.append("Sin masa o unidad de masa desconocida.")
        elif kg <= 0:
            aviso.append("Masa cero: sin material o sin densidad.")
        if hoja["tipo"] == "PRT" and not material:
            aviso.append("Sin material asignado (la densidad puede ser la de por defecto).")
        chk = check_mass_param({x: p[x]["value"] for x in ("MASA", "MASA_UNIT") if x in p},
                               props.get("mass"), unidad)
        masa_param = dif = None
        if chk and chk.get("estado") in ("coincide", "difiere"):
            masa_param, dif = chk["parametro_kg"], chk["diferencia_pct"]
            if chk["estado"] == "difiere" and not (dif is None and not masa_param):
                if dif is None:  # masa calculada cero: el aviso de masa cero ya lo explica
                    aviso.append(f"Parámetro MASA = {masa_param:.3f} kg, pero la masa calculada es cero.")
                else:
                    aviso.append(f"Parámetro MASA distinto de la calculada ({dif:+.1f} %).")
        if kg:
            suma += kg * hoja["cantidad"]
            for g, c in hoja["grupos"].items():
                sub[g] += kg * c
        dens_si = None
        if dens is not None and str(unidad or "").lower() in _MASS_TO_KG:
            # densidad en unidades del modelo (masa/longitud^3); IAC: t/mm^3 -> kg/m^3
            lu = str(conjunto.get("unidad_longitud") or "mm").lower()
            from .formato import _LEN_TO_M

            if lu in _LEN_TO_M:
                dens_si = dens * _MASS_TO_KG[str(unidad).lower()] / _LEN_TO_M[lu] ** 3
        filas.append({"modelo": hoja["modelo"], "categoria": categoria, "tipo": hoja["tipo"],
                      "grupos": ", ".join(hoja["grupos"]), "codigo": _codigo(p) if _codigo(p) else "",
                      "pieza": normalizar(p.get("PIEZA", {}).get("value")), "material": material,
                      "densidad": round(dens_si) if dens_si else None, "cantidad": hoja["cantidad"],
                      "kg": kg, "masa_param": masa_param, "dif": dif, "aviso": " ".join(aviso)})
        for a in aviso:
            avisos.append({"modelo": hoja["modelo"], "aviso": a})
    total_creo = (conjunto.get("si") or {}).get("masa_kg")
    dif_total = (suma - total_creo) / total_creo * 100 if total_creo else None
    if dif_total is not None and abs(dif_total) > 0.5:
        avisos.insert(0, {"modelo": bom.get("assembly") or "", "aviso": (
            f"La suma de piezas ({suma:.3f} kg) difiere un {dif_total:+.1f} % de la masa del conjunto según Creo "
            f"({total_creo:.3f} kg): revisa esqueletos, componentes suprimidos o envolventes.")})
    return {"conjunto": bom.get("assembly"), "filas": filas, "subtotales": sub, "suma_kg": suma,
            "total_creo_kg": total_creo, "dif_total_pct": dif_total, "si": conjunto.get("si") or {},
            "avisos": avisos}


def exportar(d: dict[str, Any], ruta: Path, origen: dict[str, Any]) -> dict[str, Any]:
    N3, N3B, B = xlsx.NUM3, xlsx.NUM3_NEGRITA, xlsx.NEGRITA
    cab = lambda cols, edit=(): [(c, xlsx.CABECERA_EDITABLE if c in edit else xlsx.CABECERA)  # noqa: E731
                                 for c in cols]

    # Piezas
    cols = ["Modelo", "Categoría", "Subconjunto (nivel 1)", "CODIGO_PLANO", "PIEZA", "Material",
            "Densidad (kg/m³)", "Cantidad", "Masa unitaria (kg)", "Masa total (kg)", "Margen (%)",
            "Masa con margen (kg)", "Parámetro MASA (kg)", "Diferencia MASA (%)", "Aviso"]
    filas: list[list[Any]] = [cab(cols, {"Margen (%)"})]
    orden = {"Fabricada": 0, "Comercial": 1, "Tornillería": 2}
    datos = sorted(d["filas"], key=lambda f: (orden[f["categoria"]], -(f["kg"] or 0) * f["cantidad"]))
    for i, f in enumerate(datos, start=2):
        filas.append([celda(f["modelo"]), f["categoria"], celda(f["grupos"]), celda(f["codigo"]),
                      celda(f["pieza"]), celda(f["material"]), f["densidad"], f["cantidad"],
                      (f["kg"], N3), (xlsx.Formula(f"H{i}*I{i}"), N3), (None, xlsx.EDITABLE),
                      (xlsx.Formula(f"J{i}*(1+K{i}/100)"), N3), (f["masa_param"], N3), f["dif"],
                      celda(f["aviso"])])
    n = len(datos) + 1
    filas.append([])
    filas.append([("TOTAL", B), "", "", "", "", "", "", (xlsx.Formula(f"SUM(H2:H{n})"), B), "",
                  (xlsx.Formula(f"SUM(J2:J{n})"), N3B), "", (xlsx.Formula(f"SUM(L2:L{n})"), N3B)])
    for cat in ("Fabricada", "Comercial", "Tornillería"):
        filas.append([(f"  {cat}", B), "", "", "", "", "", "", "", "",
                      (xlsx.Formula(f'SUMIF(B2:B{n},"{cat}",J2:J{n})'), N3B), "",
                      (xlsx.Formula(f'SUMIF(B2:B{n},"{cat}",L2:L{n})'), N3B)])
    filas.append([])
    filas.append(["Margen (%): columna amarilla, a rellenar según la madurez de cada pieza (criterio del "
                  "proyecto). Las masas son las que calcula Creo con la densidad del material asignado."])
    piezas = {"nombre": "Piezas", "filas": filas, "fijar_cabecera": True, "autofiltro": n,
              "anchos": [34, 12, 28, 24, 30, 16, 10, 9, 12, 12, 10, 14, 12, 11, 50]}

    # Por subconjunto
    total = d["suma_kg"] or 0
    fs = [cab(["Componente de primer nivel", "Masa (kg)", "% del total"])]
    for g, kg in sorted(d["subtotales"].items(), key=lambda x: -x[1]):
        fs.append([celda(g), (kg, N3), round(kg / total * 100, 1) if total else None])
    fs.append([("TOTAL", B), (total, N3B), 100.0 if total else None])
    sub = {"nombre": "Por subconjunto", "filas": fs, "fijar_cabecera": True, "anchos": [40, 14, 12]}

    # Resumen
    si = d["si"]
    cdg = si.get("cdg_mm") or {}
    r: list[list[Any]] = [cab(["Dato", "Valor", "Unidad"])]
    r += [["Conjunto", celda(d["conjunto"]), ""],
          ["Masa según Creo (conjunto)", (d["total_creo_kg"], N3), "kg"],
          ["Masa suma de piezas", (d["suma_kg"], N3), "kg"],
          ["Diferencia", round(d["dif_total_pct"], 2) if d["dif_total_pct"] is not None else None, "%"],
          ["Piezas y comerciales distintos", len(d["filas"]), ""],
          ["Elementos (con cantidades)", sum(f["cantidad"] for f in d["filas"]), ""]]
    for eje in ("x", "y", "z"):
        if eje in cdg:
            r.append([f"CdG {eje.upper()} (sistema del conjunto)", round(cdg[eje], 3), "mm"])
    ine = si.get("inercia_cdg_kg_m2") or {}
    for fila_t, row in sorted(ine.items()):
        if isinstance(row, dict):
            for col, v in sorted(row.items()):
                a, b = str(fila_t)[:1].upper(), str(col)[:1].upper()
                r.append([f"Inercia en el CdG I{a}{b}", (v, xlsx.NUM3), "kg·m²"])
    r += [[], ["Avisos", len(d["avisos"]), ""],
          ["Generado", datetime.now().strftime("%Y-%m-%d %H:%M"), origen.get("servidor", "")],
          ["Origen", "Masas calculadas por Creo (file_massprops) con la densidad del material asignado. "
                     "Borrador: lo revisa una persona.", ""]]
    res = {"nombre": "Resumen", "filas": r, "anchos": [36, 30, 10]}

    av = {"nombre": "Avisos", "filas": [cab(["Modelo", "Aviso"])] + [[celda(a["modelo"]), celda(a["aviso"])]
                                                                      for a in d["avisos"]],
          "fijar_cabecera": True, "anchos": [40, 100]}
    xlsx.escribir(ruta, [res, sub, piezas, av])
    return {"masa_creo_kg": d["total_creo_kg"], "masa_suma_kg": round(d["suma_kg"], 4),
            "diferencia_pct": round(d["dif_total_pct"], 2) if d["dif_total_pct"] is not None else None,
            "piezas": len(d["filas"]), "avisos": len(d["avisos"])}
