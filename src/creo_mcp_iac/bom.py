"""Conversión del árbol de CREOSON (bom.get_paths) a lista de materiales."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    ch = node.get("children") or []
    if isinstance(ch, dict):
        return [ch]
    return list(ch)


def root_node(raw: dict[str, Any]) -> dict[str, Any]:
    """CREOSON devuelve {'file', 'generic', 'children': <nodo raíz>}."""
    ch = raw.get("children")
    if isinstance(ch, dict) and ch.get("file"):
        return ch
    if isinstance(ch, list) and len(ch) == 1 and ch[0].get("seq_path") in ("root", None) \
            and ch[0].get("file") == raw.get("file"):
        return ch[0]
    # Estructura plana: la propia respuesta es la raíz
    return {"file": raw.get("file"), "seq_path": "root", "children": ch or []}


def build_bom(raw: dict[str, Any], max_depth: int = 50) -> dict[str, Any]:
    """Devuelve lista indentada (por nivel) y lista agregada (cantidad total).

    - `indented`: una fila por posición del árbol, con nivel y cantidad en ese
      nivel (componentes iguales bajo el mismo padre se agrupan).
    - `flat`: una fila por fichero con la cantidad total en todo el conjunto
      (multiplicando por la cantidad de los subconjuntos padre).
    """
    root = root_node(raw)
    indented: list[dict[str, Any]] = []
    totals: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def walk(node: dict[str, Any], level: int, mult: int) -> None:
        if level > max_depth:
            return
        groups: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        for c in _children(node):
            f = c.get("file")
            if not f:
                continue
            groups.setdefault(f.upper(), []).append(c)
        for nodes in groups.values():
            first = nodes[0]
            name = first["file"]
            qty = len(nodes)
            is_asm = name.upper().endswith(".ASM")
            indented.append(
                {
                    "level": level,
                    "file": name,
                    "qty": qty,
                    "type": "ASM" if is_asm else "PRT",
                    "seq_paths": [n.get("seq_path") for n in nodes if n.get("seq_path")],
                }
            )
            t = totals.setdefault(
                name.upper(), {"file": name, "type": "ASM" if is_asm else "PRT", "qty_total": 0}
            )
            t["qty_total"] += qty * mult
            # Basta con recorrer uno: todas las instancias tienen la misma estructura
            if _children(first):
                walk(first, level + 1, mult * qty)

    walk(root, 1, 1)
    return {
        "assembly": root.get("file") or raw.get("file"),
        "generic": raw.get("generic"),
        "indented": indented,
        "flat": list(totals.values()),
    }
