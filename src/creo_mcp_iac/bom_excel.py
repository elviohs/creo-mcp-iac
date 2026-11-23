"""Lista de materiales a Excel y vuelta de parámetros (parecido a Nitro-BOM).

- `exportar`: libro con el BOM indentado, la lista agregada de piezas con los
  parámetros del cajetín (editable), la tornillería agrupada y los avisos.
- `leer_cambios`: compara el libro editado con los valores exportados y con los
  actuales de Creo y devuelve los cambios, los conflictos y lo que se ignora.

No accede a Creo: recibe los datos ya leídos y devuelve estructuras simples.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import xlsx

# Columnas de parámetros del cajetín IAC, en el orden del libro.
# Los nombres de personas no se exportan (regla del ROADMAP).
COLUMNAS = [
    "CODIGO_PLANO", "PROYECTO", "SUBSISTEMA", "PIEZA", "PTC_MATERIAL_NAME", "TRATAMIENTO",
    "PROTECCION_SUPERFICIAL", "RUGOSIDAD", "TOLERANCIA", "CANTIDAD_FABRICADA", "VALIDO_PARA",
    "FUENTE",
]
# Los que se pueden devolver a Creo. El material se cambia en Creo, no por parámetro.
EDITABLES: frozenset[str] = frozenset(COLUMNAS) - {"PTC_MATERIAL_NAME"}
# Otros parámetros que se leen (descripción para reconocer tornillería, Windchill).
EXTRA_LECTURA = ("DESCRIPCION", "DESCRIPTION", "PTC_COMMON_NAME", "PTC_WM_VERSION",
                 "PTC_WM_LIFECYCLE_STATE")
# Los que debería tener toda pieza propia del IAC.
OBLIGATORIOS = ("CODIGO_PLANO", "PROYECTO", "PIEZA")

HOJA_BOM = "BOM indentado"
HOJA_LISTA = "Lista de piezas"
HOJA_TORN = "Tornilleria"
HOJA_AVISOS = "Avisos"
HOJA_CONTROL = "_control"
FORMATO = "creo-mcp-iac/bom-1"

# --- tornillería ------------------------------------------------------------------

_NORMAS = {
    # norma: (elemento, tipo)
    "912": ("Tornillo", "Allen cabeza cilíndrica"), "4762": ("Tornillo", "Allen cabeza cilíndrica"),
    "7984": ("Tornillo", "Allen cabeza baja"),
    "7991": ("Tornillo", "Avellanado allen"), "10642": ("Tornillo", "Avellanado allen"),
    "7380": ("Tornillo", "Cabeza de botón"),
    "933": ("Tornillo", "Hexagonal rosca total"), "4017": ("Tornillo", "Hexagonal rosca total"),
    "931": ("Tornillo", "Hexagonal"), "4014": ("Tornillo", "Hexagonal"),
    "84": ("Tornillo", "Cabeza cilíndrica ranurada"), "1207": ("Tornillo", "Cabeza cilíndrica ranurada"),
    "7985": ("Tornillo", "Cabeza alomada Phillips"), "7045": ("Tornillo", "Cabeza alomada Phillips"),
    "913": ("Prisionero", "Punta plana"), "4026": ("Prisionero", "Punta plana"),
    "914": ("Prisionero", "Punta cónica"), "4027": ("Prisionero", "Punta cónica"),
    "916": ("Prisionero", "Punta de copa"), "4029": ("Prisionero", "Punta de copa"),
    "934": ("Tuerca", "Hexagonal"), "4032": ("Tuerca", "Hexagonal"),
    "985": ("Tuerca", "Autoblocante"), "10511": ("Tuerca", "Autoblocante"),
    "982": ("Tuerca", "Autoblocante"), "7040": ("Tuerca", "Autoblocante"),
    "4034": ("Tuerca", "Hexagonal"), "4035": ("Tuerca", "Hexagonal baja"), "439": ("Tuerca", "Hexagonal baja"),
    "7092": ("Arandela", "Plana estrecha"), "433": ("Arandela", "Plana estrecha"),
    "125": ("Arandela", "Plana"), "7089": ("Arandela", "Plana"), "7090": ("Arandela", "Plana biselada"),
    "9021": ("Arandela", "Plana ancha"), "7093": ("Arandela", "Plana ancha"),
    "127": ("Arandela", "Grower"), "6798": ("Arandela", "Dentada"),
    "7": ("Pasador", "Cilíndrico"), "2338": ("Pasador", "Cilíndrico"), "8734": ("Pasador", "Cilíndrico templado"),
    "6325": ("Pasador", "Cilíndrico templado"), "1481": ("Pasador", "Elástico"), "8752": ("Pasador", "Elástico"),
}
_PALABRAS = (
    (r"PRISIONERO|GRUB|SET[_ ]?SCREW", "Prisionero"),
    (r"TORNILLO|SCREW|(?<![A-Z])BOLT(?![A-Z])|PERNO|BUL[OÓ]N", "Tornillo"),
    (r"TUERCA|(?<![A-Z])NUTS?(?![A-Z])", "Tuerca"),
    (r"ARANDELA|WASHER|GROWER", "Arandela"),
    (r"PASADOR|(?<![A-Z])PINS?(?![A-Z])|DOWEL", "Pasador"),
    (r"HELICOIL|INSERTO|(?<![A-Z])INSERTS?(?![A-Z])", "Inserto roscado"),
    (r"ESP[AÁ]RRAGO|(?<![A-Z])STUDS?(?![A-Z])", "Espárrago"),
)
# "DIN EN ISO 7092", "DINENISO7092", "EN_ISO_4762" -> "ISO 7092"
_PREFIJO_RE = re.compile(r"(?:DIN|EN)[ _\-]?(?:EN[ _\-]?)?ISO", re.IGNORECASE)
_NORMA_RE = re.compile(r"(?<![A-Z0-9])(ISO|DIN|EN|UNE)[ _\-]?(\d{1,5})(?!\d)", re.IGNORECASE)
# Métrica con decimal: M2.5, M2,5 o M2_5 (nombres de la biblioteca de Creo: DIN912-M2_5X25)
_MET = r"M(\d{1,2}(?:[.,]\d|_5|_6)?)"
_METRICA_L_RE = re.compile(r"(?<![A-Z0-9])" + _MET + r"\s*[Xx×*]\s*(\d{1,3}(?:[.,]\d+)?)(?![\d])")
_METRICA_RE = re.compile(r"(?<![A-Z0-9])" + _MET + r"(?![\d.,])")
# Tras la norma: arandelas "DIN125-1-A6", "DIN127-B5", "ISO7092-2"; pasadores "ISO2338-4M6X10"
_ARANDELA_RE = re.compile(r"^[AB]?(\d{1,2}(?:_5|_6)?)$")
_PASADOR_RE = re.compile(r"^(\d{1,2}(?:_5)?)[MH]\d{1,2}X(\d{1,3})$")


def _num(s: str) -> float | int:
    v = float(s.replace(",", "."))
    return int(v) if v.is_integer() else v


def clasificar_tornilleria(nombre: str, textos: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """Reconoce un elemento normalizado por el nombre del fichero y la descripción.

    Exige una norma conocida o una palabra clave: una métrica sola (p. ej.
    `SOPORTE_M5.PRT`) no basta. Devuelve None si no parece tornillería.
    """
    base = nombre.rsplit(".", 1)[0]
    texto = " ".join([base, *[str(t) for t in textos if t]]).upper()
    texto = _PREFIJO_RE.sub("ISO", texto)
    elemento = tipo = norma = None
    resto = ""
    for m in _NORMA_RE.finditer(texto):
        num = m.group(2).lstrip("0") or "0"
        if num in _NORMAS:
            elemento, tipo = _NORMAS[num]
            norma = f"{m.group(1).upper()} {num}"
            resto = texto[m.end():]
            break
    if elemento is None:
        for patron, el in _PALABRAS:
            if re.search(patron, texto):
                elemento = el
                break
    if elemento is None:
        return None
    metrica = ""
    longitud = None
    # Primero el nombre del fichero, que sigue la norma; la descripción solo si no basta
    nombre_txt = _PREFIJO_RE.sub("ISO", base.upper())
    if elemento == "Pasador" and resto:
        tokens = [t for t in re.split(r"[- ]+|_(?!\d)", resto.strip("-_ ")) if t]
        p = _PASADOR_RE.match(tokens[0]) if tokens else None
        if p:
            metrica, longitud = "Ø" + _metrica(p.group(1))[1:], _num(p.group(2))
    for txt in (nombre_txt, texto):
        if metrica:
            break
        m = _METRICA_L_RE.search(txt)
        if m:
            metrica, longitud = _metrica(m.group(1)), _num(m.group(2))
            break
        m = _METRICA_RE.search(txt)
        if m:
            metrica = _metrica(m.group(1))
            break
    if not metrica and resto and elemento == "Arandela":
        tokens = [t for t in re.split(r"[- ]+|_(?!\d)", resto.strip("-_ ")) if t]
        if norma == "DIN 125" and tokens[:1] == ["1"]:
            tokens = tokens[1:]  # DIN 125-1
        a = _ARANDELA_RE.match(tokens[0]) if tokens else None
        if a:
            metrica = _metrica(a.group(1))
    if elemento in ("Tuerca", "Arandela"):
        longitud = None
    return {"elemento": elemento, "tipo": tipo or "", "norma": norma or "", "metrica": metrica,
            "longitud": longitud}


def _metrica(s: str) -> str:
    return "M" + s.replace(",", ".").replace("_", ".")


# --- datos -----------------------------------------------------------------------

def _vacio(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def normalizar(v: Any) -> Any:
    """Valor comparable: los textos sin espacios a los lados; vacío = ''."""
    if _vacio(v):
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _posiciones(indentada: list[dict[str, Any]]) -> list[str]:
    """Numeración 1, 1.1, 1.2, 2... a partir de los niveles."""
    cont: list[int] = []
    out = []
    for fila in indentada:
        n = fila["level"]
        cont = cont[:n]
        while len(cont) < n:
            cont.append(0)
        cont[n - 1] += 1
        out.append(".".join(map(str, cont)))
    return out


def preparar(bom: dict[str, Any], parametros: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Reúne BOM, parámetros (modelo -> nombre -> {type, value}) y clasificación.

    `parametros` usa las claves de modelo en mayúsculas.
    """
    # Comerciales: CODIGO_PLANO "NA" (convención del departamento). Su contenido no se
    # lista ni se avisa: se compran enteros. Solo se omite un modelo si TODAS sus
    # apariciones están dentro de un conjunto comercial.
    def _codigo(clave: str) -> str:
        return str(normalizar(parametros.get(clave, {}).get("CODIGO_PLANO", {}).get("value"))).upper()

    def _es_comercial(clave: str) -> bool:
        return _codigo(clave) in ("NA", "N/A", "N.A.", "COMERCIAL")

    dentro: dict[str, bool] = {}
    pila: list[bool] = []
    for f in bom["indented"]:
        pila = pila[: f["level"] - 1]
        bajo = any(pila)
        k = f["file"].upper()
        dentro[k] = dentro.get(k, True) and bajo
        pila.append(bajo or (f["type"] == "ASM" and _es_comercial(k)))
    omitidos = {k for k, v in dentro.items() if v}

    piezas: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    tornilleria: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
    avisos: list[dict[str, str]] = []
    asm = (bom.get("assembly") or "").upper()
    proyecto_asm = normalizar(parametros.get(asm, {}).get("PROYECTO", {}).get("value"))
    if not proyecto_asm:  # el más repetido, si lo tiene al menos la mitad de las piezas con valor
        from collections import Counter

        cuenta = Counter(normalizar(p.get("PROYECTO", {}).get("value")) for p in parametros.values())
        cuenta.pop("", None)
        if cuenta:
            valor, n = cuenta.most_common(1)[0]
            if n * 2 >= sum(cuenta.values()):
                proyecto_asm = valor

    for fila in bom["flat"]:
        clave = fila["file"].upper()
        if clave in omitidos:
            continue
        p = parametros.get(clave, {})
        textos = tuple(str(p.get(k, {}).get("value") or "") for k in ("PIEZA", "DESCRIPCION", "DESCRIPTION",
                                                                     "PTC_COMMON_NAME"))
        t = clasificar_tornilleria(fila["file"], textos) if fila["type"] == "PRT" else None
        if t:
            k = (t["elemento"], t["tipo"], t["norma"], t["metrica"], t["longitud"])
            g = tornilleria.setdefault(k, {**t, "cantidad": 0, "modelos": []})
            g["cantidad"] += fila["qty_total"]
            g["modelos"].append(fila["file"])
            if not t["metrica"]:
                avisos.append({"modelo": fila["file"], "aviso": "Tornillería sin métrica reconocible en el nombre."})
            continue
        valores = {c: p[c] for c in COLUMNAS if c in p}
        comercial = _es_comercial(clave)
        piezas[clave] = {"modelo": fila["file"], "tipo": fila["type"] + (" comercial" if comercial else ""),
                         "cantidad": fila["qty_total"], "parametros": valores}
        if comercial:
            continue
        if not any(c in p for c in OBLIGATORIOS):
            avisos.append({"modelo": fila["file"],
                           "aviso": "Sin parámetros del cajetín IAC: ¿pieza comercial o de otra plantilla?"})
            continue
        faltan = [c for c in OBLIGATORIOS if _vacio(p.get(c, {}).get("value"))]
        if faltan:
            avisos.append({"modelo": fila["file"], "aviso": f"Vacío: {', '.join(faltan)}."})
        pr = normalizar(p.get("PROYECTO", {}).get("value"))
        if proyecto_asm and pr and pr != proyecto_asm:
            avisos.append({"modelo": fila["file"],
                           "aviso": f"PROYECTO «{pr}» distinto del conjunto («{proyecto_asm}»)."})

    codigos: dict[str, list[str]] = {}
    for pz in piezas.values():
        c = normalizar(pz["parametros"].get("CODIGO_PLANO", {}).get("value"))
        if c and str(c).upper() not in ("NA", "N/A", "N.A.", "COMERCIAL"):
            codigos.setdefault(str(c).upper(), []).append(pz["modelo"])
    for c, mods in codigos.items():
        if len(mods) > 1:
            avisos.append({"modelo": ", ".join(mods), "aviso": f"Mismo CODIGO_PLANO en varios modelos: {c}."})

    return {"conjunto": bom.get("assembly"), "indentada": bom["indented"], "piezas": piezas,
            "omitidos_comerciales": len(omitidos),
            "tornilleria": list(tornilleria.values()), "avisos": avisos,
            "tornilleria_modelos": {m.upper() for g in tornilleria.values() for m in g["modelos"]}}


# --- Excel -----------------------------------------------------------------------

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SIMBOLO_RE = re.compile("\x01([^\x02]*)\x02")


def celda(v: Any) -> Any:
    """Valor apto para Excel: símbolos de Creo legibles y sin caracteres de control.
    (Los textos se escriben como texto en línea: Excel nunca los toma por fórmulas.)"""
    v = normalizar(v)
    if isinstance(v, str):
        v = _SIMBOLO_RE.sub(lambda m: {"n": "Ø", "$": "°"}.get(m.group(1), ""), v)
        v = _CTRL_RE.sub("", v)
    return v


def _anchos(filas: list[list[Any]]) -> list[float]:
    n = max((len(f) for f in filas), default=0)
    out = []
    for c in range(n):
        largo = max((len(str(_v(f[c]))) for f in filas if c < len(f) and _v(f[c]) is not None), default=8)
        out.append(float(min(max(10, largo + 2), 45)))
    return out


def _v(item: Any) -> Any:
    return item[0] if isinstance(item, tuple) else item


def _hoja(nombre: str, cab: list[str], filas: list[list[Any]], editables: set[str] = frozenset(),
          filtro: bool = True) -> dict[str, Any]:
    cabecera = [(c, xlsx.CABECERA_EDITABLE if c in editables else xlsx.CABECERA) for c in cab]
    todas = [cabecera, *filas]
    return {"nombre": nombre, "filas": todas, "anchos": _anchos(todas), "fijar_cabecera": True,
            "autofiltro": len(todas) if filtro else None}


def exportar(datos: dict[str, Any], ruta: Path, origen: dict[str, Any]) -> dict[str, Any]:
    """Escribe el libro. `origen` va a la hoja de control (servidor, fecha, Creo)."""
    hojas = []

    # 1. BOM indentado (solo lectura)
    torn = datos["tornilleria_modelos"]
    filas = []
    for pos, f in zip(_posiciones(datos["indentada"]), datos["indentada"]):
        p = datos["piezas"].get(f["file"].upper(), {}).get("parametros", {})
        filas.append([pos, f["level"], celda(("   " * (f["level"] - 1)) + f["file"]), f["type"], f["qty"],
                      "sí" if f["file"].upper() in torn else ""]
                     + [celda(p.get(c, {}).get("value")) for c in COLUMNAS[:5]])
    hojas.append(_hoja(HOJA_BOM, ["Pos.", "Nivel", "Modelo", "Tipo", "Cant. en nivel", "Tornillería"]
                       + COLUMNAS[:5], filas))

    # 2. Lista de piezas (editable)
    filas, control = [], []
    for pz in datos["piezas"].values():
        p = pz["parametros"]
        fila: list[Any] = [celda(pz["modelo"]), pz["tipo"], pz["cantidad"]]
        for c in COLUMNAS:
            v = celda(p.get(c, {}).get("value"))
            estilo = xlsx.NO_EXISTE if c not in p else (xlsx.EDITABLE if c in EDITABLES else xlsx.NORMAL)
            fila.append((v, estilo))
        filas.append(fila)
        control.append([pz["modelo"].upper(), json.dumps(
            {c: {"type": p[c].get("type"), "value": normalizar(p[c].get("value"))} for c in COLUMNAS if c in p},
            ensure_ascii=False, default=str)])
    h = _hoja(HOJA_LISTA, ["Modelo", "Tipo", "Cantidad total"] + COLUMNAS, filas, set(EDITABLES))
    h["filas"] += [[], [NOTA_LISTA]]
    hojas.append(h)

    # 3. Tornillería
    orden = {"Tornillo": 0, "Prisionero": 1, "Espárrago": 2, "Tuerca": 3, "Arandela": 4, "Pasador": 5}

    def _m(x):
        try:
            return float(x["metrica"][1:])
        except ValueError:
            return 0.0

    filas = [[g["elemento"], g["tipo"], g["norma"], g["metrica"], g["longitud"], g["cantidad"],
              celda(", ".join(g["modelos"]))]
             for g in sorted(datos["tornilleria"], key=lambda g: (orden.get(g["elemento"], 9), g["tipo"], _m(g),
                                                                  g["longitud"] or 0))]
    hojas.append(_hoja(HOJA_TORN, ["Elemento", "Tipo", "Norma", "Métrica", "Longitud (mm)", "Cantidad",
                                   "Modelos"], filas))

    # 4. Avisos
    hojas.append(_hoja(HOJA_AVISOS, ["Modelo", "Aviso"],
                       [[celda(a["modelo"]), celda(a["aviso"])] for a in datos["avisos"]]))

    # 5. Control (oculta): valores exportados, una fila por modelo
    meta = {"formato": FORMATO, "conjunto": datos["conjunto"],
            "fecha": datetime.now().isoformat(timespec="seconds"), **origen}
    hojas.append({"nombre": HOJA_CONTROL, "oculta": True,
                  "filas": [[json.dumps(meta, ensure_ascii=False)], *control]})

    xlsx.escribir(ruta, hojas)
    return {"piezas": len(datos["piezas"]), "filas_bom": len(datos["indentada"]),
            "grupos_tornilleria": len(datos["tornilleria"]),
            "elementos_tornilleria": sum(g["cantidad"] for g in datos["tornilleria"]),
            "avisos": len(datos["avisos"])}


NOTA_LISTA = ("Columnas amarillas: se pueden editar y devolver a Creo (revisar_parametros_excel y "
              "aplicar_parametros_excel). Celdas grises: el parámetro no existe en el modelo y no se crea. "
              "No cambies la columna Modelo ni el nombre de las hojas.")


# --- vuelta de parámetros -------------------------------------------------------------

def leer_libro(ruta: Path) -> dict[str, Any]:
    hojas = xlsx.leer(ruta)
    if HOJA_LISTA not in hojas or HOJA_CONTROL not in hojas:
        raise ValueError(f"El libro no es un BOM de creo-mcp-iac (faltan las hojas «{HOJA_LISTA}» o de control).")
    ctl = hojas[HOJA_CONTROL]
    try:
        meta = json.loads(ctl[0][0])
        control = {str(f[0]).upper(): json.loads(f[1]) for f in ctl[1:] if f and f[0]}
    except (TypeError, ValueError, IndexError) as e:
        raise ValueError("La hoja de control del libro está dañada.") from e
    if meta.get("formato") != FORMATO:
        raise ValueError(f"Formato de libro no reconocido: {meta.get('formato')}")
    lista = hojas[HOJA_LISTA]
    cab = lista[0] if lista else []
    if not cab or cab[0] != "Modelo":
        raise ValueError("La hoja de piezas no empieza por la columna «Modelo».")
    filas: dict[str, dict[str, Any]] = {}
    duplicados = []
    for row in lista[1:]:
        if not row or _vacio(row[0]):
            continue
        modelo = str(row[0]).strip()
        if not re.search(r"\.(PRT|ASM)$", modelo, re.IGNORECASE):
            continue  # notas al pie
        valores = {cab[i]: (row[i] if i < len(row) else None)
                   for i in range(len(cab)) if cab[i] in EDITABLES}
        if modelo.upper() in filas:
            duplicados.append(modelo)
        filas[modelo.upper()] = {"modelo": modelo, "valores": valores}
    return {"meta": meta, "control": control, "filas": filas, "duplicados": duplicados}


def _convertir(valor: Any, tipo: str | None) -> Any:
    v = normalizar(valor)
    if v == "":
        return ""
    if tipo == "INTEGER":
        try:
            f = float(str(v).replace(",", "."))
        except ValueError as e:
            raise ValueError(f"no es un número entero: {v!r}") from e
        if not f.is_integer():
            raise ValueError(f"no es un número entero: {v!r}")
        return int(f)
    if tipo == "DOUBLE":
        try:
            return float(str(v).replace(",", "."))
        except ValueError as e:
            raise ValueError(f"no es un número: {v!r}") from e
    if tipo == "BOOL":
        s = str(v).strip().lower()
        if s in ("sí", "si", "true", "1", "yes"):
            return True
        if s in ("no", "false", "0"):
            return False
        raise ValueError(f"no es sí/no: {v!r}")
    v = str(v)
    if len(v) > 200 or any(ord(ch) < 32 for ch in v):
        raise ValueError("texto demasiado largo o con caracteres de control")
    return v


def comparar(libro: dict[str, Any], actuales: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Cambios entre el libro y Creo.

    `actuales`: modelo (mayúsculas) -> parámetro -> {type, value}, leído ahora de
    Creo. Un cambio es una celda distinta del valor exportado. Si Creo también
    cambió ese valor después de exportar, es un conflicto y no se aplica.
    """
    cambios, conflictos, ignorados, errores = [], [], [], []
    for clave, fila in libro["filas"].items():
        ctl = libro["control"].get(clave)
        if ctl is None:
            ignorados.append({"modelo": fila["modelo"], "motivo": "No estaba en el libro exportado."})
            continue
        if clave not in actuales:
            ignorados.append({"modelo": fila["modelo"], "motivo": "No está abierto en la sesión de Creo."})
            continue
        act = actuales[clave]
        for nombre, valor in fila["valores"].items():
            exp = ctl.get(nombre)
            if exp is None:
                if not _vacio(valor):
                    ignorados.append({"modelo": fila["modelo"], "parametro": nombre,
                                      "motivo": "El parámetro no existe en el modelo; no se crea."})
                continue
            try:
                nuevo = _convertir(valor, exp.get("type"))
            except ValueError as e:
                errores.append({"modelo": fila["modelo"], "parametro": nombre, "error": str(e)})
                continue
            # El libro muestra los símbolos de Creo ya traducidos (celda): se compara con eso
            exportado = celda(exp.get("value"))
            if nuevo == exportado:
                continue
            if nombre not in act:
                ignorados.append({"modelo": fila["modelo"], "parametro": nombre,
                                  "motivo": "El parámetro ya no existe en Creo."})
                continue
            ahora = celda(act[nombre].get("value"))
            if ahora == nuevo:
                continue  # ya está así en Creo
            if ahora != exportado:
                conflictos.append({"modelo": fila["modelo"], "parametro": nombre,
                                   "exportado": exp.get("value"), "en_creo": ahora, "en_excel": nuevo})
                continue
            cambios.append({"modelo": fila["modelo"], "parametro": nombre, "antes": ahora, "despues": nuevo,
                            "tipo": act[nombre].get("type")})
    for d in libro["duplicados"]:
        errores.append({"modelo": d, "error": "El modelo aparece en varias filas."})
    firma = json.dumps([(c["modelo"].upper(), c["parametro"], c["despues"]) for c in cambios],
                       ensure_ascii=False, sort_keys=True, default=str)
    codigo = hashlib.sha256(firma.encode()).hexdigest()[:8] if cambios else None
    return {"cambios": cambios, "conflictos": conflictos, "ignorados": ignorados, "errores": errores,
            "codigo": codigo}
