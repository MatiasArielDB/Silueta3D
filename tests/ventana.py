"""Prueba de la ventana: abre Silueta3D, carga una imagen y recorre lo principal sin intervención.

Uso:  .venv/bin/python tests/ventana.py        (en Linux sin pantalla: xvfb-run .venv/bin/python tests/ventana.py)

No toca las preferencias guardadas ni la carpeta «salida»: usa carpetas temporales.
"""
from __future__ import annotations

import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "programa"))
import llavero_app as app  # noqa: E402
import llavero_core as c  # noqa: E402

temporal = Path(tempfile.mkdtemp())
app.CONFIG = temporal / "config.json"     # no pisar las preferencias del usuario
app.SALIDA = temporal / "salida"
app.messagebox.askyesno = lambda *a, **k: True
fallas = []


def comprobar(nombre, ok, detalle=""):
    print(f"{'OK ' if ok else 'MAL'}  {nombre:34} {detalle}")
    if not ok:
        fallas.append(nombre)


def main() -> int:
    root = tk.Tk()
    a = app.App(root)

    def pump(t=0.3):
        fin = time.time() + t
        while time.time() < fin:
            root.update()
            time.sleep(0.02)

    def esperar():
        fin = time.time() + 60
        while time.time() < fin:
            root.update()
            time.sleep(0.05)
            if not (a.ocupado or a._control_pendiente or a._previa_programada or a.previa_pendiente):
                break
        pump(0.3)

    pump()
    a.v_ruta.set(str(AQUI / "imagenes" / "escudo.jpg"))
    a.v_nombre.set("escudo")
    a.v["alto"].set("40")
    a.previa()
    esperar()
    comprobar("vista en vivo", a.ultimo_res is not None and a.vista_pil is not None)
    comprobar("medida proporcional", a.v["ancho"].get() not in ("", "0"), f"ancho calculado {a.v['ancho'].get()} mm")
    comprobar("peso en el área 3D", "g de PLA" in a.v_peso.get(), a.v_peso.get())

    zoom_antes = a.zoom
    a._rueda(type("E", (), {"x": 100, "y": 100, "delta": 120})())
    comprobar("zoom de la etiqueta", a.zoom > zoom_antes, a.v_zoom.get())
    a.visor._rueda(None, +1)
    comprobar("zoom del 3D", a.visor.zoom > 1, a.v_zoom3d.get())

    for tipo in c.TIPOS_PIEZA:
        a.v["tipo_pieza"].set(tipo)
        esperar()
        comprobar(f"tipo {tipo}", a.ultimo_res is not None and a.ultimo_res.p.tipo_pieza == tipo,
                  a.v_peso.get())

    a.v["tipo_pieza"].set(c.PIEZA_LLAVERO)
    esperar()
    a.crear()
    esperar()
    carpeta = app.SALIDA / "escudo"
    archivos = sorted(p.name for p in carpeta.iterdir()) if carpeta.exists() else []
    comprobar("crear archivos", len(archivos) == 8 and "escudo_stl.stl" in archivos, f"{len(archivos)} archivos")

    a._cerrar()
    comprobar("preferencias guardadas aparte", app.CONFIG.exists())
    print(f"\n{'Todo bien.' if not fallas else 'Fallaron: ' + ', '.join(fallas)}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
