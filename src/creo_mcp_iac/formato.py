"""Ajustes de formato sobre lo que devuelve CREOSON (comprobados con Creo 12.4.1.0).

- Parámetros: oculta por defecto los metadatos de Windchill/PDM y sustituye los
  nombres de personas por "[rellenado]" (regla del ROADMAP: no trasladar nombres).
- Cotas: convierte el texto simbólico de Creo ("{0:\\x01n\\x02}{1:@D}") en texto
  legible ("Ø34").
- Masa: añade los valores en SI, porque Creo puede trabajar en t·mm (mmNs).
"""

from __future__ import annotations

import re
from typing import Any

# Metadatos de gestión documental que no aportan a la revisión técnica.
_PDM_PREFIXES = ("PTC_WM_", "PROI_", "PDM")
_PDM_NAMES = {"PTC_MODIFIED", "PTC_ORGANIZATION_ID"}

# Parámetros que contienen nombres de personas (cajetín IAC y Windchill).
_PERSON_RE = re.compile(r"(_POR|_BY)$|^PTC_WM_(CREATED|MODIFIED)_BY$", re.IGNORECASE)


def is_pdm(name: str) -> bool:
    n = name.upper()
    return n.startswith(_PDM_PREFIXES) or n in _PDM_NAMES


def clean_params(params: list[dict[str, Any]], include_pdm: bool, redact: bool) -> list[dict[str, Any]]:
    out = []
    for p in params:
        name = str(p.get("name", ""))
        if not include_pdm and is_pdm(name):
            continue
        p = dict(p)
        p.pop("owner_name", None)
        if redact and _PERSON_RE.search(name) and isinstance(p.get("value"), str):
            p["value"] = "[rellenado]" if p["value"].strip() else ""
        out.append(p)
    return out


# --- cotas --------------------------------------------------------------------

# Símbolos de la fuente especial de Creo (\x01<c>\x02). Solo los verificados;
# el resto se muestra como [símbolo:c] para no inventar.
_CREO_SYMBOLS = {"n": "Ø", "$": "°", "|": "|"}  # "|" separa casillas de un marco GD&T
_SYM_RE = re.compile("\x01([^\x02]*)\x02")


def _symbols(m: re.Match) -> str:
    return "".join(_CREO_SYMBOLS.get(c, f"[símbolo:{c}]") for c in m.group(1))
_FIELD_RE = re.compile(r"\{\d+:(.*?)\}", re.DOTALL)


def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s.replace(".", ",")
    return str(v)


def dim_text(item: dict[str, Any]) -> str | None:
    raw = item.get("text")
    if raw is None:
        return None
    lines = raw if isinstance(raw, list) else [raw]
    parts = []
    for line in lines:
        s = str(line)
        s = _FIELD_RE.sub(lambda m: m.group(1), s)
        s = s.replace("@D", _fmt_value(item.get("value")))
        s = _SYM_RE.sub(_symbols, s)
        parts.append(s.strip())
    return " / ".join(p for p in parts if p)


def note_text(raw: str) -> str:
    """Traduce los símbolos de Creo en el texto de una nota."""
    return _SYM_RE.sub(_symbols, raw)


# Notas del cajetín que muestran parámetros con nombres de personas (&DISEÑADO_POR...).
_NOTE_PERSON_RE = re.compile(r"&[\wÀ-ÿ]*(_POR|_BY)\b", re.IGNORECASE)


def note_has_person(raw: str) -> bool:
    return bool(_NOTE_PERSON_RE.search(raw or ""))


def _r(v: Any, nd: int = 6) -> Any:
    return round(v, nd) if isinstance(v, float) else v


def tolerance(d: dict[str, Any]) -> dict[str, Any]:
    """Normaliza la tolerancia de una cota.

    tipo: "explicita" (±, +/-, límites), "ajuste_iso" (p. ej. H7),
    "general" (rige la tolerancia general del plano) o "sin_datos" (CREOSON no
    devuelve tolerancia; habitual al leer la pieza en vez del plano).
    """
    tt = (d.get("tolerance_type") or "").lower()
    if not tt:
        return {"tipo": "sin_datos"}
    if tt == "symmetric":
        v = _r(d.get("tol_symmetric_value"))
        return {"tipo": "explicita", "texto": f"±{_fmt_value(v)}", "mas": v, "menos": v}
    if tt in ("plus_minus", "plusminus"):
        a, b = _r(d.get("tol_plus")), _r(d.get("tol_minus"))
        return {"tipo": "explicita", "texto": f"+{_fmt_value(a)}/-{_fmt_value(b)}", "mas": a, "menos": b}
    if tt == "isodin":
        ttype = str(d.get("tol_table_type") or "").lower()
        name = d.get("tol_table_name")
        if ttype == "general" or not name or name == "NONE":
            return {"tipo": "general", "texto": "tolerancia general"}
        return {"tipo": "ajuste_iso", "texto": str(name), "tabla": ttype}
    if tt == "nominal":
        return {"tipo": "general", "texto": "tolerancia general"}
    extra = {k: _r(v) for k, v in d.items() if k.startswith("tol")}
    return {"tipo": "explicita", "texto": tt, "datos": extra}


def add_dim_text(dims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in dims:
        d = dict(d)
        if isinstance(d.get("value"), float):
            d["value"] = _r(d["value"])
        t = dim_text(d)
        if t is not None:
            d["texto_legible"] = t
        tol = tolerance(d)
        d["tolerancia"] = tol
        d["tiene_tolerancia"] = tol["tipo"] in ("explicita", "ajuste_iso")
        out.append(d)
    return out


def blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# --- masa ---------------------------------------------------------------------

_LEN_TO_M = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "in": 0.0254, "ft": 0.3048, "micron": 1e-6, "um": 1e-6}
_MASS_TO_KG = {"tonne": 1000.0, "t": 1000.0, "kg": 1.0, "g": 1e-3, "gr": 1e-3,
               "lbm": 0.45359237, "lb": 0.45359237, "slug": 14.59390294}


def check_mass_param(params: dict[str, Any], calc_mass: float | None, model_mass_unit: Any) -> dict[str, Any] | None:
    """Compara el parámetro MASA del cajetín con la masa calculada por Creo."""
    if "MASA" not in params or calc_mass is None:
        return None
    mu = str(model_mass_unit or "").strip().lower()
    if mu not in _MASS_TO_KG:
        return None
    calc_kg = calc_mass * _MASS_TO_KG[mu]
    unit_raw = params.get("MASA_UNIT")
    unit = str(unit_raw).strip().lower() if not blank(unit_raw) else None
    nota = None
    if unit is None:
        unit = mu
        nota = f"sin MASA_UNIT: se supone la unidad de masa del modelo ({model_mass_unit})"
    if unit not in _MASS_TO_KG:
        return {"estado": "unidad_desconocida", "masa_parametro": params["MASA"], "masa_unit": unit_raw}
    try:
        param_kg = float(params["MASA"]) * _MASS_TO_KG[unit]
    except (TypeError, ValueError):
        return {"estado": "no_numerico", "masa_parametro": params["MASA"]}
    diff = (param_kg - calc_kg) / calc_kg * 100 if calc_kg else None
    estado = "coincide" if diff is not None and abs(diff) <= 0.1 else "difiere"
    out = {"estado": estado, "parametro_kg": param_kg, "calculada_kg": calc_kg,
           "diferencia_pct": round(diff, 2) if diff is not None else None}
    if nota:
        out["nota"] = nota
    return out


def massprops_si(props: dict[str, Any], length_unit: Any, mass_unit: Any) -> dict[str, Any] | None:
    lu = str(length_unit or "").strip().lower()
    mu = str(mass_unit or "").strip().lower()
    if lu not in _LEN_TO_M or mu not in _MASS_TO_KG:
        return None
    L, M = _LEN_TO_M[lu], _MASS_TO_KG[mu]
    si: dict[str, Any] = {}
    if props.get("mass") is not None:
        si["masa_kg"] = props["mass"] * M
    if props.get("volume") is not None:
        si["volumen_m3"] = props["volume"] * L**3
    if props.get("density") is not None:
        si["densidad_kg_m3"] = props["density"] * M / L**3
    if props.get("surface_area") is not None:
        si["area_m2"] = props["surface_area"] * L**2
    cg = props.get("ctr_grav")
    if isinstance(cg, dict):
        si["cdg_mm"] = {k: v * L * 1000 for k, v in cg.items()}
    t = props.get("ctr_grav_inertia_tensor")
    if isinstance(t, dict):
        f = M * L**2
        si["inercia_cdg_kg_m2"] = {ax: {k: v * f for k, v in row.items()} for ax, row in t.items()}
    return si
