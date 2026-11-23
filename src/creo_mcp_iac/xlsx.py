"""Lectura y escritura mínima de .xlsx con la biblioteca estándar (sin openpyxl).

Se hizo así el 2026-11-21: en el PC del departamento la carpeta de openpyxl del
entorno virtual quedó inaccesible (ni el administrador podía abrirla), y una
dependencia menos facilita el despliegue.

Escribe: textos (en línea), números, cuatro estilos, anchos de columna, primera
fila fija, autofiltro y hojas ocultas. Lee: el valor de cada celda de un libro
guardado por Excel o LibreOffice (cadenas compartidas, en línea, números,
booleanos y resultados de fórmulas).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PR = "http://schemas.openxmlformats.org/package/2006/relationships"

# Estilos (índice en cellXfs)
NORMAL, CABECERA, CABECERA_EDITABLE, EDITABLE, NO_EXISTE, NUM3, NUM3_NEGRITA, NEGRITA = 0, 1, 2, 3, 4, 5, 6, 7

_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS}">
<numFmts count="1"><numFmt numFmtId="164" formatCode="0.000###"/></numFmts>
<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="6"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFBF8F00"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1"/>
<xf numFmtId="0" fontId="0" fillId="5" borderId="0" xfId="0" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="164" fontId="2" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_HOJA_PROHIBIDO = re.compile(r"[\[\]:*?/\\]")


def col_letra(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def col_indice(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


class Formula(str):
    """Fórmula de Excel sin '=' inicial, p. ej. Formula("F2*G2"). Excel la calcula al abrir."""


def _celda_xml(ref: str, valor: Any, estilo: int) -> str:
    s = f' s="{estilo}"' if estilo else ""
    if isinstance(valor, Formula):
        return f'<c r="{ref}"{s}><f>{escape(str(valor).lstrip("="))}</f></c>'
    if valor is None or valor == "":
        return f'<c r="{ref}"{s}/>' if estilo else ""
    if isinstance(valor, bool):
        return f'<c r="{ref}"{s} t="b"><v>{int(valor)}</v></c>'
    if isinstance(valor, (int, float)):
        return f'<c r="{ref}"{s}><v>{valor!r}</v></c>'
    texto = _CTRL.sub("", str(valor))[:32767]
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(texto)}</t></is></c>'


def _hoja_xml(hoja: dict[str, Any]) -> str:
    filas = hoja["filas"]
    ncols = max((len(f) for f in filas), default=1)
    partes = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="{NS}" xmlns:r="{NS_R}">']
    if hoja.get("fijar_cabecera"):
        partes.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
                      'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
    anchos = hoja.get("anchos") or []
    if anchos:
        partes.append("<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w:.1f}" customWidth="1"/>' for i, w in enumerate(anchos)
        ) + "</cols>")
    partes.append("<sheetData>")
    for r, fila in enumerate(filas, start=1):
        celdas = []
        for c, item in enumerate(fila):
            valor, estilo = item if isinstance(item, tuple) else (item, NORMAL)
            celdas.append(_celda_xml(f"{col_letra(c)}{r}", valor, estilo))
        partes.append(f'<row r="{r}">{"".join(celdas)}</row>')
    partes.append("</sheetData>")
    if hoja.get("autofiltro") and len(filas) > 1:
        partes.append(f'<autoFilter ref="A1:{col_letra(ncols - 1)}{hoja["autofiltro"]}"/>')
    partes.append("</worksheet>")
    return "".join(partes)


def escribir(ruta: Path, hojas: list[dict[str, Any]]) -> None:
    """hojas: [{nombre, filas: [[valor | (valor, estilo)]], anchos?, fijar_cabecera?,
    autofiltro? (última fila), oculta?}]"""
    for h in hojas:
        if _HOJA_PROHIBIDO.search(h["nombre"]) or len(h["nombre"]) > 31:
            raise ValueError(f"Nombre de hoja no válido: {h['nombre']}")
    n = len(hojas)
    tipos = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, n + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{tipos}</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{NS_PR}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    activa = next((i for i, h in enumerate(hojas) if not h.get("oculta")), 0)
    sheets = "".join(
        f'<sheet name="{escape(h["nombre"])}" sheetId="{i}" r:id="rId{i}"'
        + (' state="hidden"' if h.get("oculta") else "") + "/>"
        for i, h in enumerate(hojas, start=1)
    )
    filtros = "".join(
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
        f"'{escape(h['nombre'])}'!$A$1:${col_letra(max(len(f) for f in h['filas']) - 1)}${h['autofiltro']}"
        "</definedName>"
        for i, h in enumerate(hojas) if h.get("autofiltro") and len(h["filas"]) > 1
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<workbook xmlns="{NS}" xmlns:r="{NS_R}">'
        f'<bookViews><workbookView activeTab="{activa}"/></bookViews>'
        f"<sheets>{sheets}</sheets>"
        + (f"<definedNames>{filtros}</definedNames>" if filtros else "")
        + '<calcPr calcId="191029" fullCalcOnLoad="1"/>'
        + "</workbook>"
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{NS_PR}">'
        + "".join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, n + 1)
        )
        + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/styles" Target="styles.xml"/></Relationships>'
    )
    tmp = ruta.with_name(ruta.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", _STYLES)
        for i, h in enumerate(hojas, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _hoja_xml(h))
    tmp.replace(ruta)


# --- lectura ---------------------------------------------------------------------------

def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _texto_si(si: ET.Element) -> str:
    """Texto de una cadena simple (<t>) o con formato (<r><t>), sin la guía fonética."""
    partes = [t.text or "" for t in si.findall(_q("t"))]
    partes += [t.text or "" for r in si.findall(_q("r")) for t in r.findall(_q("t"))]
    return "".join(partes)


def _numero(s: str) -> int | float:
    v = float(s)
    return int(v) if v.is_integer() and abs(v) < 1e15 else v


def leer(ruta: Path) -> dict[str, list[list[Any]]]:
    """Devuelve {nombre de hoja: filas}, cada fila una lista de valores (None si vacía)."""
    try:
        z = zipfile.ZipFile(ruta)
    except zipfile.BadZipFile as e:
        raise ValueError(f"{ruta.name} no es un .xlsx válido.") from e
    with z:
        nombres = set(z.namelist())
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        destino = {r.get("Id"): r.get("Target") for r in rels}
        compartidas: list[str] = []
        if "xl/sharedStrings.xml" in nombres:
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            compartidas = [_texto_si(si) for si in sst.findall(_q("si"))]
        out: dict[str, list[list[Any]]] = {}
        for sh in wb.find(_q("sheets")):
            target = destino.get(sh.get(f"{{{NS_R}}}id"), "")
            parte = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if parte not in nombres:
                continue
            filas: dict[int, dict[int, Any]] = {}
            root = ET.fromstring(z.read(parte))
            data = root.find(_q("sheetData"))
            for rn, row in enumerate(data if data is not None else [], start=1):
                r = int(row.get("r") or rn)
                fila = filas.setdefault(r, {})
                for cn, c in enumerate(row.findall(_q("c"))):
                    ref = c.get("r")
                    col = col_indice(ref) if ref else cn
                    t = c.get("t", "n")
                    v = c.find(_q("v"))
                    if t == "inlineStr":
                        is_ = c.find(_q("is"))
                        valor = _texto_si(is_) if is_ is not None else ""
                    elif v is None or v.text is None:
                        continue
                    elif t == "s":
                        valor = compartidas[int(v.text)]
                    elif t == "b":
                        valor = v.text.strip() == "1"
                    elif t in ("str", "e"):
                        valor = v.text
                    else:
                        try:
                            valor = _numero(v.text)
                        except ValueError:
                            valor = v.text
                    fila[col] = valor
            if filas:
                ultima = max(filas)
                out[sh.get("name")] = [
                    [(filas.get(r) or {}).get(c) for c in range(max(filas.get(r) or {}, default=-1) + 1)]
                    for r in range(1, ultima + 1)
                ]
            else:
                out[sh.get("name")] = []
        return out
