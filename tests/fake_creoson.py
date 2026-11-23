"""CREOSON simulado para pruebas sin Creo.

Responde con datos fijos y registra cada petición. Cualquier comando que no
esté en READ_COMMANDS se marca como 'mutating' para que los tests puedan
comprobar que el servidor MCP nunca lo envía.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FILES = ["CONJUNTO.ASM", "SUBCONJ.ASM", "SOPORTE.PRT", "TORNILLO.PRT", "SOPORTE.DRW", "FORM_A3.FRM"]

BOM = {
    "file": "CONJUNTO.ASM",
    "generic": None,
    "children": {
        "seq_path": "root",
        "file": "CONJUNTO.ASM",
        "children": [
            {"seq_path": "root.1", "file": "SOPORTE.PRT"},
            {"seq_path": "root.2", "file": "SOPORTE.PRT"},
            {
                "seq_path": "root.3",
                "file": "SUBCONJ.ASM",
                "children": [{"seq_path": f"root.3.{i}", "file": "TORNILLO.PRT"} for i in range(1, 5)],
            },
            {
                "seq_path": "root.4",
                "file": "SUBCONJ.ASM",
                "children": [{"seq_path": f"root.4.{i}", "file": "TORNILLO.PRT"} for i in range(1, 5)],
            },
        ],
    },
}

READ_COMMANDS = {
    ("connection", "connect"), ("connection", "is_creo_running"), ("connection", "disconnect"),
    ("creo", "pwd"), ("file", "get_active"), ("file", "list"), ("file", "get_fileinfo"),
    ("file", "get_length_units"), ("file", "get_mass_units"), ("file", "get_cur_material"),
    ("file", "list_instances"), ("file", "massprops"), ("parameter", "list"),
    ("dimension", "list"), ("dimension", "list_detail"), ("feature", "list"),
    ("bom", "get_paths"), ("drawing", "list_models"), ("drawing", "get_num_sheets"),
    ("drawing", "get_sheet_size"), ("drawing", "get_sheet_scale"),
    ("drawing", "get_sheet_format"), ("drawing", "list_view_details"),
    ("drawing", "list_symbols"), ("note", "list"),
    ("interface", "export_file"), ("interface", "export_pdf"),
    ("view", "list"), ("drawing", "list_views"), ("creo", "get_config"), ("creo", "set_creo_version"),
}


class FakeCreoson:
    def __init__(self):
        self.requests: list[dict] = []
        self.active = "CONJUNTO.ASM"
        self.creo_running = True
        self.files = list(FILES)
        self.params: dict[str, str] = {}
        # Parámetros por modelo (mayúsculas) -> lista de {name, type, value}; si un
        # modelo no está aquí se usa la lista fija.
        self.model_params: dict[str, list[dict]] = {}
        self.bom = BOM
        self.massprops: dict[str, dict] = {}
        self.drawing_models: dict[str, list[str]] = {}
        self.cortar_en_set = 0  # n > 0: la n-ésima escritura de parámetro corta la conexión  # modelo (mayúsculas) -> respuesta de massprops
        self.views: dict[str, list[str]] = {}
        self.template_dir = Path(".")
        # Simula Creo 12: la exportación clásica aborta; solo funciona "advanced".
        self.classic_export_aborts = False
        self.export_aborts = False
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()

    @property
    def commands(self):
        return [(r["command"], r["function"]) for r in self.requests]

    @property
    def mutating(self):
        return [c for c in self.commands if c not in READ_COMMANDS]

    # ------------------------------------------------------------------
    def respond(self, cmd: str, fn: str, data: dict) -> dict:
        ok = lambda d=None: {"status": {"error": False}, "sessionId": "S1", "data": d}  # noqa: E731
        err = lambda m: {"status": {"error": True, "message": m}}  # noqa: E731
        f = (data or {}).get("file") or (data or {}).get("drawing")
        if (cmd, fn) == ("drawing", "create"):
            f = data.get("model")
        if (cmd, fn) == ("drawing", "set_sheet_format"):
            f = data.get("drawing")
        if f and "*" not in f and f.upper() not in {x.upper() for x in self.files}:
            return err(f"File not in session: {f}")
        key = (cmd, fn)
        if getattr(self, "sesion_caducada", 0) and cmd != "connection":
            self.sesion_caducada -= 1   # CREOSON reiniciado: la sesión anterior ya no existe
            return err("No session found")
        if key == ("connection", "connect"):
            return {"status": {"error": False}, "sessionId": "S1"}
        if key == ("connection", "is_creo_running"):
            return ok({"running": self.creo_running})
        if key == ("creo", "pwd"):
            return ok({"dirname": "C:/creo_work/"})
        if key == ("file", "get_active"):
            return ok({"file": self.active, "dirname": "C:/creo_work/"})
        if key == ("file", "list"):
            pat = (data or {}).get("file")
            if pat and pat.startswith("*."):
                return ok({"files": [x for x in self.files if x.upper().endswith(pat[1:].upper())]})
            return ok({"files": self.files})
        if key == ("file", "get_fileinfo"):
            return ok({"dirname": "C:/creo_work/", "file": f, "revision": 3})
        if key == ("file", "get_length_units"):
            return ok({"units": "mm"})
        if key == ("file", "get_mass_units"):
            return ok({"units": "kg"})
        if key == ("file", "get_cur_material"):
            if not f.endswith(".PRT"):
                return err("not a part")
            return ok({"material": "AL6061"})
        if key == ("file", "list_instances"):
            return ok({"dirname": "C:/creo_work/", "generic": f, "files": []})
        if key == ("file", "massprops") and f.upper() in self.massprops:
            return ok(self.massprops[f.upper()])
        if key == ("file", "massprops"):
            return ok({"volume": 1000.0, "mass": 0.0027, "density": 2.7e-6,
                       "surface_area": 600.0, "ctr_grav": {"x": 5.0, "y": 5.0, "z": 5.0}})
        if key == ("parameter", "list") and f == "*" and self.model_params:
            names = (data or {}).get("names")
            plist = [{"designate": True, "encoded": False, "owner_name": m, **p}
                     for m, ps in self.model_params.items() for p in ps
                     if not names or p["name"] in names]
            return ok({"paramlist": plist})
        if key == ("parameter", "list") and f and f.upper() in self.model_params:
            plist = [{"designate": True, "encoded": False, "owner_name": f, **p}
                     for p in self.model_params[f.upper()]]
            name = (data or {}).get("name")
            names = (data or {}).get("names")
            if name:
                plist = [x for x in plist if x["name"] == name]
            if names:
                plist = [x for x in plist if x["name"] in names]
            return ok({"paramlist": plist})
        if key == ("parameter", "list"):
            plist = [
                {"name": "PTC_MATERIAL_NAME", "type": "STRING", "value": "AL6061",
                 "designate": True, "encoded": False, "owner_name": f},
                {"name": "DESCRIPCION", "type": "STRING",
                 "value": base64.b64encode("Soporte Ø20 ±0,1".encode()).decode(),
                 "designate": True, "encoded": True, "owner_name": f},
                {"name": "TOLERANCIA", "type": "STRING", "value": "FINA",
                 "designate": True, "encoded": False, "owner_name": f},
                {"name": "DISEÑADO_POR", "type": "STRING", "value": "usuario1",
                 "designate": True, "encoded": False, "owner_name": f},
                {"name": "PTC_WM_CREATED_BY", "type": "STRING", "value": "usuario1 (usuario1: IAC)",
                 "designate": False, "encoded": False, "owner_name": f},
                {"name": "PDMREV", "type": "STRING", "value": "A.16",
                 "designate": False, "encoded": False, "owner_name": f},
            ]
            plist += [{"name": "MASA", "type": "DOUBLE", "value": 3.5, "designate": True,
                       "encoded": False, "owner_name": f},
                      {"name": "MASA_UNIT", "type": "STRING", "value": "gr", "designate": True,
                       "encoded": False, "owner_name": f}]
            name = (data or {}).get("name")
            names = (data or {}).get("names")
            if name:
                plist = [x for x in plist if x["name"] == name]
            if names:
                plist = [x for x in plist if x["name"] in names]
            return ok({"paramlist": plist})
        if key in (("dimension", "list"), ("dimension", "list_detail")):
            if f.endswith(".FRM"):
                return err("Error: Unimplemented function")
            return ok({"dimlist": [
                {"name": "d0", "value": 20.0, "dim_type": "diameter", "tolerance_type": "plus_minus",
                 "tol_plus": 0.1, "tol_minus": 0.1, "dwg_dim": False, "sheet": 1, "view_name": "FRONT"},
                {"name": "d1", "value": 45.0, "dim_type": "linear", "dwg_dim": False},
                {"name": "d48", "value": 34.0, "dim_type": "diameter", "dwg_dim": False, "sheet": 0,
                 "encoded": False, "text": ["{0:\u0001n\u0002}{1:@D}\n"]},
                {"name": "ad149", "value": 1519.17, "dim_type": "radial", "dwg_dim": False, "sheet": 1,
                 "view_name": "izquierda_7", "tolerance_type": "symmetric", "tol_symmetric_value": 15.2,
                 "text": ["{0:R}{1:@D}\n"]},
                {"name": "d173", "value": 5.0, "dim_type": "linear", "dwg_dim": False, "sheet": 1,
                 "tolerance_type": "isodin", "tol_table_type": "general", "tol_table_name": "NONE",
                 "tol_table_column": -1,
                 "text": ["{0:Ch}{1:@D}{2:x45\u0001$\u0002}\n", "{3:(x4)}\n"]},
            ]})
        if key == ("feature", "list"):
            return ok({"featlist": [
                {"name": "PROTRUSION_1", "type": "PROTRUSION", "status": "ACTIVE", "feat_id": 40, "feat_number": 5},
                {"name": "HOLE_1", "type": "HOLE", "status": "ACTIVE", "feat_id": 52, "feat_number": 6},
            ]})
        if key == ("bom", "get_paths"):
            return ok(self.bom)
        if key == ("drawing", "list_models"):
            return ok({"files": self.drawing_models.get((data.get("drawing") or "").upper(), ["SOPORTE.PRT"])})
        if key == ("drawing", "get_num_sheets"):
            return ok({"num_sheets": 2})
        if key == ("drawing", "get_sheet_size"):
            return ok({"size": "A3"})
        if key == ("drawing", "get_sheet_scale"):
            return ok({"scale": 0.5})
        if key == ("drawing", "get_sheet_format"):
            return ok({"file": "IAC_A3.FRM", "name": "IAC_A3"})
        if key == ("drawing", "list_view_details"):
            return ok({"views": [{"name": "FRONT", "sheet": 1, "view_model": "SOPORTE.PRT"}]})
        if key == ("drawing", "list_symbols"):
            return ok({"symbols": [{"id": 7, "symbol_name": "RUGOSIDAD", "sheet": 1}]})
        if key == ("note", "list"):
            nota = "TOLERANCIA GENERAL ISO 2768-f / \x01n\x024 H7"
            enc = base64.b64encode(nota.encode()).decode()
            return ok({"itemlist": [
                {"name": "NOTA_1", "value": "ANODIZADO NEGRO", "value_expanded": "ANODIZADO NEGRO",
                 "encoded": False},
                {"name": "NOTA_2", "value": enc, "value_expanded": enc, "encoded": True},
                {"name": "NOTA_3", "value": "&DISEÑADO_POR", "value_expanded": " E.PEREZ"},
                {"name": "NOTA_4", "value": "&DG0", "value_expanded": "\x01l|\x020.06\x01|\x02A"},
            ]})
        # --- planos (escritura) ---
        if key == ("creo", "set_creo_version"):
            self.creo_version = data["version"]
            return ok()
        if key == ("creo", "get_config"):
            vals = {"template_drawing": [str(self.template_dir / "iac_a3_pieza.drw")],
                    "pro_format_dir": [str(self.template_dir)]}
            return ok({"values": vals.get(data["name"], [])})
        if key == ("view", "list"):
            return ok({"viewlist": ["FRONT", "TOP", "RIGHT", "ISO"]})
        if key == ("drawing", "create"):
            name = data["drawing"] + ".drw"
            self.files.append(name.upper())
            self.views[name.upper()] = []
            return ok({"drawing": name})
        if key == ("drawing", "create_gen_view"):
            self.views[data["drawing"].upper()].append(data.get("view") or data["model_view"])
            return ok()
        if key == ("drawing", "create_proj_view"):
            self.views[data["drawing"].upper()].append(data.get("view") or "proj_1")
            return ok()
        if key == ("drawing", "list_views"):
            return ok({"views": self.views.get(data["drawing"].upper(), [])})
        if key == ("drawing", "delete_view"):
            self.views[data["drawing"].upper()].remove(data["view"])
            return ok()
        if key in (("drawing", "set_view_loc"), ("drawing", "scale_view"), ("drawing", "scale_sheet"),
                   ("drawing", "set_sheet_format"), ("drawing", "regenerate"),
                   ("drawing", "create_symbol"), ("note", "set"), ("dimension", "show")):
            return ok()
        if key == ("parameter", "set"):
            self.params[data["name"]] = data.get("value")
            for p in self.model_params.get((data.get("file") or "").upper(), []):
                if p["name"] == data["name"]:
                    p["value"] = data.get("value")
            return ok()
        if key == ("interface", "export_pdf"):
            out = Path(data["dirname"]) / data["filename"]
            out.write_bytes(b"%PDF-1.7\n%fake\n%%EOF\n")
            return ok()
        if key == ("interface", "export_file"):
            if self.export_aborts or (self.classic_export_aborts and not data.get("advanced")):
                return err("A Pro/TOOLKIT error has occurred: Abort.  Check your model/drawing for accuracy.")
            out = Path(data["dirname"]) / data["filename"]
            out.write_bytes(b"ISO-10303-21;\n/* fake */\nEND-ISO-10303-21;\n")
            return ok({"dirname": data["dirname"], "filename": data["filename"]})
        return err(f"fake: comando no simulado {cmd}.{fn}")

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append(req)
                if (req["command"], req["function"]) == ("parameter", "set") and fake.cortar_en_set:
                    fake.cortar_en_set -= 1
                    if fake.cortar_en_set == 0:
                        self.close_connection = True
                        self.connection.close()   # Creo deja de responder a mitad de la escritura
                        return
                body = json.dumps(fake.respond(req["command"], req["function"], req.get("data") or {}))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        return H
