"""Comprobación de taladros conjugados en un conjunto (lectura nativa, sin tocar Creo).

Junta las operaciones de taladro de cada pieza (orden `taladros`, ejes en coordenadas de la
pieza) con la posición de cada componente (orden `posiciones`, matriz 4×4) y busca, en
coordenadas del conjunto, parejas de taladros de piezas distintas que están enfrentados:
ejes casi paralelos, casi coincidentes y con los tramos de eje tocándose a lo largo del eje.
De cada pareja comprueba la alineación (frente a la holgura que deja el tornillo) y que los
tamaños casen (roscado M6 con holgura M6, no con holgura M5).

No ve taladros hechos como extrusión o revolución (solo operaciones de taladro).
"""

from __future__ import annotations

import math
from typing import Any, Callable

from .nativo import diametro_nominal, nombre_fichero, resumir_operacion

TOL_COAXIAL = 0.01          # mm: por debajo, coaxiales
ANGULO_MAX = 5.0            # grados: por encima no se consideran enfrentados
ANGULO_AVISO = 0.05         # grados: por encima se avisa de ejes no paralelos
ANGULO_ERROR = 1.0
HOLGURA_GRANDE = 1.25       # Ø recto > 1,25·d + 0,5 mm: holgura mayor que la basta de ISO 273

ORDEN_ESTADO = {"error": 0, "aviso": 1, "correcto": 2}


# ------------------------------------------------------------------ geometría

def _transformar(m: list[float], p: list[float]) -> tuple[float, float, float]:
    """Matriz de Creo 4×4 por filas (filas 0-2 ejes del componente, fila 3 origen)."""
    return tuple(p[0] * m[k] + p[1] * m[4 + k] + p[2] * m[8 + k] + m[12 + k] for k in range(3))  # type: ignore[return-value]


def _resta(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _punto(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norma(a):
    return math.sqrt(_punto(a, a))


def _dist_recta(p, origen, d):
    w = _resta(p, origen)
    t = _punto(w, d)
    return _norma((w[0] - t * d[0], w[1] - t * d[1], w[2] - t * d[2])), t


# ------------------------------------------------------------------ taladros

def describir(op: dict[str, Any]) -> dict[str, Any]:
    """Función del taladro: roscado, holgura, taladrado (broca de roscar sin rosca), recto o cónico."""
    clase = op.get("clase")
    diam = op.get("diametro") if isinstance(op.get("diametro"), (int, float)) else None
    if clase == "rosca_cosmetica":
        return {"tipo": "roscado", "nominal": diam, "diametro": diam, "texto": f"rosca cosmética Ø{diam:g}" if diam else "rosca cosmética"}
    est = op.get("estandar")
    if op.get("tipo") == "normalizado":
        nom = diametro_nominal(op.get("tamano_tornillo")) or (op.get("rosca") or {}).get("diametro")
        tam = op.get("tamano_tornillo") or (f"M{nom:g}" if nom else "?")
        if est == "roscado":
            return {"tipo": "roscado", "nominal": nom, "diametro": diam, "texto": f"{tam} roscado"}
        if est == "holgura":
            aj = f", ajuste {op['ajuste']}" if op.get("ajuste") else ""
            return {"tipo": "holgura", "nominal": nom, "diametro": diam,
                    "texto": f"{tam} holgura Ø{diam:g}{aj}" if diam else f"{tam} holgura"}
        if est == "taladrado":
            return {"tipo": "taladrado", "nominal": nom, "diametro": diam,
                    "texto": f"{tam} taladrado sin rosca Ø{diam:g}" if diam else f"{tam} taladrado sin rosca"}
        return {"tipo": "conico", "nominal": nom, "diametro": diam, "texto": f"{tam} {est or 'normalizado'}"}
    return {"tipo": "recto", "nominal": None, "diametro": diam, "texto": f"recto Ø{diam:g}" if diam else "recto"}


def _compatibilidad(a: dict[str, Any], b: dict[str, Any]) -> tuple[str, str, float | None]:
    """(estado, nota, descentrado admisible en mm o None si no se sabe) según los tamaños."""
    par = {a["tipo"], b["tipo"]}
    por = {a["tipo"]: a, b["tipo"]: b}
    na, nb = a.get("nominal"), b.get("nominal")
    if "conico" in par:
        return "aviso", "Taladro cónico: revisar a mano.", None
    if par == {"roscado", "holgura"}:
        r, h = por["roscado"], por["holgura"]
        if r["nominal"] and h["nominal"] and abs(r["nominal"] - h["nominal"]) > 1e-6:
            return "error", f"Tamaños distintos: rosca M{r['nominal']:g} frente a holgura de M{h['nominal']:g}.", None
        adm = (h["diametro"] - r["nominal"]) / 2 if h.get("diametro") and r.get("nominal") else None
        return "correcto", "Roscado con su taladro de holgura.", adm
    if par == {"roscado", "recto"}:
        r, d = por["roscado"], por["recto"]
        if not (r.get("nominal") and d.get("diametro")):
            return "aviso", "No se puede comparar el tamaño (falta el Ø o la métrica).", None
        if d["diametro"] <= r["nominal"] + 1e-6:
            return "error", f"El tornillo M{r['nominal']:g} no pasa por el recto Ø{d['diametro']:g}.", None
        adm = (d["diametro"] - r["nominal"]) / 2
        if d["diametro"] > HOLGURA_GRANDE * r["nominal"] + 0.5:
            return "aviso", (f"Recto Ø{d['diametro']:g} para M{r['nominal']:g}: holgura mayor que la basta de "
                             "ISO 273. ¿Es intencionado?"), adm
        return "correcto", f"Roscado con recto Ø{d['diametro']:g} como holgura.", adm
    if par == {"roscado"}:
        if na and nb and abs(na - nb) > 1e-6:
            return "error", f"Dos roscas enfrentadas de tamaño distinto (M{na:g} y M{nb:g}).", 0.0
        return "aviso", "Dos roscas enfrentadas: solo vale con espárrago o si una pieza no se atornilla a la otra.", 0.0
    if "taladrado" in par:
        otro = b if a["tipo"] == "taladrado" else a
        return "aviso", (f"Taladrado sin rosca frente a {otro['texto']}: ¿falta la rosca, o es un taladro "
                         "de holgura mal elegido?"), None
    if par == {"holgura"}:
        if na and nb and abs(na - nb) > 1e-6:
            return "aviso", f"Holguras de tamaños distintos (M{na:g} y M{nb:g}).", None
        dmin = min(x["diametro"] for x in (a, b) if x.get("diametro")) if (a.get("diametro") or b.get("diametro")) else None
        adm = (dmin - na) / 2 if dmin and na else None
        return "correcto", "Dos holguras: tornillo pasante con tuerca.", adm
    if par == {"holgura", "recto"}:
        h, d = por["holgura"], por["recto"]
        if h.get("nominal") and d.get("diametro") and d["diametro"] <= h["nominal"] + 1e-6:
            return "error", f"El tornillo M{h['nominal']:g} no pasa por el recto Ø{d['diametro']:g}.", None
        ds = [x for x in (h.get("diametro"), d.get("diametro")) if x]
        adm = (min(ds) - h["nominal"]) / 2 if ds and h.get("nominal") else None
        return "correcto", "Holgura con recto: tornillo pasante.", adm
    # recto con recto
    da, db = a.get("diametro"), b.get("diametro")
    if da and db and abs(da - db) > 1e-6:
        return "aviso", f"Rectos de Ø distinto (Ø{da:g} y Ø{db:g}): ¿escalón intencionado?", None
    return "correcto", "Rectos del mismo Ø (pasador o tornillo pasante).", None


# ------------------------------------------------------------------ revisión

def revisar_conjugados(taladros: dict[str, Any], posiciones: dict[str, Any],
                       separacion_max: float = 1.0, tol_coaxial: float = TOL_COAXIAL,
                       excluir: Callable[[str], bool] | None = None) -> dict[str, Any]:
    conjunto = nombre_fichero(posiciones["modelo"]["nombre"], posiciones["modelo"]["tipo"])
    unidad = posiciones.get("unidad_longitud") or taladros.get("unidad_longitud")
    avisos: list[str] = []
    ops_modelo: dict[tuple[str, str], list[tuple[dict, list]]] = {}
    for m in taladros.get("modelos", []):
        if (m.get("unidad_longitud") or unidad) != unidad:
            avisos.append(f"{nombre_fichero(m.get('nombre', ''), m.get('tipo'))} está en {m.get('unidad_longitud')} "
                          f"y el conjunto en {unidad}: sus taladros no se revisan.")
            continue
        if excluir and excluir(nombre_fichero(m.get("nombre", ""), m.get("tipo"))):
            continue
        lista = []
        for op in m.get("operaciones", []):
            ejes = op.get("ejes") or []
            if ejes:
                lista.append((resumir_operacion(op), ejes[0][:6]))
        if lista:
            ops_modelo[(str(m.get("nombre", "")).upper(), m.get("tipo") or "PRT")] = lista

    # taladros de cada instancia, en coordenadas del conjunto
    taladros_inst: list[dict[str, Any]] = []
    cuenta: dict[str, int] = {}
    sin_matriz = 0
    for c in posiciones.get("componentes", []):
        clave = (str(c.get("modelo", "")).upper(), c.get("tipo") or "PRT")
        if clave[1] != "PRT" or clave not in ops_modelo:
            continue
        if not c.get("matriz") or len(c["matriz"]) != 16:
            sin_matriz += 1
            continue
        nombre = nombre_fichero(clave[0], "PRT")
        cuenta[nombre] = cuenta.get(nombre, 0) + 1
        etiqueta = f"{nombre} #{cuenta[nombre]}"
        for op, eje in ops_modelo[clave]:
            p1 = _transformar(c["matriz"], eje[:3])
            p2 = _transformar(c["matriz"], eje[3:6])
            v = _resta(p2, p1)
            n = _norma(v)
            if n < 1e-9:
                continue
            d = describir(op)
            taladros_inst.append({"pieza": etiqueta, "modelo": nombre, "ruta": c.get("ruta") or [],
                                  "id": op.get("id"), "nombre": op.get("nombre"), **d,
                                  "p1": p1, "dir": (v[0] / n, v[1] / n, v[2] / n), "largo": n})
    if sin_matriz:
        avisos.append(f"{sin_matriz} componentes sin posición (Creo no dio su matriz): no se revisan.")

    pares = []
    con_pareja: set[int] = set()
    cos_max = math.cos(math.radians(ANGULO_MAX))
    for i, a in enumerate(taladros_inst):
        ra = (a.get("diametro") or 0) / 2
        for j in range(i + 1, len(taladros_inst)):
            b = taladros_inst[j]
            if a["ruta"] == b["ruta"]:
                continue
            cosang = abs(_punto(a["dir"], b["dir"]))
            if cosang < cos_max:
                continue
            q1 = b["p1"]
            q2 = tuple(b["p1"][k] + b["dir"][k] * b["largo"] for k in range(3))
            r1, t1 = _dist_recta(q1, a["p1"], a["dir"])
            r2, t2 = _dist_recta(q2, a["p1"], a["dir"])
            lo, hi = max(0.0, min(t1, t2)), min(a["largo"], max(t1, t2))
            hueco = max(0.0, lo - hi)
            if hueco > separacion_max:
                continue
            # descentrado en la zona de contacto (punto medio del solape, o el extremo más cercano)
            tm = min(max((lo + hi) / 2, 0.0), a["largo"])
            s = (tm - t1) / (t2 - t1) if abs(t2 - t1) > 1e-12 else 0.5
            s = min(1.0, max(0.0, s))
            r = r1 + (r2 - r1) * s
            rb = (b.get("diametro") or 0) / 2
            if r > max(ra, rb, tol_coaxial):
                continue                      # no se pisan: no son conjugados
            angulo = math.degrees(math.acos(min(1.0, cosang)))
            estado, nota, adm = _compatibilidad(a, b)
            notas = [nota]
            if r > tol_coaxial:
                if adm is not None and r > adm + 1e-6:
                    estado = "error"
                    notas.insert(0, f"Desalineados {r:.3f} mm: más que la holgura que deja el tornillo ({adm:.3f} mm).")
                elif adm is not None:
                    estado = "aviso" if estado == "correcto" else estado
                    notas.insert(0, f"Descentrados {r:.3f} mm, dentro de la holgura ({adm:.3f} mm).")
                else:
                    if r > 0.1:
                        estado = "error"
                    elif estado == "correcto":
                        estado = "aviso"
                    notas.insert(0, f"Desalineados {r:.3f} mm (holgura no conocida).")
            if angulo > ANGULO_AVISO:
                if angulo > ANGULO_ERROR:
                    estado = "error"
                elif estado == "correcto":
                    estado = "aviso"
                notas.insert(0, f"Ejes no paralelos ({angulo:.2f}°).")
            if hueco > 0.01:
                notas.append(f"Separados {hueco:.2f} mm a lo largo del eje.")
            p = tuple(a["p1"][k] + a["dir"][k] * tm for k in range(3))
            pares.append({"estado": estado,
                          "a": {"pieza": a["pieza"], "ruta": a["ruta"], "id": a["id"], "nombre": a["nombre"], "taladro": a["texto"]},
                          "b": {"pieza": b["pieza"], "ruta": b["ruta"], "id": b["id"], "nombre": b["nombre"], "taladro": b["texto"]},
                          "descentrado": round(r, 4), "angulo": round(angulo, 3),
                          "punto": [round(x, 3) for x in p], "nota": " ".join(notas)})
            con_pareja.update((i, j))
    pares.sort(key=lambda x: (ORDEN_ESTADO[x["estado"]], -x["descentrado"]))
    sin_pareja = [{"pieza": t["pieza"], "id": t["id"], "nombre": t["nombre"], "taladro": t["texto"], "tipo": t["tipo"],
                   "punto": [round(x, 3) for x in t["p1"]]}
                  for k, t in enumerate(taladros_inst) if k not in con_pareja]
    por_estado = {e: sum(1 for x in pares if x["estado"] == e) for e in ("error", "aviso", "correcto")}
    return {"conjunto": conjunto, "unidad_longitud": unidad,
            "piezas_con_taladros": len(cuenta), "taladros_revisados": len(taladros_inst),
            "pares": pares, "por_estado": por_estado,
            "sin_pareja": sin_pareja, "roscados_sin_pareja": sum(1 for t in sin_pareja if t["tipo"] == "roscado"),
            "avisos": avisos}
