"""Operaciones sencillas en piezas de Creo (escritura): taladros rectos y normalizados.

Flujo:
  1. `caras` (lectura nativa) da las caras planas, los planos de referencia y el cuerpo.
  2. `planificar_taladro` convierte una petición («M6 roscado en la cara superior, en este
     punto») en un plan: el árbol de elementos del taladro en el orden en que lo guarda Creo,
     con los valores de la tabla de roscas (.hol) y la colocación lineal respecto a dos planos.
  3. El programa nativo (`crear --plan`) crea la operación, comprueba que el eje pasa por el
     punto pedido y, si no, la borra y prueba con el otro signo de las distancias.

Las plantillas salen de taladros hechos a mano en Creo 12.4.1.0 (PRUEBA_CMD.PRT, 2026-11-23):
recto Ø8 pasante, ISO M6x1 roscado (broca 15, rosca 12) e ISO M5x.8 taladrado pasante.
Identificadores de elementos: ProElemId.h.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Métricas gruesas (paso normal) para «M6» sin paso.
PASO_NORMAL = {1.6: .35, 2: .4, 2.5: .45, 3: .5, 4: .7, 5: .8, 6: 1, 8: 1.25, 10: 1.5, 12: 1.75,
               14: 2, 16: 2, 20: 2.5, 24: 3}
NOMBRES_PLANOS = ("PERFIL", "ALZADO", "PLANTA", "RIGHT", "FRONT", "TOP")
AJUSTES = {"fino": 0, "libre": 1, "medio": 2}
ALT_DIST = "386.1340.706 386.1340.708"     # PRO_E_HLE_DIM_DIST1 y DIST2
_NOMBRE_RE = re.compile(r"[^A-Z0-9_]")


class PlanError(ValueError):
    pass


# --------------------------------------------------------------- tablas .hol

@dataclass(frozen=True)
class FilaHol:
    tamano: str          # FASTENER_ID, p. ej. M6x1
    nominal: float
    paso: float
    broca_rosca: float   # TAP_DR
    holgura: dict[str, float]   # fino/libre/medio
    cajeado: tuple[float, float] = (0.0, 0.0)      # CBOREDIAM, CBOREDEPTH (0 = no hay dato)
    avellanado: tuple[float, float] = (0.0, 90.0)  # CSINKDIAM, CSINKANGLE


def _num(c: dict[str, str], k: str, defecto: float = 0.0) -> float:
    try:
        return float(c.get(k, defecto))
    except ValueError:
        return defecto


def leer_hol(ruta: Path) -> dict[str, FilaHol]:
    """Lee una tabla .hol de Creo (p. ej. text/hole/ISO.hol)."""
    filas: dict[str, FilaHol] = {}
    cab: list[str] | None = None
    for linea in ruta.read_text(encoding="latin-1").splitlines():
        t = linea.split()
        if not t:
            continue
        if t[0] == "FASTENER_ID":
            cab = t
            continue
        if cab and len(t) >= len(cab) - 2 and re.match(r"^M\d", t[0]):
            c = dict(zip(cab, t))
            try:
                filas[t[0].upper()] = FilaHol(
                    t[0], float(c["BASIC_DIAM"]), float(c["THREAD"]), float(c["TAP_DR"]),
                    {"fino": float(c["CLEAR_DR_CLOSE"]), "libre": float(c["CLEAR_DR_FREE"]),
                     "medio": float(c["CLEAR_DR_MED"])},
                    (_num(c, "CBOREDIAM"), _num(c, "CBOREDEPTH")),
                    (_num(c, "CSINKDIAM"), _num(c, "CSINKANGLE", 90.0)))
            except (KeyError, ValueError):
                continue
    if not filas:
        raise PlanError(f"No se han podido leer tamaños de {ruta}")
    return filas


def buscar_tamano(tabla: dict[str, FilaHol], metrica: str) -> FilaHol:
    """'M6x1', 'M6X1', 'M6' (paso normal), 'M2.5', 'M2,5x0.45' -> fila de la tabla."""
    m = re.match(r"^\s*M\s*(\d+(?:[.,]\d+)?)\s*(?:[xX×]\s*(\d*[.,]?\d+))?\s*$", metrica or "", re.IGNORECASE)
    if not m:
        raise PlanError(f"Métrica no reconocida: {metrica!r} (ejemplos: M6, M6x1, M2.5)")
    nominal = float(m.group(1).replace(",", "."))
    paso = float(m.group(2).replace(",", ".")) if m.group(2) else PASO_NORMAL.get(nominal)
    if paso is None:
        raise PlanError(f"Indica el paso de {metrica} (no es una métrica normal de la tabla).")
    for f in tabla.values():
        if abs(f.nominal - nominal) < 1e-6 and abs(f.paso - paso) < 1e-6:
            return f
    raise PlanError(f"M{nominal:g}x{paso:g} no está en la tabla de roscas de Creo.")


# --------------------------------------------------------------- geometría

def _sub(a, b): return [a[i] - b[i] for i in range(3)]
def _dot(a, b): return sum(a[i] * b[i] for i in range(3))


def _unit(v):
    n = math.sqrt(_dot(v, v)) or 1.0
    return [c / n for c in v]


def resolver_cara(caras: dict[str, Any], cara: int | str) -> dict[str, Any]:
    planas = caras.get("caras_planas", [])
    if isinstance(cara, int) or (isinstance(cara, str) and cara.strip().isdigit()):
        cid = int(cara)
        for c in planas:
            if c["id"] == cid:
                return c
        raise PlanError(f"La cara {cid} no es una cara plana de la pieza.")
    ejes = {"+X": [1, 0, 0], "-X": [-1, 0, 0], "+Y": [0, 1, 0], "-Y": [0, -1, 0], "+Z": [0, 0, 1], "-Z": [0, 0, -1]}
    d = ejes.get(str(cara).strip().upper())
    if d is None:
        raise PlanError("La cara se indica por su id (caras_creo o seleccion_creo) o como +X, -X, +Y, -Y, +Z, -Z.")
    cand = [c for c in planas if "normal_exterior" in c and _dot(c["normal_exterior"], d) > 0.9999]
    if not cand:
        raise PlanError(f"No hay ninguna cara plana con la normal hacia {cara}.")
    # la más exterior en esa dirección; a igualdad, la de mayor área
    cand.sort(key=lambda c: (-_dot(c["punto_plano"], d), -c.get("area", 0)))
    return cand[0]


def elegir_referencias(caras: dict[str, Any], normal: list[float], nombres: list[str] | None = None):
    """Dos planos de referencia perpendiculares a la cara y no paralelos entre sí."""
    planos = [p for p in caras.get("planos_referencia", [])
              if "normal" in p and abs(_dot(p["normal"], normal)) < 1e-6]
    if nombres:
        sel = []
        for n in nombres:
            p = next((x for x in planos if x.get("nombre", "").upper() == n.upper()), None)
            if p is None:
                raise PlanError(f"El plano {n} no existe o no es perpendicular a la cara.")
            sel.append(p)
    else:
        nombrados = [p for p in planos if p.get("nombre", "").lower() != "no_name"]
        pref = {n: i for i, n in enumerate(NOMBRES_PLANOS)}
        nombrados.sort(key=lambda p: pref.get(p.get("nombre", "").upper(), 99))
        sel = []
        for p in nombrados:
            if all(abs(_dot(p["normal"], q["normal"])) < 0.999 for q in sel):
                sel.append(p)
            if len(sel) == 2:
                break
        if len(sel) < 2:
            # caras planas perpendiculares a la cara como último recurso
            for c in caras.get("caras_planas", []):
                nc = c.get("normal_exterior")
                if nc and abs(_dot(nc, normal)) < 1e-6 and all(abs(_dot(nc, q["normal"])) < 0.999 for q in sel):
                    sel.append({"nombre": f"cara {c['id']}", "id": c["id"], "normal": nc, "origen": c["punto_plano"]})
                if len(sel) == 2:
                    break
    if len(sel) != 2:
        raise PlanError("No hay dos planos de referencia perpendiculares a la cara para acotar el taladro.")
    return sel


def _dentro(cara: dict[str, Any], p: list[float], tol: float = 1e-3) -> bool:
    a, b = cara.get("caja_min"), cara.get("caja_max")
    if not a or not b:
        return True
    return all(min(a[i], b[i]) - tol <= p[i] <= max(a[i], b[i]) + tol for i in range(3))


def _distancia_borde(cara: dict[str, Any], p: list[float], normal: list[float]) -> float | None:
    a, b = cara.get("caja_min"), cara.get("caja_max")
    if not a or not b:
        return None
    ds = [min(p[i] - min(a[i], b[i]), max(a[i], b[i]) - p[i]) for i in range(3) if abs(normal[i]) < 0.5]
    return min(ds) if ds else None


# --------------------------------------------------------------- plan

def _rebaje(pet: dict[str, Any], fila: FilaHol, broca: float, prof: float | None):
    """(cajeado, avellanado): (Ø, profundidad) y (Ø, ángulo), o None. Por defecto, la tabla .hol."""
    cb = cs = None
    if pet.get("cajeado") and pet.get("avellanado"):
        raise PlanError("Elige cajeado o avellanado, no los dos en el mismo taladro.")
    if pet.get("cajeado"):
        d = float(pet.get("cajeado_diametro") or fila.cajeado[0])
        h = float(pet.get("cajeado_profundidad") or fila.cajeado[1])
        if d <= 0 or h <= 0:
            raise PlanError(f"La tabla de Creo no da cajeado para {fila.tamano}: indica cajeado_diametro y "
                            "cajeado_profundidad.")
        if d <= broca:
            raise PlanError(f"El cajeado (Ø{d:g}) debe ser mayor que el taladro (Ø{broca:g}).")
        if prof is not None and h >= prof:
            raise PlanError(f"El cajeado ({h:g} mm) debe ser menos profundo que el taladro ({prof:g} mm).")
        cb = (d, h)
    if pet.get("avellanado"):
        d = float(pet.get("avellanado_diametro") or fila.avellanado[0])
        a = float(pet.get("avellanado_angulo") or fila.avellanado[1] or 90)
        if d <= 0:
            raise PlanError(f"La tabla de Creo no da avellanado para {fila.tamano}: indica avellanado_diametro.")
        if d <= broca:
            raise PlanError(f"El avellanado (Ø{d:g}) debe ser mayor que el taladro (Ø{broca:g}).")
        if not 30 <= a <= 150:
            raise PlanError("avellanado_angulo entre 30 y 150° (90° es el normal en métrica).")
        cs = (d, a)
    return cb, cs


def nombre_operacion(base: str, sufijo: str) -> str:
    n = _NOMBRE_RE.sub("_", f"IA_{base}_{sufijo}".upper())
    return n[:31]


def planificar_taladro(peticion: dict[str, Any], caras: dict[str, Any], tabla: dict[str, FilaHol] | None,
                       nombre: str) -> dict[str, Any]:
    """Devuelve {'plan': texto, 'resumen': {...}, 'avisos': [...]}. No toca Creo."""
    tipo = str(peticion.get("tipo", "")).lower()
    if tipo not in ("recto", "roscado", "holgura", "taladrado"):
        raise PlanError("tipo: recto, roscado, holgura o taladrado.")
    modelo = caras["modelo"]["nombre"] + ".PRT"
    cara = resolver_cara(caras, peticion.get("cara", "+Z"))
    n = _unit(cara["normal_exterior"])
    p = peticion.get("punto")
    if not (isinstance(p, (list, tuple)) and len(p) == 3):
        raise PlanError("punto: [x, y, z] en coordenadas de la pieza (la coordenada normal a la cara se ignora).")
    p = [float(v) for v in p]
    o = cara["punto_plano"]
    k = _dot(_sub(p, o), n)
    p = [p[i] - k * n[i] for i in range(3)]          # proyección sobre la cara
    avisos = []
    if not _dentro(cara, p):
        raise PlanError(f"El punto {[round(v, 3) for v in p]} cae fuera de la cara {cara['id']}.")
    refs = elegir_referencias(caras, n, peticion.get("referencias"))
    dist = [_dot(_sub(p, r["origen"]), _unit(r["normal"])) for r in refs]
    cuerpo = caras.get("cuerpo_por_defecto")

    prof = peticion.get("profundidad", "pasante")
    pasante = isinstance(prof, str) and prof.lower().startswith("pasante")
    if not pasante:
        try:
            prof = float(prof)
        except (TypeError, ValueError):
            raise PlanError("profundidad: 'pasante' o un número (mm).")
        if prof <= 0:
            raise PlanError("La profundidad debe ser positiva.")

    e: list[str] = ["e 386.387 i 911"]
    resumen: dict[str, Any] = {"modelo": modelo, "cara": cara["id"], "punto": [round(v, 4) for v in p],
                               "referencias": [f"{r['nombre']} a {d:+.3f}" for r, d in zip(refs, dist)]}
    serie = tamano = None
    if tipo == "recto":
        d = float(peticion.get("diametro") or 0)
        if d <= 0:
            raise PlanError("diametro: obligatorio para un taladro recto.")
        if peticion.get("cajeado") or peticion.get("avellanado"):
            raise PlanError("Cajeado y avellanado solo en taladros normalizados (holgura, roscado o taladrado), "
                            "no en un recto: usa tipo holgura con la métrica del tornillo.")
        e += ["e 386.388 i 1", "e 386.1671.1672 i 16", "e 386.1671.3887 i -1", f"e 386.1671.82 d {d:.4f}",
              f"e 386.1671.1684.1686.1688 i {3 if pasante else 1}"]
        if not pasante:
            e.append(f"e 386.1671.1684.1686.434 d {prof:.4f}")
        e += ["e 386.1671.1684.1685.1687 i 6", "e 386.1671.6900 i -1", "e 386.1671.3750 i 9",
              "e 386.1671.6053 i -1"]
        resumen.update(tipo="recto", diametro=d, profundidad="pasante" if pasante else prof)
        base = f"D{d:g}"
    else:
        if tabla is None:
            raise PlanError("No está disponible la tabla de roscas ISO.hol de Creo.")
        fila = buscar_tamano(tabla, str(peticion.get("metrica", "")))
        serie, tamano = "ISO", fila.tamano
        estandar = {"roscado": 14, "holgura": 15, "taladrado": 17}[tipo]
        ajuste = str(peticion.get("ajuste", "medio")).lower()
        if tipo == "holgura" and ajuste not in AJUSTES:
            raise PlanError("ajuste: fino, medio o libre.")
        if tipo == "holgura":
            broca = fila.holgura[ajuste]
            if broca <= 0:
                raise PlanError(f"{fila.tamano} no tiene diámetro de holgura «{ajuste}» en la tabla.")
            if not pasante:
                raise PlanError("Los taladros de holgura normalizados son pasantes en Creo.")
        else:
            broca = fila.broca_rosca
        cb, cs = _rebaje(peticion, fila, broca, None if pasante else prof)
        e += ["e 386.388 i 2", "e 386.1671.1672 i 24", f"e 386.1671.1673 i {estandar}", "e 386.1671.1356 i 0",
              f"e 386.1671.1661 i {AJUSTES[ajuste] if tipo == 'holgura' else 0}", "e 386.1671.1358 i 0",
              f"e 386.1671.1674 i {26 if tipo == 'roscado' else -1}",
              f"e 386.1671.1675 i {27 if cb else -1}", f"e 386.1671.1676 i {28 if cs else -1}",
              f"e 386.1671.1347 d {broca:.4f}", f"e 386.1671.1662 i {31 if pasante else 29}"]
        # orden de ProHole.h: CSINKANGLE, CBOREDEPTH, CBOREDIAM, CSINKDIAM
        if cs:
            e.append(f"e 386.1671.1352 d {cs[1]:.4f}")
        if cb:
            e += [f"e 386.1671.1349 d {cb[1]:.4f}", f"e 386.1671.1350 d {cb[0]:.4f}"]
        if cs:
            e.append(f"e 386.1671.1351 d {cs[0]:.4f}")
        rosca = None
        rosca_pasante = False
        if tipo == "roscado":
            r = peticion.get("rosca", "pasante" if pasante else None)
            if r is None:
                r = round(min(prof, 1.5 * fila.nominal), 3)
                avisos.append(f"Profundidad de rosca no indicada: {r:g} mm (1,5·d, sin pasar del taladro).")
            rosca_pasante = isinstance(r, str) and r.lower().startswith("pasante")
            if rosca_pasante and not pasante:
                raise PlanError("Una rosca pasante necesita un taladro pasante.")
            if not rosca_pasante:
                try:
                    rosca = float(r)
                except (TypeError, ValueError):
                    raise PlanError("rosca: 'pasante' o un número (mm).")
                if rosca <= 0 or (not pasante and rosca > prof):
                    raise PlanError("La rosca debe ser positiva y no más profunda que el taladro.")
        if not pasante:
            e.append("e 386.1671.3741 i -1")                       # cota hasta el hombro
        if tipo == "roscado":
            e.append(f"e 386.1671.1680 i {32 if rosca_pasante else 33}")
        # THRDDEPTH y DRILLDEPTH son obligatorios aunque no se usen (ProHole.h, tabla 2)
        e.append(f"e 386.1671.1354 d {(rosca or 0):.4f}")
        if not pasante:
            e.append("e 386.1671.1348 d 118.0000")
        e.append(f"e 386.1671.1663 d {(prof if not pasante else max(3 * broca, 10)):.4f}")
        e.append("e 386.1671.1689 i -1")
        if pasante:
            e.append("e 386.1671.3110 i -1")
        e += ["e 386.1671.3750 i -1", "e 386.1671.6053 i -1"]
        resumen.update(tipo=f"normalizado ISO {tamano} {tipo}", broca=broca,
                       profundidad="pasante" if pasante else prof)
        if tipo == "holgura":
            resumen["ajuste"] = ajuste
        if tipo == "roscado":
            resumen["rosca"] = "pasante" if rosca is None else rosca
        if cb:
            resumen["cajeado"] = {"diametro": cb[0], "profundidad": cb[1]}
        if cs:
            resumen["avellanado"] = {"diametro": cs[0], "angulo": cs[1]}
        base = tamano + ("_CB" if cb else "_CS" if cs else "")

    r1, r2 = refs
    e += [f"e 386.1340.1341 r 5 {cara['id']}", "e 386.1340.696 i 1",
          f"e 386.1340.704 r 5 {r1['id']}", "e 386.1340.3194 i 0", f"e 386.1340.706 d {dist[0]:.4f}",
          f"e 386.1340.707 r 5 {r2['id']}", "e 386.1340.3195 i 0", f"e 386.1340.708 d {dist[1]:.4f}",
          f"e 386.1964 w {nombre}"]
    if cuerpo is not None:
        e += ["e 386.6314.6315 i 2", f"e 386.6314.6316 r 98 {cuerpo}"]
    diam_borde = float(peticion.get("diametro") or 0) if tipo == "recto" else broca
    borde = _distancia_borde(cara, p, n)
    if borde is not None and borde < diam_borde:
        avisos.append(f"El taladro queda a {borde:.2f} mm del borde de la cara (Ø {diam_borde:g}).")
    lineas = [f"modelo {modelo}", *([f"serie {serie}", f"tamano {tamano}"] if serie else []), *e,
              f"verificar {p[0]:.6f} {p[1]:.6f} {p[2]:.6f} {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}",
              f"alternar {ALT_DIST}"]
    resumen["nombre"] = nombre
    return {"plan": "\n".join(lineas) + "\n", "resumen": resumen, "avisos": avisos, "base_nombre": base,
            "ids_caras": [cara["id"]], "ids_planos": [r1["id"], r2["id"]]}


# --------------------------------------------------------------- patrones

EJES = {"+X": [1, 0, 0], "-X": [-1, 0, 0], "+Y": [0, 1, 0], "-Y": [0, -1, 0], "+Z": [0, 0, 1], "-Z": [0, 0, -1]}


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _girar(p, a, d, ang):
    """Gira el punto p un ángulo ang (rad) alrededor del eje que pasa por a con dirección d."""
    d = _unit(d)
    v = _sub(p, a)
    c, s_ = math.cos(ang), math.sin(ang)
    kxv = _cross(d, v)
    kv = _dot(d, v)
    return [a[i] + v[i] * c + kxv[i] * s_ + d[i] * kv * (1 - c) for i in range(3)]


def _radio_exterior(op: dict[str, Any]) -> float:
    ds = [op.get("diametro") or 0]
    for k in ("avellanado_cilindrico", "avellanado_conico"):
        if isinstance(op.get(k), dict) and isinstance(op[k].get("diametro"), (int, float)):
            ds.append(op[k]["diametro"])
    return max(ds) / 2


def _referencia_direccion(caras: dict[str, Any], d: list[float]) -> tuple[str, str]:
    """(texto de referencia para el plan, nombre) para una dirección: eje de referencia paralelo
    o, si no hay, plano de referencia perpendicular."""
    for e in caras.get("ejes_referencia", []):
        if "direccion" in e and abs(abs(_dot(_unit(e["direccion"]), d)) - 1) < 1e-6:
            return f"r 21 {e['id']}", f"eje {e.get('nombre')}"
    for p in caras.get("planos_referencia", []):
        if p.get("nombre", "").lower() != "no_name" and "normal" in p and abs(abs(_dot(p["normal"], d)) - 1) < 1e-6:
            return f"r 5 {p['id']}", f"normal a {p.get('nombre')}"
    raise PlanError("No hay ningún eje ni plano de referencia en esa dirección para orientar el patrón.")


def planificar_patron(peticion: dict[str, Any], caras: dict[str, Any], guia: dict[str, Any],
                      otros: list[dict[str, Any]], nombre: str) -> dict[str, Any]:
    """Patrón lineal (1 o 2 direcciones) o circular de un taladro. `guia` y `otros` son taladros
    resumidos (taladros_creo); `guia["cara"]` es el id de su cara de colocación. No toca Creo."""
    tipo = str(peticion.get("tipo", "")).lower()
    if tipo not in ("lineal", "circular"):
        raise PlanError("tipo: lineal o circular.")
    if guia.get("clase") != "taladro" or not guia.get("eje"):
        raise PlanError(f"La operación {guia.get('id')} no es un taladro con eje: de momento solo se hacen patrones de taladros.")
    modelo = caras["modelo"]["nombre"] + ".PRT"
    cara = resolver_cara(caras, guia["cara"])
    n = _unit(cara["normal_exterior"])
    p0 = guia["eje"]["punto"]
    k = _dot(_sub(p0, cara["punto_plano"]), n)
    p0 = [p0[i] - k * n[i] for i in range(3)]                       # centro sobre la cara
    r = _radio_exterior(guia)
    e: list[str] = ["e 604.3124 i " + ("5" if tipo == "lineal" else "6")]
    resumen: dict[str, Any] = {"modelo": modelo, "guia": guia["id"], "guia_nombre": guia.get("nombre"),
                               "cara": cara["id"], "tipo": tipo}
    puntos: list[list[float]] = []
    alternar = ""
    if tipo == "lineal":
        dirs = []
        for i in (1, 2):
            d = peticion.get(f"direccion{i}")
            if d is None:
                continue
            u = EJES.get(str(d).strip().upper())
            if u is None:
                raise PlanError(f"direccion{i}: +X, -X, +Y, -Y, +Z o -Z.")
            if abs(_dot(u, n)) > 1e-6:
                raise PlanError(f"direccion{i} ({d}) no es paralela a la cara del taladro.")
            sep = float(peticion.get(f"separacion{i}") or 0)
            cant = int(peticion.get(f"cantidad{i}") or 0)
            if sep <= 0 or cant < 2:
                raise PlanError(f"separacion{i} > 0 y cantidad{i} ≥ 2 (incluido el taladro guía).")
            dirs.append((str(d).strip().upper(), u, sep, cant))
        if not dirs:
            raise PlanError("Indica al menos direccion1, separacion1 y cantidad1.")
        if len(dirs) == 2 and abs(_dot(dirs[0][1], dirs[1][1])) > 1e-6:
            raise PlanError("Las dos direcciones deben ser perpendiculares.")
        n1, n2 = dirs[0][3], dirs[1][3] if len(dirs) == 2 else 1
        for a in range(n1):
            for b in range(n2):
                q = [p0[i] + a * dirs[0][2] * dirs[0][1][i] for i in range(3)]
                if len(dirs) == 2:
                    q = [q[i] + b * dirs[1][2] * dirs[1][1][i] for i in range(3)]
                puntos.append(q)
        etiquetas = []
        for i, (dn, u, sep, cant) in enumerate(dirs, 1):
            ref, etiqueta = _referencia_direccion(caras, u)
            sub = "3291" if i == 1 else "3292"
            e += [f"e 604.3129.{sub}.3252.3253 {ref}", f"e 604.3129.{sub}.3252.3254 i 1",
                  f"e 604.3129.{3273 + i} d {sep:.4f}", f"e 604.3129.{3306 + i} i 0",
                  f"e 604.3129.{4025 + i} i -1"]
            etiquetas.append(f"{cant} × {sep:g} mm hacia {dn} ({etiqueta})")
        e += [f"e 604.3129.3134 i {n1}", f"e 604.3129.3136 i {n2}"]
        alternar = "alternar_i 604.3129.3307 604.3129.3308" if len(dirs) == 2 else "alternar_i 604.3129.3307 604.3129.3307"
        resumen["direcciones"] = etiquetas
    else:
        nom = str(peticion.get("eje") or "").strip()
        eje = next((x for x in caras.get("ejes_referencia", [])
                    if str(x.get("nombre", "")).upper() == nom.upper() or str(x.get("id")) == nom), None)
        if eje is None:
            disp = ", ".join(str(x.get("nombre")) for x in caras.get("ejes_referencia", [])) or "ninguno"
            raise PlanError(f"eje: nombre de un eje de referencia de la pieza (hay: {disp}).")
        d = _unit(eje["direccion"])
        if abs(abs(_dot(d, n)) - 1) > 1e-6:
            raise PlanError(f"El eje {nom} no es perpendicular a la cara del taladro.")
        cant = int(peticion.get("cantidad") or 0)
        if cant < 2:
            raise PlanError("cantidad ≥ 2 (incluido el taladro guía).")
        ang = float(peticion.get("angulo") or 360.0 / cant)
        if not 0 < abs(ang) * (cant - 1) < 360 + 1e-6:
            raise PlanError("angulo × (cantidad − 1) debe ser menor que 360°.")
        for i in range(cant):
            puntos.append(_girar(p0, eje["punto"], d, math.radians(ang * i)))
        e += [f"e 604.3130.3273 r 21 {eje['id']}", f"e 604.3130.3276 d {ang:.4f}", "e 604.3130.3309 i 0",
              f"e 604.3130.3134 i {cant}", "e 604.3130.3136 i 1"]
        alternar = "alternar_i 604.3130.3309 604.3130.3309"
        radio = math.sqrt(max(_dot(_sub(p0, eje["punto"]), _sub(p0, eje["punto"])) - _dot(_sub(p0, eje["punto"]), d) ** 2, 0))
        resumen.update(eje=eje.get("nombre"), cantidad=cant, angulo=ang, radio=round(radio, 3))
        if radio < r:
            raise PlanError(f"El taladro guía está a {radio:.2f} mm del eje: los miembros se pisarían (radio exterior {r:g}).")
    e += ["e 604.3131 i 4", f"e 604.1964 w {nombre}"]

    # comprobaciones: dentro de la cara, solapes entre miembros y con otros taladros
    avisos, fuera = [], []
    for q in puntos[1:]:
        if not _dentro(cara, q):
            fuera.append([round(v, 3) for v in q])
        else:
            b = _distancia_borde(cara, q, n)
            if b is not None and b < r:
                avisos.append(f"El miembro en {[round(v, 2) for v in q]} queda a {b:.2f} mm del borde (radio {r:g}).")
    if fuera:
        raise PlanError(f"{len(fuera)} miembro(s) caen fuera de la cara {cara['id']} (fuera de la pieza): {fuera}. "
                        "Cambia la dirección, la separación o la cantidad.")
    for i in range(len(puntos)):
        for j in range(i + 1, len(puntos)):
            if math.dist(puntos[i], puntos[j]) < 2 * r - 1e-6:
                raise PlanError(f"Dos miembros se pisan (separación {math.dist(puntos[i], puntos[j]):.2f} mm < Ø{2 * r:g}).")
    for o in otros:
        if o.get("id") == guia["id"] or not o.get("eje"):
            continue
        dd = _unit(o["eje"]["direccion"])
        if abs(abs(_dot(dd, n)) - 1) > 1e-6:
            continue
        for q in puntos[1:]:
            v = _sub(q, o["eje"]["punto"])
            dist = math.sqrt(max(_dot(v, v) - _dot(v, dd) ** 2, 0))
            if dist < r + _radio_exterior(o) - 1e-6:
                avisos.append(f"El miembro en {[round(x, 2) for x in q]} se pisa con el taladro {o.get('id')}"
                              f"{' ' + o['nombre'] if o.get('nombre') else ''} (a {dist:.2f} mm).")
    resumen.update(miembros=len(puntos), posiciones=[[round(v, 3) for v in q] for q in puntos], nombre=nombre)
    lineas = [f"modelo {modelo}", f"patron {guia['id']}", *e,
              f"verificar {p0[0]:.6f} {p0[1]:.6f} {p0[2]:.6f} {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}",
              *[f"esperado {q[0]:.6f} {q[1]:.6f} {q[2]:.6f}" for q in puntos], alternar]
    return {"plan": "\n".join(lineas) + "\n", "resumen": resumen, "avisos": avisos, "ids_caras": [cara["id"]]}


# --------------------------------------------------------------- redondeos

def _corregir_forma(datos: dict[str, Any]) -> dict[str, Any]:
    """creo_iac_tk 0.5.0 daba cóncava/convexa al revés (corregido en 0.5.1)."""
    if datos.get("version_tk") != "0.5.0":
        return datos
    cambio = {"concava": "convexa", "convexa": "concava"}
    return {**datos, "aristas": [{**a, "forma": cambio.get(a.get("forma"), a.get("forma"))}
                                 for a in datos.get("aristas", [])]}


def resumir_aristas(datos: dict[str, Any], forma: str | None = None, cara: int | None = None,
                    longitud_min: float = 0.0, maximo: int = 300, direccion: str | None = None) -> dict[str, Any]:
    """Aristas de la pieza filtradas por forma (concava, convexa, tangente), cara, longitud y
    dirección (X, Y o Z: rectas paralelas a ese eje, p. ej. las esquinas interiores paralelas al
    eje de la fresa)."""
    out = []
    datos = _corregir_forma(datos)
    for a in datos.get("aristas", []):
        if forma and a.get("forma") != forma:
            continue
        if cara is not None and cara not in (a.get("caras") or []):
            continue
        if (a.get("longitud") or 0) < longitud_min:
            continue
        x = {"id": a["id"], "tipo": a.get("tipo"), "forma": a.get("forma"), "longitud": round(a.get("longitud", 0), 3),
             "caras": a.get("caras"), "medio": _r(a.get("medio"), 3)}
        t = a.get("tangente")
        if t and a.get("tipo") == "recta":
            o = orientacion(t) or orientacion([-c for c in t])
            if o:
                x["direccion"] = o.lstrip("+-")
        if direccion and x.get("direccion") != direccion.strip().upper().lstrip("+-"):
            continue
        out.append(x)
    out.sort(key=lambda x: (x["forma"] != "concava", -x["longitud"]))
    return {"modelo": f"{datos['modelo']['nombre']}.PRT", "unidad_longitud": datos.get("unidad_longitud"),
            "aristas": out[:maximo], "total": len(out), "truncado": len(out) > maximo,
            "total_pieza": datos.get("total")}


def planificar_redondeo(peticion: dict[str, Any], aristas: dict[str, Any], nombre: str) -> dict[str, Any]:
    """Redondeo de radio constante sobre una o varias aristas (una operación). No toca Creo."""
    ids = [int(i) for i in (peticion.get("aristas") or [])]
    if not ids:
        raise PlanError("aristas: lista de ids (aristas_creo o seleccion_creo).")
    if len(set(ids)) != len(ids) or len(ids) > 64:
        raise PlanError("aristas: ids sin repetir, como mucho 64.")
    radio = float(peticion.get("radio") or 0)
    if radio <= 0:
        raise PlanError("radio: obligatorio y positivo (mm).")
    aristas = _corregir_forma(aristas)
    por_id = {a["id"]: a for a in aristas.get("aristas", [])}
    sel, avisos = [], []
    for i in ids:
        a = por_id.get(i)
        if a is None:
            raise PlanError(f"La arista {i} no existe en la pieza (vuelve a leer con aristas_creo).")
        if a.get("forma") == "tangente":
            raise PlanError(f"La arista {i} une dos caras tangentes: no hay esquina que redondear.")
        sel.append(a)
        if a.get("longitud") and radio > a["longitud"]:
            avisos.append(f"El radio ({radio:g}) es mayor que la arista {i} ({a['longitud']:.2f} mm): Creo puede no poder hacerlo.")
    formas = sorted({a.get("forma") for a in sel})
    modelo = aristas["modelo"]["nombre"] + ".PRT"
    lista = " ".join(map(str, ids))
    e = ["e 386.387 i 913", f"e 386.1964 w {nombre}",
         "e 386.2101.2102.2178 i 0",                 # radio constante
         "e 386.2101.2102.2177.2119 i 2",            # sin cónica
         "e 386.2101.2102.2121.2179 i 1",            # referencias: aristas
         f"e 386.2101.2102.2121.2124 k {lista}",
         "e 386.2101.2102.2123.2122.2106.2108 i 1",  # radio por valor
         f"e 386.2101.2102.2123.2122.2106.2109 d {radio:.4f}",
         "e 386.3042 i 0"]                           # unido a la pieza
    resumen = {"modelo": modelo, "radio": radio, "aristas": [
        {"id": a["id"], "forma": a.get("forma"), "longitud": round(a.get("longitud", 0), 3),
         "medio": _r(a.get("medio"), 3)} for a in sel], "formas": formas, "nombre": nombre}
    # La comprobación de que las aristas han desaparecido la hace el servidor releyendo las aristas
    # (ProEdgeInit no sirve: en Creo 12.4 devuelve la arista aunque ya no esté, visto el 2026-11-24).
    lineas = [f"modelo {modelo}", *e]
    return {"plan": "\n".join(lineas) + "\n", "resumen": resumen, "avisos": avisos}


ESQUEMAS_CHAFLAN = {"45xD": 601, "DxD": 602}


def planificar_chaflan(peticion: dict[str, Any], aristas: dict[str, Any], nombre: str) -> dict[str, Any]:
    """Chaflán de distancia constante (45×D o D×D) sobre una o varias aristas. No toca Creo."""
    esquema = str(peticion.get("esquema") or "45xD")
    if esquema not in ESQUEMAS_CHAFLAN:
        raise PlanError("esquema: 45xD (45° y distancia D) o DxD (misma distancia a las dos caras).")
    d = float(peticion.get("distancia") or 0)
    if d <= 0:
        raise PlanError("distancia: obligatoria y positiva (mm).")
    # se reutilizan las comprobaciones del redondeo (aristas, tangentes, longitud)
    base = planificar_redondeo({"aristas": peticion.get("aristas"), "radio": d}, aristas, nombre)
    avisos = [a.replace("El radio", "La distancia") for a in base["avisos"]]
    concavas = [a["id"] for a in base["resumen"]["aristas"] if a.get("forma") == "concava"]
    if concavas:
        avisos.append(f"Las aristas {concavas} son esquinas interiores: un chaflán ahí AÑADE material. Lo normal es "
                      "achaflanar aristas exteriores (convexas).")
    lista = " ".join(str(a["id"]) for a in base["resumen"]["aristas"])
    e = ["e 386.387 i 914", f"e 386.1964 w {nombre}",
         f"e 386.2101.2102.2103 i {ESQUEMAS_CHAFLAN[esquema]}",
         "e 386.2101.2102.3184 i 1",                 # forma: superficies desplazadas
         "e 386.2101.2102.2121.2179 i 1",            # referencias: aristas
         f"e 386.2101.2102.2121.2124 k {lista}",
         "e 386.2101.2102.2123.2122.2106.2108 i 1",  # distancia por valor
         f"e 386.2101.2102.2123.2122.2106.2109 d {d:.4f}",
         "e 386.3042 i 0"]
    resumen = {k: v for k, v in base["resumen"].items() if k != "radio"}
    resumen.update(esquema=esquema, distancia=d, nombre=nombre, tipo="chaflan")
    lineas = [f"modelo {base['resumen']['modelo']}", *e]
    return {"plan": "\n".join(lineas) + "\n", "resumen": resumen, "avisos": avisos}


def huella_aristas(aristas: dict[str, Any], ids: list[int]) -> str:
    import hashlib
    import json

    por_id = {a["id"]: a for a in aristas.get("aristas", [])}
    partes = [aristas["modelo"]["nombre"]] + [
        [i, _r(por_id[i].get("medio"), 4), round(por_id[i].get("longitud", 0), 4)] if i in por_id else [i, None]
        for i in sorted(ids)]
    return hashlib.sha256(json.dumps(partes).encode()).hexdigest()[:16]


# --------------------------------------------------------------- resúmenes

TIPOS_ELEMENTO = {1: "conjunto", 2: "pieza", 3: "operacion", 5: "cara", 6: "arista", 21: "eje",
                  25: "sistema de coordenadas", 57: "superficie (quilt)", 62: "curva", 66: "punto",
                  98: "cuerpo"}


def _r(v, n=4):
    return [round(float(c), n) + 0.0 for c in v] if isinstance(v, (list, tuple)) else v


def orientacion(normal) -> str | None:
    """'+Y' si la normal coincide con un eje de la pieza; None si es oblicua."""
    for i, eje in enumerate("XYZ"):
        if abs(abs(normal[i]) - 1) < 1e-6:
            return ("+" if normal[i] > 0 else "-") + eje
    return None


def resumir_caras(datos: dict[str, Any], maximo: int = 200) -> dict[str, Any]:
    caras = []
    for c in datos.get("caras_planas", []):
        n = c.get("normal_exterior")
        x = {"id": c["id"], "orientacion": orientacion(n) if n else None, "area": round(c.get("area", 0), 2),
             "normal_exterior": _r(n), "punto": _r(c.get("punto_plano"), 3),
             "caja_min": _r(c.get("caja_min"), 3), "caja_max": _r(c.get("caja_max"), 3)}
        caras.append({k: v for k, v in x.items() if v is not None})
    caras.sort(key=lambda c: -c["area"])
    planos = [{"nombre": p.get("nombre"), "id": p["id"], "orientacion": orientacion(p["normal"]) if "normal" in p else None,
               "normal": _r(p.get("normal")), "origen": _r(p.get("origen"), 3)} for p in datos.get("planos_referencia", [])]
    return {"modelo": f"{datos['modelo']['nombre']}.PRT", "unidad_longitud": datos.get("unidad_longitud"),
            "caras_planas": caras[:maximo], "caras_planas_total": len(caras), "truncado": len(caras) > maximo,
            "planos_referencia": planos, "cuerpo_por_defecto": datos.get("cuerpo_por_defecto")}


def resumir_seleccion(datos: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for s in datos.get("seleccion", []):
        t = s.get("tipo_elemento")
        x: dict[str, Any] = {"tipo": TIPOS_ELEMENTO.get(t, f"tipo {t}"), "id": s.get("id"),
                             "modelo": f"{s.get('modelo', '')}.{s.get('tipo_modelo', 'PRT')}"}
        if "ruta" in s:
            x["ruta_en_conjunto"] = s["ruta"]
        if "punto" in s:
            x["punto_pinchado"] = _r(s["punto"], 3)
        if "coordenadas" in s:
            x["coordenadas"] = _r(s["coordenadas"], 3)
        c = s.get("cara")
        if c:
            n = c.get("normal_exterior")
            x["cara"] = {"orientacion": orientacion(n) if n else None, "normal_exterior": _r(n),
                         "area": round(c.get("area", 0), 2), "caja_min": _r(c.get("caja_min"), 3),
                         "caja_max": _r(c.get("caja_max"), 3)}
        out.append(x)
    return out


def huella(caras: dict[str, Any], ids_caras: list[int], ids_planos: list[int]) -> str:
    """Resumen de la geometría que usa una propuesta: planos de las caras y de las referencias
    (normal y distancia al origen) y el cuerpo. Si cambia, la propuesta ya no vale. No incluye
    áreas ni cajas, que cambian al hacer los propios taladros."""
    import hashlib
    import json

    def plano(n, p):
        n = _unit(n)
        return [round(c, 6) + 0.0 for c in n] + [round(_dot(n, p), 4) + 0.0]

    partes: list[Any] = [caras["modelo"]["nombre"], caras.get("cuerpo_por_defecto"), caras.get("unidad_longitud")]
    por_id = {c["id"]: c for c in caras.get("caras_planas", [])}
    for i in sorted(set(ids_caras)):
        c = por_id.get(i)
        partes.append([i, plano(c["normal_exterior"], c["punto_plano"])] if c and "normal_exterior" in c else [i, None])
    refs = {p["id"]: p for p in caras.get("planos_referencia", [])}
    refs.update({c["id"]: {"normal": c["normal_exterior"], "origen": c["punto_plano"]}
                 for c in caras.get("caras_planas", []) if "normal_exterior" in c and c["id"] not in refs})
    for i in sorted(set(ids_planos)):
        p = refs.get(i)
        partes.append([i, plano(p["normal"], p["origen"])] if p and "normal" in p else [i, None])
    return hashlib.sha256(json.dumps(partes).encode()).hexdigest()[:16]


# Líneas que admite el servicio en un plan (defensa adicional: nada de rutas ni órdenes).
PLAN_RE = re.compile(r"^(modelo [A-Za-z0-9_\-]{1,80}\.PRT|serie [A-Za-z0-9_]{1,30}|tamano [A-Za-z0-9_.]{1,30}|"
                     r"e [0-9.]{3,150} (i -?\d+|d -?\d+(\.\d+)?|b [01]|r \d+ -?\d+|w [A-Z0-9_]{1,31}|c|k( \d+){1,64})|"
                     r"verificar( -?\d+(\.\d+)?){6}|alternar(_i)? [0-9.]+ [0-9.]+|patron \d+|"
                     r"esperado( -?\d+(\.\d+)?){3}|sin_aristas( \d+){1,64}|"
                     r"sk_ref \d+ \d+|sk_linea( -?\d+(\.\d+)?){6}|sk_arco( -?\d+(\.\d+)?){12}|sk_circulo( -?\d+(\.\d+)?){4}|"
                     r"sk_cota ((diam|radio) \d+|ref \d+ [cse] \d+|refl \d+ \d+|entre \d+ \d+)( -?\d+(\.\d+)?){4}|"
                     r"sentido (-1|0|1)|probar_sentidos|lado_material [0-3]|rango [XYZ]( -?\d+(\.\d+)?){2}|"
                     r"volumen(_max)? -?\d+(\.\d+)?|volumen_signo -?1)$")


def plan_valido(texto: str) -> bool:
    lineas = [x for x in texto.splitlines() if x.strip()]
    return 0 < len(lineas) <= 600 and all(PLAN_RE.match(x) for x in lineas)
