"""Holguras mínimas entre piezas de un conjunto y su estimación en frío (sin tocar Creo).

1. `candidatos`: con la orden `posiciones` (matriz, material y caja de cada pieza) calcula la
   caja de cada pieza en coordenadas del conjunto y elige los pares cuyas cajas están a menos
   del umbral (sin tornillería, si se pide).
2. La orden `holguras` de creo_iac_tk calcula para cada par la distancia mínima exacta
   (ProFitClearanceCompute) y los dos puntos más cercanos.
3. `revisar_holguras` clasifica cada par y, si se da una temperatura, estima la holgura en frío:
   cada pieza se contrae con la contracción integrada de su material (ΔL/L desde 293 K)
   alrededor de un punto fijo común, y la holgura cambia en la dirección que une los dos
   puntos más cercanos:

       h_frío = h + n · (ε_B·(p_B − r) − ε_A·(p_A − r))

   Con el mismo material en las dos piezas, h_frío = h·(1 + ε), sea cual sea el punto fijo.
   Con materiales distintos el resultado depende del punto fijo (por dónde está sujeto el
   conjunto) y es una estimación de primer orden: no tiene en cuenta que los puntos más
   cercanos pueden cambiar, ni gradientes de temperatura dentro de una pieza, ni la
   deformación por tensiones.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable

from .nativo import nombre_fichero

CONTACTO = 1e-4      # mm: por debajo, las piezas se tocan


# ------------------------------------------------------------------ materiales

def cargar_tabla(ruta: str | None = None) -> dict[str, Any]:
    ruta = ruta or os.environ.get("CREO_MCP_CONTRACCION") or str(Path(__file__).with_name("contraccion_iac.json"))
    datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
    datos["_fichero"] = ruta
    for m in datos["materiales"]:
        m["_re"] = [re.compile(a, re.IGNORECASE) for a in m.get("alias", [])]
    return datos


def buscar_material(tabla: dict[str, Any], nombre: str | None) -> dict[str, Any] | None:
    """Por nombre exacto de la tabla o por los alias (nombre del material de Creo)."""
    if not nombre:
        return None
    n = str(nombre).strip()
    for m in tabla["materiales"]:
        if m["nombre"].lower() == n.lower():
            return m
    for m in tabla["materiales"]:
        if any(r.search(n.upper()) for r in m["_re"]):
            return m
    return None


def contraccion(m: dict[str, Any], t: float) -> float:
    """ΔL/L desde 293 K a la temperatura t (K); negativo al enfriar."""
    escala = m.get("escala", 1.0)
    if m["tipo"] == "poly":
        lo, hi = m.get("rango", [0, 300])
        tt = min(max(t, lo), 293.0)
        # se resta el valor a 293 K para que el ajuste dé exactamente 0 en la referencia
        p = lambda x: sum(c * x ** i for i, c in enumerate(m["coef"]))  # noqa: E731
        return (p(tt) - p(293.0)) * escala
    pts = m["puntos"]
    tt = min(max(t, pts[0][0]), pts[-1][0])
    for (t1, v1), (t2, v2) in zip(pts, pts[1:]):
        if t1 <= tt <= t2:
            return (v1 + (v2 - v1) * (tt - t1) / (t2 - t1)) * escala
    return pts[-1][1] * escala


# ------------------------------------------------------------------ geometría

def _transformar(m: list[float], p) -> tuple[float, float, float]:
    return tuple(p[0] * m[k] + p[1] * m[4 + k] + p[2] * m[8 + k] + m[12 + k] for k in range(3))  # type: ignore


def caja_conjunto(c: dict[str, Any]) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    caja, m = c.get("caja"), c.get("matriz")
    if not caja or len(caja) != 6 or not m or len(m) != 16:
        return None
    esquinas = [_transformar(m, (x, y, z)) for x in (caja[0], caja[3]) for y in (caja[1], caja[4])
                for z in (caja[2], caja[5])]
    return (tuple(min(e[k] for e in esquinas) for k in range(3)), tuple(max(e[k] for e in esquinas) for k in range(3)))


def _hueco_cajas(a, b) -> float:
    d = [max(0.0, a[0][k] - b[1][k], b[0][k] - a[1][k]) for k in range(3)]
    return math.sqrt(sum(x * x for x in d))


def piezas(posiciones: dict[str, Any]) -> list[dict[str, Any]]:
    """Piezas del conjunto con etiqueta (MODELO.PRT #n), ruta, material y caja en el conjunto."""
    out, cuenta = [], {}
    for c in posiciones.get("componentes", []):
        if (c.get("tipo") or "PRT") != "PRT":
            continue
        nombre = nombre_fichero(str(c.get("modelo", "")), "PRT")
        cuenta[nombre] = cuenta.get(nombre, 0) + 1
        out.append({"pieza": f"{nombre} #{cuenta[nombre]}", "modelo": nombre, "ruta": c.get("ruta") or [],
                    "material": c.get("material"), "caja": caja_conjunto(c)})
    return out


def candidatos(lista: list[dict[str, Any]], umbral: float, excluir: Callable[[str], bool] | None = None,
               maximo: int = 3000) -> tuple[list[tuple[int, int, float]], int]:
    """Pares (i, j, hueco entre cajas) con cajas a menos de `umbral`; los más cercanos primero."""
    validos = [i for i, p in enumerate(lista) if p["caja"] and p["ruta"] and not (excluir and excluir(p["modelo"]))]
    pares = []
    for x, i in enumerate(validos):
        for j in validos[x + 1:]:
            h = _hueco_cajas(lista[i]["caja"], lista[j]["caja"])
            if h <= umbral:
                pares.append((i, j, h))
    pares.sort(key=lambda p: p[2])
    return pares[:maximo], len(pares)


def punto_de_seleccion(seleccion: dict[str, Any], posiciones: dict[str, Any]) -> tuple[tuple[float, float, float], str]:
    """Punto (coordenadas del conjunto) de lo seleccionado en Creo: centro de la caja de la cara
    seleccionada, o el punto pinchado, pasado al conjunto con la matriz de su componente."""
    for s in seleccion.get("seleccion", []):
        ruta = s.get("ruta")
        comp = next((c for c in posiciones.get("componentes", []) if ruta and c.get("ruta") == ruta), None)
        if comp is None or not comp.get("matriz"):
            continue
        cara = s.get("cara") or {}
        if cara.get("caja_min") and cara.get("caja_max"):
            local = [(cara["caja_min"][k] + cara["caja_max"][k]) / 2 for k in range(3)]
            que = "centro de la cara seleccionada"
        elif s.get("punto"):
            local, que = s["punto"], "punto pinchado"
        else:
            continue
        nombre = nombre_fichero(str(s.get("modelo", "")), s.get("tipo_modelo") or "PRT")
        return _transformar(comp["matriz"], local), f"{que} de {nombre} (id {s.get('id')})"
    raise ValueError("No hay en Creo una cara o un punto seleccionado dentro del conjunto: selecciona la "
                     "superficie por la que se sujeta y repite.")


# ------------------------------------------------------------------ revisión

def _dist_eje(p, eje) -> float:
    o, dv = eje[:3], eje[3:]
    n = math.sqrt(sum(x * x for x in dv)) or 1.0
    dv = [x / n for x in dv]
    w = [p[k] - o[k] for k in range(3)]
    t = sum(w[k] * dv[k] for k in range(3))
    return math.sqrt(max(0.0, sum((w[k] - t * dv[k]) ** 2 for k in range(3))))


def concentrico(r: dict[str, Any], d: float) -> tuple[str, float, float] | None:
    """Ajuste concéntrico (eje dentro de agujero): las dos caras más cercanas son cilindros de
    ejes paralelos y casi coincidentes cuya diferencia de radios explica la holgura. Devuelve
    ('a' o 'b' = la pieza del cilindro exterior, radio exterior, radio interior).

    Si Creo solo da el cilindro de una de las dos piezas (el punto de la otra cae en una arista,
    p. ej. el borde de un separador), se acepta si la distancia del otro punto al eje explica la
    holgura: ese es el radio de la otra pieza."""
    ca, cb = r.get("cilindro_a"), r.get("cilindro_b")
    if bool(ca and ca.get("eje")) != bool(cb and cb.get("eje")) and r.get("puntos"):
        lado, c, otro = ("a", ca, r["puntos"][1]) if ca and ca.get("eje") else ("b", cb, r["puntos"][0])
        r_otro = _dist_eje(otro, c["eje"])
        if r_otro <= 0 or abs(abs(r_otro - c["radio"]) - d) > 1e-3:
            return None
        if c["radio"] >= r_otro:
            return lado, c["radio"], r_otro
        return ("b" if lado == "a" else "a"), r_otro, c["radio"]
    if not (ca and cb and ca.get("eje") and cb.get("eje")):
        return None
    da, db = ca["eje"][3:], cb["eje"][3:]
    na, nb = math.sqrt(sum(x * x for x in da)) or 1.0, math.sqrt(sum(x * x for x in db)) or 1.0
    da, db = [x / na for x in da], [x / nb for x in db]
    if abs(sum(da[k] * db[k] for k in range(3))) < 0.9999:
        return None
    w = [cb["eje"][k] - ca["eje"][k] for k in range(3)]
    t = sum(w[k] * da[k] for k in range(3))
    desc = math.sqrt(max(0.0, sum((w[k] - t * da[k]) ** 2 for k in range(3))))
    if ca["radio"] >= cb["radio"]:
        exterior, r_ext, r_int = "a", ca["radio"], cb["radio"]
    else:
        exterior, r_ext, r_int = "b", cb["radio"], ca["radio"]
    radial = r_ext - r_int
    if radial <= 0 or desc > radial + 0.01 or radial < d - 1e-3:
        return None
    return exterior, r_ext, r_int

ORDEN = {"interferencia": 0, "se cierra en frío": 1, "insuficiente": 2, "contacto": 3, "correcta": 4}


def revisar_holguras(lista: list[dict[str, Any]], pares: list[tuple[int, int, float]], resultado: dict[str, Any],
                     umbral: float, holgura_minima: float, temperatura: float | None = None,
                     temperaturas: dict[str, float] | None = None, materiales: dict[str, str] | None = None,
                     punto_fijo: tuple[float, float, float] = (0.0, 0.0, 0.0),
                     tabla: dict[str, Any] | None = None) -> dict[str, Any]:
    temperaturas = {k.upper(): v for k, v in (temperaturas or {}).items()}
    materiales = {k.upper(): v for k, v in (materiales or {}).items()}
    avisos: list[str] = []
    sin_datos: set[str] = set()
    hipotesis: set[str] = set()

    def eps(p: dict[str, Any]) -> tuple[float | None, str | None, float | None]:
        nombre_mat = materiales.get(p["modelo"]) or p.get("material")
        t = temperaturas.get(p["modelo"], temperatura)
        if t is None or tabla is None:
            return None, nombre_mat, t
        m = buscar_material(tabla, nombre_mat)
        if m is None:
            sin_datos.add(f"{p['modelo']} ({nombre_mat or 'sin material'})")
            return None, nombre_mat, t
        if "HIPÓTESIS" in (m.get("nota") or ""):
            hipotesis.add(m["nombre"])
        return contraccion(m, t), m["nombre"], t

    filas = []
    fallidos = 0
    for r in resultado.get("pares", []):
        k = r.get("n")
        if not isinstance(k, int) or k >= len(pares):
            continue
        i, j, _ = pares[k]
        a, b = lista[i], lista[j]
        if "error" in r:
            fallidos += 1
            continue
        d = float(r["distancia"])
        if d > umbral and not r.get("interferencia"):
            continue
        pa, pb = r["puntos"]
        if r.get("interferencia"):
            estado = "interferencia"
        elif d <= CONTACTO:
            estado = "contacto"
        elif d < holgura_minima:
            estado = "insuficiente"
        else:
            estado = "correcta"
        fila: dict[str, Any] = {"estado": estado, "a": a["pieza"], "b": b["pieza"], "ruta_a": a["ruta"],
                                "ruta_b": b["ruta"], "distancia": round(d, 4),
                                "punto_a": [round(x, 3) for x in pa], "punto_b": [round(x, 3) for x in pb]}
        if temperatura is not None or temperaturas:
            ea, ma, ta = eps(a)
            eb, mb, tb = eps(b)
            fila.update(material_a=ma, material_b=mb, temperatura_a=ta, temperatura_b=tb,
                        contraccion_a=None if ea is None else round(ea, 6),
                        contraccion_b=None if eb is None else round(eb, 6))
            if estado in ("interferencia", "contacto"):
                fila["estado_frio"] = "no evaluada (" + estado + ")"
            elif ea is None or eb is None:
                fila["estado_frio"] = "sin datos de contracción"
            elif (conc := concentrico(r, d)) is not None:
                # eje dentro de agujero: cada cilindro se contrae sobre su propio eje y la
                # excentricidad se mantiene; no depende del punto fijo
                ext, r_ext, r_int = conc
                e_ext, e_int = (ea, eb) if ext == "a" else (eb, ea)
                hf = d + e_ext * r_ext - e_int * r_int
                fila.update(ajuste="concéntrico", radio_exterior=round(r_ext, 4), radio_interior=round(r_int, 4),
                            distancia_frio=round(hf, 4), cambio=round(hf - d, 4))
                fila["estado_frio"] = ("se cierra en frío" if hf <= CONTACTO else
                                       "insuficiente" if hf < holgura_minima else "correcta")
            else:
                v = [pb[q] - pa[q] for q in range(3)]
                nv = math.sqrt(sum(x * x for x in v)) or 1.0
                n = [x / nv for x in v]
                mov = [eb * (pb[q] - punto_fijo[q]) - ea * (pa[q] - punto_fijo[q]) for q in range(3)]
                hf = d + sum(n[q] * mov[q] for q in range(3))
                fila["distancia_frio"] = round(hf, 4)
                fila["cambio"] = round(hf - d, 4)
                if hf <= CONTACTO:
                    fila["estado_frio"] = "se cierra en frío"
                elif hf < holgura_minima:
                    fila["estado_frio"] = "insuficiente"
                else:
                    fila["estado_frio"] = "correcta"
        filas.append(fila)

    def clave(f: dict[str, Any]) -> tuple:
        ef = f.get("estado_frio", "")
        peor = min(ORDEN.get(f["estado"], 9), ORDEN.get(ef, 9))
        return (peor, f.get("distancia_frio", f["distancia"]))

    filas.sort(key=clave)
    if fallidos:
        avisos.append(f"Creo no pudo calcular la distancia de {fallidos} pares.")
    if resultado.get("truncado"):
        avisos.append("El cálculo se cortó por tiempo: no se han revisado todos los pares. Baja el umbral o "
                      "revisa un subconjunto.")
    if sin_datos:
        avisos.append("Sin datos de contracción (indica el material con `materiales`): " + ", ".join(sorted(sin_datos)))
    for h in sorted(hipotesis):
        avisos.append(f"{h}: contracción tomada de un polinomio de la calculadora del departamento que parece mal "
                      "asignado (hipótesis del asistente, ver contraccion_iac.json). Verificar con NIST.")
    cuenta = {}
    for f in filas:
        cuenta[f["estado"]] = cuenta.get(f["estado"], 0) + 1
    cuenta_frio = {}
    for f in filas:
        if "estado_frio" in f:
            cuenta_frio[f["estado_frio"]] = cuenta_frio.get(f["estado_frio"], 0) + 1
    return {"filas": filas, "por_estado": cuenta, "por_estado_frio": cuenta_frio, "avisos": avisos,
            "calculados": resultado.get("calculados"), "fallidos": fallidos,
            "segundos": resultado.get("segundos")}
