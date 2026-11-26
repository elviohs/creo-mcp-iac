"""Extrusiones (añadir material) y cortes (quitar material) con croquis, planificados sin tocar Creo.

El croquis va en un plano de referencia o en una cara plana perpendicular a un eje de la pieza.
Las coordenadas del croquis son las dos coordenadas de la pieza que hay en ese plano, en el
orden X, Y, Z: plano normal a Z -> (x, y); normal a Y -> (x, z); normal a X -> (y, z).

Contornos admitidos (todos cerrados):
  {"tipo": "circulo", "centro": [u, v], "diametro": D}            (o "radio")
  {"tipo": "rectangulo", "centro": [u, v] | "esquina": [u, v], "ancho": a, "alto": b,
   "radio_esquinas": r}                                            (ancho en u, alto en v)
  {"tipo": "ranura", "centro1": [u, v], "centro2": [u, v], "ancho": w}   (coliso)
  {"tipo": "perfil", "puntos": [[u, v], [u, v], {"hasta": [u, v], "por": [u, v]},
                                {"hasta": [u, v], "centro": [u, v], "giro": "horario"}, ...]}
     cada elemento después del primero es una recta hasta ese punto, o un arco (por tres puntos o
     por centro y giro). El perfil se cierra solo con una recta al primer punto.

Contornos dentro de otros son huecos (y dentro de un hueco, isla), como en el croquis de Creo.
"""
from __future__ import annotations

import math
from typing import Any

from .operaciones import PlanError, elegir_referencias, orientacion, _corregir_forma

TIPOS_EXT = {"anadir": (917, -1), "quitar": (916, 916)}   # PRO_E_FEATURE_TYPE, PRO_E_REMOVE_MATERIAL
EXTENSIONES = ("ciega", "simetrica", "pasante", "pasante_ambos")
FROM_NONE, FROM_ALL = 1 << 12, 1 << 9
TO_BLIND, TO_ALL, TO_SYM = 1 << 18, 1 << 20, 1 << 28
LEJOS = 1e9
MAX_ENTIDADES = 400
TOL = 1e-6


def _p2(v: Any, que: str) -> tuple[float, float]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise PlanError(f"{que}: hace falta [u, v].")
    try:
        p = (float(v[0]), float(v[1]))
    except (TypeError, ValueError) as e:
        raise PlanError(f"{que}: coordenadas no numéricas.") from e
    if not all(math.isfinite(c) and abs(c) < 1e5 for c in p):
        raise PlanError(f"{que}: coordenadas fuera de rango.")
    return p


def _pos(v: Any, que: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError) as e:
        raise PlanError(f"{que}: obligatorio y numérico (mm).") from e
    if not (x > 0 and math.isfinite(x) and x < 1e5):
        raise PlanError(f"{que}: debe ser positivo (mm).")
    return x


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _redondear(x: float) -> float:
    y = round(x, 3)
    return y if abs(x - y) < 5e-5 else x


# Segmentos: ("L", p, q) o ("A", centro, radio, angulo_inicio, barrido_con_signo)

def _arco_3p(p, m, q, que: str):
    ax, ay = p; bx, by = m; cx, cy = q
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        raise PlanError(f"{que}: los tres puntos del arco están alineados.")
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    # Los puntos de tangencia llegan redondeados: si el centro o el radio quedan a menos de 5e-5 de un
    # valor de milésimas, se toma ese valor (el del dibujo) para no acotar 14,99999 en vez de 15.
    ux, uy = _redondear(ux), _redondear(uy)
    c = (ux, uy)
    r = _redondear(_dist(c, p))
    a1 = math.atan2(p[1] - uy, p[0] - ux)
    a2 = math.atan2(q[1] - uy, q[0] - ux)
    ccw = (bx - ax) * (cy - by) - (by - ay) * (cx - bx) > 0
    s = (a2 - a1) % (2 * math.pi)
    if not ccw:
        s -= 2 * math.pi
    return ("A", c, r, a1, s)


def _arco_centro(p, c, q, giro: str, que: str):
    r = _dist(c, p)
    if r < 1e-6:
        raise PlanError(f"{que}: el centro del arco coincide con su inicio.")
    if abs(_dist(c, q) - r) > max(1e-3, 1e-5 * r):
        raise PlanError(f"{que}: el centro no está a la misma distancia del inicio ({r:.3f}) y del final "
                        f"({_dist(c, q):.3f}).")
    a1 = math.atan2(p[1] - c[1], p[0] - c[0])
    a2 = math.atan2(q[1] - c[1], q[0] - c[0])
    s = (a2 - a1) % (2 * math.pi)
    if giro == "horario":
        s -= 2 * math.pi
    elif giro != "antihorario":
        raise PlanError(f"{que}: giro 'horario' o 'antihorario' (visto con u a la derecha y v hacia arriba).")
    if abs(s) < 1e-9:
        raise PlanError(f"{que}: arco de longitud nula.")
    return ("A", c, r, a1, s)


def _fin(seg):
    if seg[0] == "L":
        return seg[2]
    _, c, r, a1, s = seg
    return (c[0] + r * math.cos(a1 + s), c[1] + r * math.sin(a1 + s))


def _area_seg(seg) -> float:
    if seg[0] == "L":
        (px, py), (qx, qy) = seg[1], seg[2]
        return 0.5 * (px * qy - qx * py)
    _, (cx, cy), r, a1, s = seg
    a2 = a1 + s
    return 0.5 * (r * r * s + cx * r * (math.sin(a2) - math.sin(a1)) - cy * r * (math.cos(a2) - math.cos(a1)))


def _puntos_seg(seg) -> list[tuple[float, float]]:
    """Polilínea del segmento (sin el punto final) para comprobar cruces e inclusiones."""
    if seg[0] == "L":
        return [seg[1]]
    _, c, r, a1, s = seg
    n = max(4, math.ceil(abs(s) / (math.pi / 16)))
    return [(c[0] + r * math.cos(a1 + s * i / n), c[1] + r * math.sin(a1 + s * i / n)) for i in range(n)]


def _contorno(ct: Any, i: int) -> dict[str, Any]:
    que = f"contorno {i + 1}"
    if not isinstance(ct, dict):
        raise PlanError(f"{que}: debe ser un objeto con 'tipo'.")
    tipo = str(ct.get("tipo") or "").lower()
    segs: list[tuple] = []
    if tipo == "circulo":
        c = _p2(ct.get("centro"), que + " centro")
        r = _pos(ct["radio"], que + " radio") if ct.get("radio") is not None else _pos(ct.get("diametro"), que + " diametro") / 2
        return {"tipo": "circulo", "centro": c, "radio": r, "segs": [("C", c, r)], "area": math.pi * r * r,
                "poli": [(c[0] + r * math.cos(2 * math.pi * k / 64), c[1] + r * math.sin(2 * math.pi * k / 64)) for k in range(64)],
                "entidades": 1, "desc": f"círculo Ø{2 * r:g} en ({c[0]:g}, {c[1]:g})"}
    if tipo == "rectangulo":
        a, b = _pos(ct.get("ancho"), que + " ancho"), _pos(ct.get("alto"), que + " alto")
        if ct.get("centro") is not None:
            c = _p2(ct["centro"], que + " centro"); x0, y0 = c[0] - a / 2, c[1] - b / 2
        else:
            x0, y0 = _p2(ct.get("esquina"), que + " esquina (o centro)")
        r = float(ct.get("radio_esquinas") or 0)
        if r < 0 or r > min(a, b) / 2 + 1e-9:
            raise PlanError(f"{que}: radio_esquinas entre 0 y la mitad del lado menor ({min(a, b) / 2:g}).")
        x1, y1 = x0 + a, y0 + b
        if r <= 1e-9:
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            segs = [("L", pts[k], pts[(k + 1) % 4]) for k in range(4)]
        else:
            h = math.pi / 2
            esquinas = [((x1 - r, y0 + r), -h), ((x1 - r, y1 - r), 0.0), ((x0 + r, y1 - r), h), ((x0 + r, y0 + r), math.pi)]
            for k, (cc, ang) in enumerate(esquinas):
                arco = ("A", cc, r, ang, h)
                segs.append(arco)
                fin = _fin(arco)
                sig_c, sig_a = esquinas[(k + 1) % 4]
                ini = (sig_c[0] + r * math.cos(sig_a), sig_c[1] + r * math.sin(sig_a))
                if _dist(fin, ini) > 1e-9:
                    segs.append(("L", fin, ini))
        desc = f"rectángulo {a:g}×{b:g} de ({x0:g}, {y0:g}) a ({x1:g}, {y1:g})" + (f", R{r:g} en las esquinas" if r > 1e-9 else "")
        extra = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "r": r}
    elif tipo == "ranura":
        c1, c2 = _p2(ct.get("centro1"), que + " centro1"), _p2(ct.get("centro2"), que + " centro2")
        w = _pos(ct.get("ancho"), que + " ancho"); r = w / 2
        L = _dist(c1, c2)
        if L < 1e-6:
            raise PlanError(f"{que}: centro1 y centro2 coinciden (para eso, un círculo).")
        ang = math.atan2(c2[1] - c1[1], c2[0] - c1[0]); nx, ny = -math.sin(ang), math.cos(ang)
        p1 = (c1[0] - r * nx, c1[1] - r * ny); p2 = (c2[0] - r * nx, c2[1] - r * ny)
        p3 = (c2[0] + r * nx, c2[1] + r * ny); p4 = (c1[0] + r * nx, c1[1] + r * ny)
        segs = [("L", p1, p2), ("A", c2, r, ang - math.pi / 2, math.pi), ("L", p3, p4), ("A", c1, r, ang + math.pi / 2, math.pi)]
        desc = f"ranura de ancho {w:g} entre ({c1[0]:g}, {c1[1]:g}) y ({c2[0]:g}, {c2[1]:g})"
        extra = {"c1": c1, "c2": c2, "r": r}
    elif tipo == "perfil":
        pts = ct.get("puntos")
        if not isinstance(pts, list) or len(pts) < 2:
            raise PlanError(f"{que}: 'puntos' con al menos dos elementos.")
        cur = _p2(pts[0], que + " punto 1")
        inicio = cur
        for k, el in enumerate(pts[1:], start=2):
            qk = f"{que} elemento {k}"
            if isinstance(el, dict):
                hasta = _p2(el.get("hasta"), qk + " hasta")
                if el.get("por") is not None:
                    seg = _arco_3p(cur, _p2(el["por"], qk + " por"), hasta, qk)
                elif el.get("centro") is not None:
                    seg = _arco_centro(cur, _p2(el["centro"], qk + " centro"), hasta, str(el.get("giro") or ""), qk)
                else:
                    raise PlanError(f"{qk}: un arco necesita 'por' (punto intermedio) o 'centro' y 'giro'.")
            else:
                hasta = _p2(el, qk)
                if _dist(cur, hasta) < 1e-6:
                    raise PlanError(f"{qk}: punto repetido.")
                seg = ("L", cur, hasta)
            segs.append(seg)
            cur = hasta
        if _dist(cur, inicio) > 1e-6:
            segs.append(("L", cur, inicio))
        if len(segs) < 2 or (len(segs) == 2 and all(s[0] == "L" for s in segs)):
            raise PlanError(f"{que}: el perfil no encierra ningún área.")
        desc = f"perfil de {len(segs)} tramos ({sum(1 for s in segs if s[0] == 'A')} arcos)"
        extra = {}
    else:
        raise PlanError(f"{que}: tipo 'circulo', 'rectangulo', 'ranura' o 'perfil'.")
    area = sum(_area_seg(s) for s in segs)
    poli = [p for s in segs for p in _puntos_seg(s)]
    return {"tipo": tipo, "segs": segs, "area": abs(area), "poli": poli, "entidades": len(segs), "desc": desc, **extra}


def _cruzan(a, b, c, d) -> bool:
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 * o2 < 0 and o3 * o4 < 0


def _aristas(poli):
    return [(poli[k], poli[(k + 1) % len(poli)]) for k in range(len(poli))]


def _caja(poli):
    xs, ys = [p[0] for p in poli], [p[1] for p in poli]
    return min(xs), min(ys), max(xs), max(ys)


def _dentro(p, poli) -> bool:
    x, y = p
    dentro = False
    for (x1, y1), (x2, y2) in _aristas(poli):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            dentro = not dentro
    return dentro


def _comprobar_cruces(cts: list[dict[str, Any]]) -> None:
    for i, c in enumerate(cts):
        ar = _aristas(c["poli"])
        n = len(ar)
        for a in range(n):
            for b in range(a + 2, n):
                if a == 0 and b == n - 1:
                    continue
                if _cruzan(*ar[a], *ar[b]):
                    raise PlanError(f"El contorno {i + 1} se corta a sí mismo.")
    cajas = [_caja(c["poli"]) for c in cts]
    for i in range(len(cts)):
        for j in range(i + 1, len(cts)):
            ci, cj = cajas[i], cajas[j]
            if ci[2] < cj[0] or cj[2] < ci[0] or ci[3] < cj[1] or cj[3] < ci[1]:
                continue
            for s in _aristas(cts[i]["poli"]):
                for t in _aristas(cts[j]["poli"]):
                    if _cruzan(*s, *t):
                        raise PlanError(f"Los contornos {i + 1} y {j + 1} se cortan: cada contorno debe quedar "
                                        "fuera de los demás o dentro del todo (hueco).")


def _plano_croquis(caras: dict[str, Any], plano: int | str) -> dict[str, Any]:
    if isinstance(plano, str) and not plano.strip().isdigit():
        p = next((x for x in caras.get("planos_referencia", []) if str(x.get("nombre", "")).upper() == plano.strip().upper()), None)
        if p is None:
            nombres = [x.get("nombre") for x in caras.get("planos_referencia", [])]
            raise PlanError(f"No hay ningún plano de referencia {plano}. Hay: {nombres}.")
        n, o, pid, que = p.get("normal"), p.get("origen"), p["id"], f"plano {p.get('nombre')}"
    else:
        pid = int(plano)
        c = next((x for x in caras.get("caras_planas", []) if x["id"] == pid), None)
        if c is None:
            p = next((x for x in caras.get("planos_referencia", []) if x["id"] == pid), None)
            if p is None:
                raise PlanError(f"{pid} no es una cara plana ni un plano de referencia de la pieza.")
            n, o, que = p.get("normal"), p.get("origen"), f"plano {p.get('nombre')}"
        else:
            n, o, que = c.get("normal_exterior"), c.get("punto_plano"), f"cara {pid}"
    if not n or not o:
        raise PlanError(f"El {que} no tiene normal u origen conocidos.")
    ori = orientacion(n)
    if ori is None:
        # plano oblicuo (0.21.0): el marco del croquis se calcula en _marco_oblicuo
        es_cara = any(x["id"] == pid for x in caras.get("caras_planas", []))
        return {"id": pid, "que": que, "normal": [float(x) for x in n], "punto": [float(x) for x in o],
                "oblicuo": True, "es_cara": es_cara}
    k = "XYZ".index(ori[1])
    ejes = [i for i in range(3) if i != k]
    return {"id": pid, "que": que, "normal": [float(x) for x in n], "orientacion": ori, "eje": k,
            "coord": float(o[k]), "ejes_uv": ejes}


def _v3(a, b, op):
    return [op(a[i], b[i]) for i in range(3)]


def _punto3(a): return [float(x) for x in a]
def _prod(a, b): return sum(a[i] * b[i] for i in range(3))
def _cruz(a, b): return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _unit3(v):
    n = math.sqrt(_prod(v, v)) or 1.0
    return [c / n for c in v]


def _marco_oblicuo(caras: dict[str, Any], pl: dict[str, Any], aristas: dict[str, Any] | None,
                   referencia: str | None = None) -> dict[str, Any]:
    """Marco del croquis en una cara oblicua (0.21.0).
    u: distancia a un plano (de referencia o cara) perpendicular a la cara, medida en la cara;
    v: distancia, a lo largo de la cara, a una arista recta de la cara paralela a ese plano (la
    «primera» en sentido v). Devuelve el origen y los ejes en coordenadas de la pieza y las referencias
    del croquis: el plano perpendicular (también orienta el croquis) y aristas rectas de la cara."""
    if not pl.get("es_cara"):
        raise PlanError(f"El {pl['que']} es oblicuo: en planos oblicuos el croquis va sobre una cara plana de la "
                        "pieza (su id, de caras_creo o seleccion_creo), no sobre un plano de referencia.")
    n = _unit3(pl["normal"])
    tol = 1e-5
    perp = []
    for p in caras.get("planos_referencia", []):
        if p.get("normal") and p.get("origen") and abs(_prod(p["normal"], n)) < tol:
            perp.append({"tipo": 5, "id": p["id"], "nombre": p.get("nombre"), "normal": _unit3(p["normal"]),
                         "origen": _punto3(p["origen"]), "es_plano": True})
    for c in caras.get("caras_planas", []):
        if c["id"] != pl["id"] and c.get("normal_exterior") and abs(_prod(c["normal_exterior"], n)) < tol:
            perp.append({"tipo": 5, "id": c["id"], "nombre": f"cara {c['id']}", "normal": _unit3(c["normal_exterior"]),
                         "origen": _punto3(c["punto_plano"]), "es_plano": False})
    if referencia:
        sel = [p for p in perp if str(p["nombre"]).upper() == referencia.strip().upper() or str(p["id"]) == referencia.strip()]
        if not sel:
            raise PlanError(f"La referencia {referencia} no existe o no es perpendicular a la cara {pl['id']}.")
        perp = sel + [p for p in perp if p is not sel[0]]
    else:
        orden = {"PERFIL": 0, "ALZADO": 1, "PLANTA": 2, "RIGHT": 3, "FRONT": 4, "TOP": 5}
        perp.sort(key=lambda p: (not p["es_plano"], orden.get(str(p["nombre"]).upper(), 9)))
    if not perp:
        raise PlanError(f"No hay ningún plano de referencia ni cara plana perpendicular a la cara {pl['id']}: hace "
                        "falta uno para orientar el croquis (la cara está inclinada respecto a dos ejes). Crea en "
                        "Creo un plano de referencia perpendicular a la cara y vuelve a proponer.")
    r1 = perp[0]
    U = r1["normal"]
    V = _unit3(_cruz(n, U))
    # v hacia donde crece la coordenada de la pieza dominante en V (p. ej. «subiendo» por una rampa)
    dom = max(range(3), key=lambda i: abs(V[i]))
    if V[dom] < 0:
        V = [-c for c in V]
    # aristas rectas de la cara
    rectas = []
    for a in (_corregir_forma(aristas).get("aristas", []) if aristas else []):
        if pl["id"] in (a.get("caras") or []) and a.get("tipo") == "recta" and a.get("inicio") and a.get("fin"):
            t = _unit3(_v3(a["fin"], a["inicio"], lambda x, y: x - y))
            rectas.append({"id": a["id"], "inicio": _punto3(a["inicio"]), "t": t, "longitud": a.get("longitud", 0)})
    paralelas_u = [e for e in rectas if abs(abs(_prod(e["t"], U)) - 1) < 1e-6]
    otras = [e for e in rectas if abs(_prod(e["t"], U)) < 0.999 and e not in paralelas_u]
    p0 = pl["punto"]
    base = [n[i] * _prod(n, p0) for i in range(3)]          # proyección del origen de la pieza sobre la cara
    u_base = _prod(U, _v3(base, r1["origen"], lambda x, y: x - y))
    refs = [{"tipo": 5, "id": r1["id"], "nombre": r1["nombre"]}]
    ejes_ref = {0: (0, 0.0)}
    if paralelas_u:
        e0 = min(paralelas_u, key=lambda e: _prod(V, e["inicio"]))
        v_base = _prod(V, _v3(base, e0["inicio"], lambda x, y: x - y))
        refs.append({"tipo": 6, "id": e0["id"], "nombre": f"arista {e0['id']}"})
        ejes_ref[1] = (1, 0.0)
        desc_v = f"distancia a lo largo de la cara desde la arista {e0['id']}"
    else:
        v_base = _prod(V, base)
        desc_v = "distancia a lo largo de la cara desde la proyección del origen de la pieza"
    for e in otras[:2]:                                       # más referencias para situar el croquis
        refs.append({"tipo": 6, "id": e["id"], "nombre": f"arista {e['id']}"})
    O = [base[i] - u_base * U[i] - v_base * V[i] for i in range(3)]
    return {"O": O, "U": U, "V": V, "n": n, "refs": refs, "ejes_ref": ejes_ref, "orienta": r1,
            "desc": {"u": f"distancia a {r1['nombre']} (dirección {[round(c, 4) for c in U]})",
                     "v": f"{desc_v} (dirección {[round(c, 4) for c in V]})",
                     "origen": [round(c, 4) for c in O]}}


def _tangente_seg(seg, al_final: bool):
    """Dirección del tramo en su inicio o en su final."""
    if seg[0] == "L":
        d = (seg[2][0] - seg[1][0], seg[2][1] - seg[1][1])
    else:
        _, c, r, a1, sw = seg
        a = a1 + (sw if al_final else 0)
        sg = 1 if sw > 0 else -1
        d = (-math.sin(a) * sg, math.cos(a) * sg)
    n = math.hypot(*d) or 1.0
    return (d[0] / n, d[1] / n)


def _describir_cota(ct: tuple, refs: list[dict[str, Any]]) -> str:
    if ct[0] == "diam":
        return f"Ø{ct[2]:g}"
    if ct[0] == "radio":
        return f"R{ct[2]:g}"
    if ct[0] == "diaeje":
        return f"Ø{ct[4]:g} ({'lado' if ct[2] == 'w' else 'centro' if ct[2] == 'c' else 'vértice'}, al eje)"
    if ct[0] in ("ref", "refl"):
        ref = ct[3] if ct[0] == "ref" else ct[2]
        val = ct[4] if ct[0] == "ref" else ct[3]
        que = {"c": "centro", "s": "vértice", "e": "vértice", None: "lado"}.get(ct[2] if ct[0] == "ref" else None)
        return f"{que} a {val:g} de {refs[ref].get('nombre')}"
    return f"{ct[3]:g} entre lados"


def _cotas_boceto(cts: list[dict[str, Any]], ejes_ref: dict[int, tuple[int, float]],
                  radial: tuple[int, float, int] | None = None) -> list[tuple]:
    """Cotas del boceto como las acotaría un delineante: diámetros y radios, posición de centros y
    vértices desde los planos de referencia, ancho y alto de los rectángulos. Para no chocar con las
    restricciones que pone Creo (radios iguales, concéntricos, alineados, tangentes), cada radio y
    cada coordenada se acota una sola vez y no se acotan los puntos de tangencia ni las coordenadas
    que caen sobre una referencia. Lo que falte lo completa el acotado automático.
    Devuelve tuplas (tipo, entidad, ..., valor, lugar_uv)."""
    radios: set[float] = set()
    hechas: dict[int, set[float]] = {0: set(), 1: set()}
    out: list[tuple] = []

    def nuevo_radio(r):
        k = round(r, 6)
        if k in radios:
            return False
        radios.add(k)
        return True

    def pos(eje, valor, ent, pt, otro, recta=False):
        """Cota desde la referencia del eje 0 (u) o 1 (v). En una revolución, la coordenada
        perpendicular al eje se acota como diámetro desde la línea de centros."""
        if radial is not None and eje == radial[0]:
            k = round(valor, 6)
            d = abs(valor - radial[1])
            if d < 1e-6 or k in hechas[eje]:
                return
            hechas[eje].add(k)
            lugar = ((valor + radial[1]) / 2, otro + 6) if eje == 0 else (otro + 6, (valor + radial[1]) / 2)
            out.append(("diaeje", ent, "w" if recta else pt, radial[2], 2 * d, lugar))
            return
        if eje not in ejes_ref:
            return
        ref, origen = ejes_ref[eje]
        k = round(valor, 6)
        d = abs(valor - origen)
        if d < 1e-6 or k in hechas[eje]:
            return
        hechas[eje].add(k)
        lugar = ((valor + origen) / 2, otro + 6) if eje == 0 else (otro + 6, (valor + origen) / 2)
        out.append(("refl", ent, ref, d, lugar) if recta else ("ref", ent, pt, ref, d, lugar))

    idx = 0
    for c in cts:
        segs = c["segs"]
        base = idx
        idx += len(segs)
        if c["tipo"] == "circulo":
            (cu, cv), r = c["centro"], c["radio"]
            if nuevo_radio(r):
                out.append(("diam", base, 2 * r, (cu + 0.9 * r, cv + 0.9 * r)))
            pos(0, cu, base, "c", cv)
            pos(1, cv, base, "c", cu)
            continue
        if c["tipo"] == "rectangulo":
            def recta(fijo, eje):
                for j, sg in enumerate(segs):
                    if sg[0] == "L" and abs(sg[1][eje] - fijo) < 1e-9 and abs(sg[2][eje] - fijo) < 1e-9:
                        return base + j
                return None
            iz, de = recta(c["x0"], 0), recta(c["x1"], 0)
            ab, ar = recta(c["y0"], 1), recta(c["y1"], 1)
            if c["r"] > 1e-9:
                j = next(j for j, sg in enumerate(segs) if sg[0] == "A")
                if nuevo_radio(c["r"]):
                    sg = segs[j]
                    out.append(("radio", base + j, c["r"], (sg[1][0] + 1.5 * c["r"], sg[1][1] - 1.5 * c["r"])))
            rad = radial[0] if radial is not None else None
            # en una revolución, los dos lados paralelos al eje se acotan como diámetros
            if rad == 0:
                for lado, xv in ((iz, c["x0"]), (de, c["x1"])):
                    if lado is not None:
                        pos(0, xv, lado, None, (c["y0"] + c["y1"]) / 2, recta=True)
            elif iz is not None and de is not None:
                out.append(("entre", iz, de, c["x1"] - c["x0"], ((c["x0"] + c["x1"]) / 2, c["y1"] + 8)))
            if rad == 1:
                for lado, yv in ((ab, c["y0"]), (ar, c["y1"])):
                    if lado is not None:
                        pos(1, yv, lado, None, (c["x0"] + c["x1"]) / 2, recta=True)
            elif ab is not None and ar is not None:
                out.append(("entre", ab, ar, c["y1"] - c["y0"], (c["x1"] + 8, (c["y0"] + c["y1"]) / 2)))
            if iz is not None and rad != 0:
                pos(0, c["x0"], iz, None, (c["y0"] + c["y1"]) / 2, recta=True)
            if ab is not None and rad != 1:
                pos(1, c["y0"], ab, None, (c["x0"] + c["x1"]) / 2, recta=True)
            if rad != 0:
                hechas[0].add(round(c["x1"], 6))
            if rad != 1:
                hechas[1].add(round(c["y1"], 6))
            continue
        if c["tipo"] == "ranura":
            js = [j for j, sg in enumerate(segs) if sg[0] == "A"]
            if nuevo_radio(c["r"]):
                sg = segs[js[0]]
                out.append(("radio", base + js[0], c["r"], (sg[1][0] + 1.5 * c["r"], sg[1][1] + 1.5 * c["r"])))
            for j in js:
                cc = segs[j][1]
                pos(0, cc[0], base + j, "c", cc[1])
                pos(1, cc[1], base + j, "c", cc[0])
            continue
        # perfil: radio y centro de cada arco; vértices que no son de tangencia
        n = len(segs)
        for j, sg in enumerate(segs):
            if sg[0] == "A":
                _, cc, r, a1, sw = sg
                if nuevo_radio(r):
                    am = a1 + sw / 2
                    out.append(("radio", base + j, r, (cc[0] + 1.3 * r * math.cos(am), cc[1] + 1.3 * r * math.sin(am))))
                pos(0, cc[0], base + j, "c", cc[1])
                pos(1, cc[1], base + j, "c", cc[0])
        for j in range(n):
            a, b = segs[j - 1], segs[j]            # vértice entre a (que acaba) y b (que empieza)
            ta, tb = _tangente_seg(a, True), _tangente_seg(b, False)
            if (a[0] == "A" or b[0] == "A") and abs(ta[0] * tb[1] - ta[1] * tb[0]) < 1e-6 and ta[0] * tb[0] + ta[1] * tb[1] > 0:
                continue                           # punto de tangencia: lo fija la tangencia
            if b[0] == "L":
                ent, pt, p = base + j, "s", b[1]
            elif a[0] == "L":
                ent, pt, p = base + (j - 1) % n, "e", a[2]
            else:
                continue                           # entre dos arcos sin tangencia: acotado automático
            pos(0, p[0], ent, pt, p[1])
            pos(1, p[1], ent, pt, p[0])
    return out


def _preparar_contornos(peticion: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    cts_in = peticion.get("contornos")
    if not isinstance(cts_in, list) or not cts_in:
        raise PlanError("contornos: lista con al menos un contorno cerrado.")
    cts = [_contorno(ct, i) for i, ct in enumerate(cts_in)]
    if sum(c["entidades"] for c in cts) > MAX_ENTIDADES:
        raise PlanError(f"Demasiadas entidades en el croquis (máximo {MAX_ENTIDADES}).")
    _comprobar_cruces(cts)
    for i, c in enumerate(cts):
        if c["area"] < 1e-6:
            raise PlanError(f"contorno {i + 1}: área nula.")
    # profundidad de anidamiento: par = material, impar = hueco
    area = 0.0
    for i, c in enumerate(cts):
        nivel = sum(1 for j, d in enumerate(cts) if j != i and _dentro(c["poli"][0], d["poli"]))
        c["hueco"] = nivel % 2 == 1
        area += -c["area"] if c["hueco"] else c["area"]
    if area <= 1e-6:
        raise PlanError("El croquis no encierra área neta.")

    return cts, area


def _lineas_entidades(cts, p3, fmt) -> list[str]:
    sk: list[str] = []
    for c in cts:
        for s in c["segs"]:
            if s[0] == "C":
                sk.append(f"sk_circulo {fmt(p3(s[1]) + [s[2]])}")
            elif s[0] == "L":
                sk.append(f"sk_linea {fmt(p3(s[1]) + p3(s[2]))}")
            else:
                _, cc, r, a1, sw = s
                ini = (cc[0] + r * math.cos(a1), cc[1] + r * math.sin(a1))
                fin = (cc[0] + r * math.cos(a1 + sw), cc[1] + r * math.sin(a1 + sw))
                med = (cc[0] + r * math.cos(a1 + sw / 2), cc[1] + r * math.sin(a1 + sw / 2))
                sk.append(f"sk_arco {fmt(p3(cc) + p3(ini) + p3(fin) + p3(med))}")
    return sk


def _ejes_de_referencia(refs, u, v) -> dict[int, tuple[int, float]]:
    """Referencia del croquis que mide cada coordenada (u, v): plano normal a ese eje."""
    ejes_ref: dict[int, tuple[int, float]] = {}
    for i_r, r in enumerate(refs):
        nr, orr = r.get("normal"), r.get("origen")
        if not nr or not orr:
            continue
        for e_i, k_uv in enumerate((u, v)):
            if abs(abs(nr[k_uv]) - 1) < 1e-9 and e_i not in ejes_ref:
                ejes_ref[e_i] = (i_r, float(orr[k_uv]))
    return ejes_ref


def _lineas_cotas(cotas, p3, fmt) -> list[str]:
    out = []
    for ct in cotas:
        lugar = fmt(p3(ct[-1]))
        if ct[0] in ("diam", "radio"):
            out.append(f"sk_cota {ct[0]} {ct[1]} {ct[2]:.6f} {lugar}")
        elif ct[0] == "ref":
            out.append(f"sk_cota ref {ct[1]} {ct[2]} {ct[3]} {ct[4]:.6f} {lugar}")
        elif ct[0] == "refl":
            out.append(f"sk_cota refl {ct[1]} {ct[2]} {ct[3]:.6f} {lugar}")
        elif ct[0] == "diaeje":
            out.append(f"sk_cota diaeje {ct[1]} {ct[2]} {ct[3]} {ct[4]:.6f} {lugar}")
        else:
            out.append(f"sk_cota entre {ct[1]} {ct[2]} {ct[3]:.6f} {lugar}")
    return out


def planificar_extrusion(peticion: dict[str, Any], caras: dict[str, Any], nombre: str,
                         aristas: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plan para el servicio nativo (líneas e/sk_*/...) y resumen para la tabla de confirmación."""
    tipo = str(peticion.get("tipo") or "")
    if tipo not in TIPOS_EXT:
        raise PlanError("tipo: 'anadir' (extrusión, añade material) o 'quitar' (corte).")
    ext = str(peticion.get("extension") or "ciega")
    if ext not in EXTENSIONES:
        raise PlanError(f"extension: una de {', '.join(EXTENSIONES)}.")
    if tipo == "anadir" and ext.startswith("pasante"):
        raise PlanError("Una extrusión que añade material necesita profundidad (ciega o simétrica).")
    prof = _pos(peticion.get("profundidad"), "profundidad") if ext in ("ciega", "simetrica") else None
    pl = _plano_croquis(caras, peticion.get("plano") if peticion.get("plano") is not None else "")
    oblicuo = bool(pl.get("oblicuo"))
    marco = _marco_oblicuo(caras, pl, aristas, peticion.get("referencia")) if oblicuo else None
    k = pl.get("eje")
    c0 = pl.get("coord")
    eje = "XYZ"[k] if k is not None else None
    sentido = None
    if ext in ("ciega", "pasante"):
        sentido = str(peticion.get("sentido") or "").strip()
        sl = sentido.lower()
        if sl in ("fuera", "dentro"):
            if not any(x["id"] == pl["id"] for x in caras.get("caras_planas", [])):
                raise PlanError("sentido 'fuera'/'dentro' solo vale con el croquis en una cara de la pieza.")
            if not oblicuo:                       # en una cara normal a un eje, se traduce a ±eje
                hacia = pl["normal"][k] > 0
                sentido = ("+" if hacia == (sl == "fuera") else "-") + eje
            else:
                sentido = sl
        elif oblicuo:
            raise PlanError("sentido: 'fuera' (hacia el exterior de la cara) o 'dentro' (hacia el material); la cara "
                            "es oblicua.")
        else:
            sentido = sentido.upper()
            if sentido not in (f"+{eje}", f"-{eje}"):
                raise PlanError(f"sentido: +{eje} o -{eje}, o 'fuera'/'dentro' si el croquis va en una cara "
                                f"(el croquis está en un plano normal a {eje}).")
    planas = caras.get("caras_planas", [])
    if tipo == "quitar" and not planas:
        raise PlanError("La pieza no tiene material: un corte no quitaría nada. Empieza por una extrusión que añada.")

    cts, area = _preparar_contornos(peticion)

    if oblicuo:
        O, U, V = marco["O"], marco["U"], marco["V"]

        def p3(p):
            return [O[i] + p[0] * U[i] + p[1] * V[i] for i in range(3)]
        refs = marco["refs"]
        u = v = None
    else:
        u, v = pl["ejes_uv"]

        def p3(p):
            x = [0.0, 0.0, 0.0]
            x[k], x[u], x[v] = c0, p[0], p[1]
            return x
        refs = elegir_referencias(caras, pl["normal"])

    def fmt(vals):
        return " ".join(f"{x:.6f}" for x in vals)

    sk = _lineas_entidades(cts, p3, fmt)

    ftype, remove = TIPOS_EXT[tipo]
    modelo = caras["modelo"]["nombre"] + ".PRT"
    e = [f"e 386.387 i {ftype}", "e 386.388 i 1", "e 386.1956 i 917", f"e 386.2023 i {remove}", "e 386.3176 i 0"]
    if ext == "ciega":
        e += [f"e 386.515.428.430 i {FROM_NONE}", f"e 386.515.429.433 i {TO_BLIND}", f"e 386.515.429.434 d {prof:.4f}"]
    elif ext == "simetrica":
        e += [f"e 386.515.428.430 i {FROM_NONE}", f"e 386.515.429.433 i {TO_SYM}", f"e 386.515.429.434 d {prof:.4f}"]
    elif ext == "pasante":
        e += [f"e 386.515.428.430 i {FROM_NONE}", f"e 386.515.429.433 i {TO_ALL}"]
    else:
        e += [f"e 386.515.428.430 i {FROM_ALL}", f"e 386.515.429.433 i {TO_ALL}"]
    e += [f"e 386.447.450.451 r 5 {pl['id']}", "e 386.447.450.452 i 1", "e 386.447.450.454 i 1",
          f"e 386.447.450.455 r 5 {refs[0]['id']}", f"e 386.1964 w {nombre}"]
    cuerpo = caras.get("cuerpo_por_defecto")
    if cuerpo is not None:
        e += ["e 386.6314.6315 i 2", f"e 386.6314.6316 r 98 {cuerpo}"]

    lineas = [f"modelo {modelo}", *e]
    lineas += [f"sk_ref {r.get('tipo', 5)} {r['id']}" for r in refs]
    lineas += sk
    cotas = _cotas_boceto(cts, marco["ejes_ref"] if oblicuo else _ejes_de_referencia(refs, u, v))
    lineas += _lineas_cotas(cotas, p3, fmt)
    vol = area * prof if prof else None
    if oblicuo:
        # Sin comprobación de tramo (la cara no es normal a un eje): el sentido lo comprueba el volumen.
        # STD_DIRECTION = 1 va hacia la normal exterior de la cara (visto en 0.17).
        if ext in ("ciega", "pasante"):
            lineas += [f"sentido {1 if sentido == 'fuera' else -1}", "probar_sentidos"]
    elif ext in ("ciega", "pasante"):
        mas = sentido.startswith("+")
        # Con la vista del croquis por el lado 1, PRO_E_STD_DIRECTION = 1 va hacia la normal del plano o
        # de la cara (visto en Creo 12.4 el 2026-11-25: ALZADO +Z, cara +X y corte -Z en cara +Z).
        # Si no, la comprobación del rango lo detecta y se prueba el otro.
        a_favor = (pl["normal"][k] > 0) == mas
        lineas += [f"sentido {1 if a_favor else -1}", "probar_sentidos"]
        hasta = (c0 + prof if mas else c0 - prof) if prof else (LEJOS if mas else -LEJOS)
        lo, hi = sorted((c0, hasta))
        lineas.append(f"rango {eje} {lo:.6f} {hi:.6f}")
    elif ext == "simetrica":
        lineas.append(f"rango {eje} {c0 - prof / 2:.6f} {c0 + prof / 2:.6f}")
    if tipo == "anadir":
        lineas.append(f"volumen_max {vol:.6f}")
    elif vol:
        lineas.append(f"volumen_max {-vol:.6f}")
    else:
        lineas.append("volumen_signo -1")

    avisos = []
    if tipo == "anadir" and not planas and ext == "ciega":
        avisos.append("Primera operación de una pieza vacía: se comprueba que el volumen sea exactamente área × profundidad.")
    if oblicuo:
        croquis_en = f"{pl['que']} (oblicua, normal {[round(c, 4) for c in marco['n']]})"
        coords = {"u": marco["desc"]["u"], "v": marco["desc"]["v"], "origen_u0_v0": marco["desc"]["origen"]}
    else:
        nombres_uv = "".join("XYZ"[i] for i in (u, v))
        croquis_en = f"{pl['que']} ({eje} = {c0:g})"
        coords = f"({nombres_uv[0].lower()}, {nombres_uv[1].lower()})"
    resumen = {
        "modelo": modelo, "nombre": nombre,
        "operacion": "extrusión (añade material)" if tipo == "anadir" else "corte (quita material)",
        "croquis_en": croquis_en, "coordenadas_croquis": coords,
        "contornos": [{"n": i + 1, "descripcion": c["desc"], "area_mm2": round(c["area"], 3), "hueco": c["hueco"]}
                      for i, c in enumerate(cts)],
        "area_neta_mm2": round(area, 3),
        "extension": (f"ciega {prof:g} mm hacia {sentido}" + (" de la cara" if oblicuo else "") if ext == "ciega" else
                      f"simétrica {prof:g} mm ({prof / 2:g} a cada lado)" if ext == "simetrica" else
                      f"pasante hasta el final de la pieza hacia {sentido}" if ext == "pasante" else
                      "pasante a los dos lados"),
        "volumen_teorico_mm3": round(vol, 3) if vol else None,
        "referencias_croquis": [r.get("nombre") for r in refs],
        "cotas_boceto": [_describir_cota(ct, refs) for ct in cotas],
    }
    if oblicuo:
        avisos.append("Croquis en una cara oblicua: comprueba en la tabla el origen y los ejes u, v del croquis antes "
                      "de confirmar. El sentido se comprueba por el cambio de volumen.")
    refs_planas = [r["id"] for r in refs if r.get("tipo", 5) == 5]
    return {"plan": "\n".join(lineas) + "\n", "resumen": resumen, "avisos": avisos,
            "ids_caras": [pl["id"]] if any(x["id"] == pl["id"] for x in planas) else [],
            "ids_planos": refs_planas + ([] if any(x["id"] == pl["id"] for x in planas) else [pl["id"]])}


# --------------------------------------------------------------- revoluciones

ANG_FROM_NONE, ANG_TO_ANGLE, ANG_SYM = 1 << 18, 1 << 22, 1 << 29


def _momento_radial(c: dict[str, Any], eje_uv: int, pos_eje: float) -> float:
    """∫∫ r dA del contorno (r = distancia a la línea de centros), por integración de contorno con
    los arcos divididos finamente (error relativo < 1e-6)."""
    pts: list[tuple[float, float]] = []
    for sg in c["segs"]:
        if sg[0] == "L":
            pts.append(sg[1])
        else:
            if sg[0] == "C":
                cc, r, a1, sw = sg[1], sg[2], 0.0, 2 * math.pi
            else:
                _, cc, r, a1, sw = sg
            n = max(16, math.ceil(abs(sw) / (math.pi / 2000)))
            pts += [(cc[0] + r * math.cos(a1 + sw * i / n), cc[1] + r * math.sin(a1 + sw * i / n)) for i in range(n)]
    # Green: ∫∫ f(w) dA con w la coordenada radial; f = |w - p| con el contorno a un solo lado del eje
    tot = 0.0
    otro = 1 - eje_uv
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        wa, wb = a[eje_uv] - pos_eje, b[eje_uv] - pos_eje
        da = b[otro] - a[otro]
        # ∮ (w²/2) d(otro), con signo según la orientación de los ejes
        tot += (wa * wa + wa * wb + wb * wb) / 6 * da
    signo = 1 if eje_uv == 0 else -1          # ∮ x²/2 dy = ∫∫ x dA ; ∮ -y²/2 dx = ∫∫ y dA
    return abs(tot * signo)


def planificar_revolucion(peticion: dict[str, Any], caras: dict[str, Any], nombre: str) -> dict[str, Any]:
    """Revolución (añadir) o corte por revolución (quitar) de un perfil cerrado alrededor de una línea de
    centros del croquis, paralela a un eje de la pieza. No toca Creo."""
    tipo = str(peticion.get("tipo") or "")
    if tipo not in TIPOS_EXT:
        raise PlanError("tipo: 'anadir' (revolución, añade material) o 'quitar' (corte por revolución).")
    pl = _plano_croquis(caras, peticion.get("plano") if peticion.get("plano") is not None else "")
    if pl.get("oblicuo"):
        raise PlanError("Las revoluciones van en un plano o cara normal a X, Y o Z (el eje de giro es paralelo a un eje).")
    k, c0 = pl["eje"], pl["coord"]
    u, v = pl["ejes_uv"]
    eje_txt = str(peticion.get("eje") or "").upper().lstrip("+-")
    if eje_txt not in ("X", "Y", "Z") or "XYZ".index(eje_txt) == k:
        posibles = " o ".join("XYZ"[i] for i in (u, v))
        raise PlanError(f"eje: {posibles} (el eje de giro tiene que estar en el plano del croquis, normal a {'XYZ'[k]}).")
    ka = "XYZ".index(eje_txt)
    eje_uv_axial = (u, v).index(ka)            # coordenada del croquis a lo largo del eje
    eje_uv_radial = 1 - eje_uv_axial           # coordenada perpendicular (radial)
    pos_eje = float(peticion.get("eje_desplazado") or 0.0)
    angulo = float(peticion.get("angulo") or 360.0)
    if not 0 < angulo <= 360:
        raise PlanError("angulo: entre 0 y 360 grados.")
    planas = caras.get("caras_planas", [])
    if tipo == "quitar" and not planas:
        raise PlanError("La pieza no tiene material: un corte no quitaría nada. Empieza por una revolución que añada.")
    cts, area = _preparar_contornos(peticion)
    # todo el perfil a un lado de la línea de centros (puede tocarla)
    lados = set()
    for i, c in enumerate(cts):
        for p in c["poli"]:
            w = p[eje_uv_radial] - pos_eje
            if abs(w) > 1e-6:
                lados.add(w > 0)
        for sg in c["segs"]:
            if sg[0] in ("A", "C"):
                cc, r = sg[1], sg[2]
                if abs(cc[eje_uv_radial] - pos_eje) < r - 1e-6 and sg[0] == "C":
                    raise PlanError(f"contorno {i + 1}: el círculo corta la línea de centros.")
    if len(lados) > 1:
        raise PlanError("El perfil cruza la línea de centros: dibuja solo la mitad de la sección, a un lado del eje.")
    momento = sum(-_momento_radial(c, eje_uv_radial, pos_eje) if c["hueco"] else _momento_radial(c, eje_uv_radial, pos_eje)
                  for c in cts)
    vol = momento * math.radians(angulo)

    def p3(p):
        x = [0.0, 0.0, 0.0]
        x[k], x[u], x[v] = c0, p[0], p[1]
        return x

    def fmt(vals):
        return " ".join(f"{x:.6f}" for x in vals)

    sk = _lineas_entidades(cts, p3, fmt)
    n_ent = len(sk)
    # línea de centros (última entidad): a lo largo del eje, por el eje desplazado
    lo_ax = min(p[eje_uv_axial] for c in cts for p in c["poli"])
    hi_ax = max(p[eje_uv_axial] for c in cts for p in c["poli"])
    a2, b2 = [0.0, 0.0], [0.0, 0.0]
    a2[eje_uv_radial] = b2[eje_uv_radial] = pos_eje
    a2[eje_uv_axial], b2[eje_uv_axial] = lo_ax - 10, hi_ax + 10
    sk.append(f"sk_eje {fmt(p3(a2) + p3(b2))}")

    refs = elegir_referencias(caras, pl["normal"])
    ftype, remove = TIPOS_EXT[tipo]
    modelo = caras["modelo"]["nombre"] + ".PRT"
    e = [f"e 386.387 i {ftype}", "e 386.388 i 2", "e 386.1956 i 917", f"e 386.2023 i {remove}", "e 386.3176 i 0",
         "e 386.3280 i 1"]                           # eje: línea de centros del croquis
    if angulo >= 360:
        e += [f"e 386.6.437.439 i {ANG_FROM_NONE}", f"e 386.6.438.440 i {ANG_TO_ANGLE}", "e 386.6.438.442 d 360.0000"]
    else:
        e += [f"e 386.6.437.439 i {ANG_FROM_NONE}", f"e 386.6.438.440 i {ANG_SYM}", f"e 386.6.438.442 d {angulo:.4f}"]
    e += [f"e 386.447.450.451 r 5 {pl['id']}", "e 386.447.450.452 i 1", "e 386.447.450.454 i 1",
          f"e 386.447.450.455 r 5 {refs[0]['id']}", f"e 386.1964 w {nombre}"]
    cuerpo = caras.get("cuerpo_por_defecto")
    if cuerpo is not None:
        e += ["e 386.6314.6315 i 2", f"e 386.6314.6316 r 98 {cuerpo}"]
    lineas = [f"modelo {modelo}", *e]
    lineas += [f"sk_ref 5 {r['id']}" for r in refs]
    lineas += sk
    ejes_ref = _ejes_de_referencia(refs, u, v)
    cotas = _cotas_boceto(cts, ejes_ref, radial=(eje_uv_radial, pos_eje, n_ent))
    lineas += _lineas_cotas(cotas, p3, fmt)
    # Sin comprobación de tramo: un corte por revolución parte las caras cilíndricas que atraviesa y
    # Creo da los trozos nuevos a la operación (garganta Y 12-15 en un Ø20 de Y 0-30: caras de 0 a 15,
    # visto el 2026-11-26). Una revolución de 360° no tiene sentido que elegir; basta el volumen.
    lineas.append(f"volumen_max {vol if tipo == 'anadir' else -vol:.6f}")

    avisos = []
    if tipo == "anadir" and not planas:
        avisos.append("Primera operación de una pieza vacía: se comprueba que el volumen sea el del teorema de Pappus.")
    nombres_uv = "".join("XYZ"[i] for i in (u, v))
    radial_txt = "XYZ"[(u, v)[eje_uv_radial]]
    resumen = {
        "modelo": modelo, "nombre": nombre,
        "operacion": "revolución (añade material)" if tipo == "anadir" else "corte por revolución (quita material)",
        "croquis_en": f"{pl['que']} ({'XYZ'[k]} = {c0:g})",
        "coordenadas_croquis": f"({nombres_uv[0].lower()}, {nombres_uv[1].lower()})",
        "eje_de_giro": f"paralelo a {eje_txt}, en {radial_txt.lower()} = {pos_eje:g}",
        "angulo": f"{angulo:g}°" + ("" if angulo >= 360 else " (simétrico respecto al plano del croquis)"),
        "contornos": [{"n": i + 1, "descripcion": c["desc"], "area_mm2": round(c["area"], 3), "hueco": c["hueco"]}
                      for i, c in enumerate(cts)],
        "area_neta_mm2": round(area, 3),
        "volumen_teorico_mm3": round(vol, 3),
        "referencias_croquis": [r.get("nombre") for r in refs],
        "cotas_boceto": [_describir_cota(ct, refs) for ct in cotas],
    }
    return {"plan": "\n".join(lineas) + "\n", "resumen": resumen, "avisos": avisos,
            "ids_caras": [pl["id"]] if any(x["id"] == pl["id"] for x in planas) else [],
            "ids_planos": [r["id"] for r in refs] + ([] if any(x["id"] == pl["id"] for x in planas) else [pl["id"]])}
