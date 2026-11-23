"""Tareas largas: responder antes del límite del enlace y recoger el resultado después.

El enlace entre el asistente y el PC corta la espera a los 80 s, pero el servidor sigue
trabajando (visto el 2026-11-22: interferencias de un conjunto grande, 110 s). Una herramienta
marcada como larga se ejecuta en un hilo: si termina antes de `espera_s`, devuelve su
resultado como siempre; si no, devuelve `estado: en_curso` con un identificador, y
`resultado_tarea` lo recoge cuando acaba.

Si se vuelve a pedir lo mismo (misma herramienta y mismos argumentos) mientras la tarea
sigue en marcha, no se lanza otra vez: se devuelve la que ya está en curso.
"""

from __future__ import annotations

import functools
import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tarea:
    id: str
    herramienta: str
    clave: str
    inicio: float = field(default_factory=time.time)
    fin: float | None = None
    resultado: Any = None
    error: BaseException | None = None
    hecho: threading.Event = field(default_factory=threading.Event)

    @property
    def en_curso(self) -> bool:
        return not self.hecho.is_set()

    def resumen(self) -> dict[str, Any]:
        t = (self.fin or time.time()) - self.inicio
        estado = "en_curso" if self.en_curso else ("error" if self.error else "terminada")
        r: dict[str, Any] = {"tarea_id": self.id, "herramienta": self.herramienta, "estado": estado,
                             "inicio": time.strftime("%H:%M:%S", time.localtime(self.inicio)),
                             "duracion_s" if not self.en_curso else "transcurrido_s": round(t, 1)}
        if self.error:
            r["error"] = str(self.error)[:300]
        return r


class GestorTareas:
    def __init__(self, espera_s: float = 55.0, guardar: int = 20, audit: Any = None):
        self.espera_s = espera_s
        self.guardar = guardar
        self.audit = audit
        self._tareas: dict[str, Tarea] = {}
        self._lock = threading.Lock()
        self._contador = itertools.count(1)

    # -- gestión ---------------------------------------------------------
    def lanzar(self, herramienta: str, fn: Callable[..., Any], args: tuple, kw: dict) -> tuple[Tarea, bool]:
        clave = herramienta + json.dumps([list(args), kw], sort_keys=True, default=str)
        with self._lock:
            for t in self._tareas.values():
                if t.clave == clave and t.en_curso:
                    return t, False
            tid = f"T{next(self._contador):03d}-{time.strftime('%H%M%S')}"
            tarea = Tarea(tid, herramienta, clave)
            self._tareas[tid] = tarea
            for t in sorted((t for t in self._tareas.values() if not t.en_curso), key=lambda t: t.inicio):
                if len(self._tareas) <= self.guardar:
                    break
                self._tareas.pop(t.id, None)

        def correr() -> None:
            try:
                tarea.resultado = fn(*args, **kw)
            except BaseException as e:  # noqa: BLE001 - se entrega al recoger el resultado
                tarea.error = e
            finally:
                tarea.fin = time.time()
                tarea.hecho.set()
                if self.audit is not None:
                    self.audit.write(event="tarea", tarea=tid, tool=herramienta,
                                     ok=tarea.error is None, s=round(tarea.fin - tarea.inicio, 1))

        threading.Thread(target=correr, name=f"tarea-{tid}", daemon=True).start()
        return tarea, True

    def obtener(self, tarea_id: str | None) -> Tarea:
        with self._lock:
            if not self._tareas:
                raise ValueError("No hay tareas en esta sesión del servidor.")
            if not tarea_id:
                return max(self._tareas.values(), key=lambda t: t.inicio)
            t = self._tareas.get(tarea_id.strip())
        if t is None:
            raise ValueError(f"No existe la tarea {tarea_id} (las tareas se pierden al reiniciar Claude; "
                             "el Excel, si lo hay, sigue en la carpeta de exportación).")
        return t

    def lista(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.resumen() for t in sorted(self._tareas.values(), key=lambda t: -t.inicio)]

    # -- espera y entrega --------------------------------------------------
    def entregar(self, tarea: Tarea, espera_s: float) -> Any:
        """Espera hasta espera_s: devuelve el resultado, relanza el error o da el estado."""
        tarea.hecho.wait(max(0.0, espera_s))
        if tarea.en_curso:
            return {**tarea.resumen(),
                    "siguiente": f"Sigue calculando. Llama a resultado_tarea con tarea_id='{tarea.id}' "
                                 "para recoger el resultado (no repitas la herramienta)."}
        if tarea.error is not None:
            raise tarea.error
        return tarea.resultado

    def larga(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorador: ejecuta la herramienta como tarea y espera como mucho espera_s."""

        @functools.wraps(fn)
        def envoltura(*args: Any, **kw: Any) -> Any:
            tarea, nueva = self.lanzar(fn.__name__, fn, args, kw)
            r = self.entregar(tarea, self.espera_s)
            if isinstance(r, dict) and r.get("estado") == "en_curso" and not nueva:
                r["aviso"] = "Ya había una tarea igual en marcha; no se ha lanzado otra."
            return r

        return envoltura
