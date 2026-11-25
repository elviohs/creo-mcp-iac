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

from .operaciones import PlanError, elegir_referencias, orientacion

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
        raise PlanError(f"El {que} no es perpendicular a un eje de la pieza: de momento el croquis solo va en "
                        "planos o caras normales a X, Y o Z.")
    k = "XYZ".index(ori[1])
    ejes = [i for i in range(3) if i != k]
    return {"id": pid, "que": que, "normal": [float(x) for x in n], "orientacion": ori, "eje": k,
            "coord": float(o[k]), "ejes_uv": ejes}


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
    if ct[0] in ("ref", "refl"):
        ref = ct[3] if ct[0] == "ref" else ct[2]
        val = ct[4] if ct[0] == "ref" else ct[3]
        que = {"c": "centro", "s": "vértice", "e": "vértice", None: "lado"}.get(ct[2] if ct[0] == "ref" else None)
        return f"{que} a {val:g} de {refs[ref].get('nombre')}"
    return f"{ct[3]:g} entre lados"


def _cotas_boceto(cts: list[dict[str, Any]], ejes_ref: dict[int, tuple[int, float]]) -> list[tuple]:
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
        """Cota desde la referencia del eje 0 (u) o 1 (v)."""
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
            if iz is not None and de is not None:
                out.append(("entre", iz, de, c["x1"] - c["x0"], ((c["x0"] + c["x1"]) / 2, c["y1"] + 8)))
            if ab is not None and ar is not None:
                out.append(("entre", ab, ar, c["y1"] - c["y0"], (c["x1"] + 8, (c["y0"] + c["y1"]) / 2)))
            if iz is not None:
                pos(0, c["x0"], iz, None, (c["y0"] + c["y1"]) / 2, recta=True)
            if ab is not None:
                pos(1, c["y0"], ab, None, (c["x0"] + c["x1"]) / 2, recta=True)
            hechas[0].add(round(c["x1"], 6))
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


def planificar_extrusion(peticion: dict[str, Any], caras: dict[str, Any], nombre: str) -> dict[str, Any]:
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
    k, c0 = pl["eje"], pl["coord"]
    eje = "XYZ"[k]
    sentido = None
    if ext in ("ciega", "pasante"):
        sentido = str(peticion.get("sentido") or "").upper()
        if sentido not in (f"+{eje}", f"-{eje}"):
            raise PlanError(f"sentido: +{eje} o -{eje} (el croquis está en un plano normal a {eje}).")
    planas = caras.get("caras_planas", [])
    if tipo == "quitar" and not planas:
        raise PlanError("La pieza no tiene material: un corte no quitaría nada. Empieza por una extrusión que añada.")

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

    u, v = pl["ejes_uv"]

    def p3(p):
        x = [0.0, 0.0, 0.0]
        x[k], x[u], x[v] = c0, p[0], p[1]
        return x

    def fmt(vals):
        return " ".join(f"{x:.6f}" for x in vals)

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

    refs = elegir_referencias(caras, pl["normal"])
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
    lineas += [f"sk_ref 5 {r['id']}" for r in refs]
    lineas += sk
    # referencia que mide cada coordenada del croquis (u, v): plano normal a ese eje
    ejes_ref: dict[int, tuple[int, float]] = {}
    for i_r, r in enumerate(refs):
        nr, orr = r.get("normal"), r.get("origen")
        if not nr or not orr:
            continue
        for e_i, k_uv in enumerate((u, v)):
            if abs(abs(nr[k_uv]) - 1) < 1e-9 and e_i not in ejes_ref:
                ejes_ref[e_i] = (i_r, float(orr[k_uv]))
    cotas = _cotas_boceto(cts, ejes_ref)
    for ct in cotas:
        lugar = fmt(p3(ct[-1]))
        if ct[0] in ("diam", "radio"):
            lineas.append(f"sk_cota {ct[0]} {ct[1]} {ct[2]:.6f} {lugar}")
        elif ct[0] == "ref":
            lineas.append(f"sk_cota ref {ct[1]} {ct[2]} {ct[3]} {ct[4]:.6f} {lugar}")
        elif ct[0] == "refl":
            lineas.append(f"sk_cota refl {ct[1]} {ct[2]} {ct[3]:.6f} {lugar}")
        else:
            lineas.append(f"sk_cota entre {ct[1]} {ct[2]} {ct[3]:.6f} {lugar}")
    vol = area * prof if prof else None
    if ext in ("ciega", "pasante"):
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
    nombres_uv = "".join("XYZ"[i] for i in (u, v))
    resumen = {
        "modelo": modelo, "nombre": nombre,
        "operacion": "extrusión (añade material)" if tipo == "anadir" else "corte (quita material)",
        "croquis_en": f"{pl['que']} ({eje} = {c0:g})", "coordenadas_croquis": f"({nombres_uv[0].lower()}, {nombres_uv[1].lower()})",
        "contornos": [{"n": i + 1, "descripcion": c["desc"], "area_mm2": round(c["area"], 3), "hueco": c["hueco"]}
                      for i, c in enumerate(cts)],
        "area_neta_mm2": round(area, 3),
        "extension": (f"ciega {prof:g} mm hacia {sentido}" if ext == "ciega" else
                      f"simétrica {prof:g} mm ({prof / 2:g} a cada lado)" if ext == "simetrica" else
                      f"pasante hasta el final de la pieza hacia {sentido}" if ext == "pasante" else
                      "pasante a los dos lados"),
        "volumen_teorico_mm3": round(vol, 3) if vol else None,
        "referencias_croquis": [r.get("nombre") for r in refs],
        "cotas_boceto": [_describir_cota(ct, refs) for ct in cotas],
    }
    return {"plan": "\n".join(lineas) + "\n", "resumen": resumen, "avisos": avisos,
            "ids_caras": [pl["id"]] if any(x["id"] == pl["id"] for x in planas) else [],
            "ids_planos": [r["id"] for r in refs] + ([] if any(x["id"] == pl["id"] for x in planas) else [pl["id"]])}
