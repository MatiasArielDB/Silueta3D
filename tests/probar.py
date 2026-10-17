"""Pruebas automáticas del núcleo (sin ventana).

Uso:  .venv\\Scripts\\python.exe tests\\probar.py

Verifica que cada pieza mida lo pedido y que el STL quede cerrado (sin agujeros en la malla),
para los distintos tipos de pieza, huecos y formas.
"""
from __future__ import annotations

import math
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "programa"))
import llavero_core as c  # noqa: E402

IMAGENES = AQUI / "imagenes"


def stl_cerrado(ruta) -> bool:
    """Cada arista compartida por exactamente dos triángulos con orientación opuesta (como lo lee un slicer)."""
    datos = np.fromfile(ruta, np.uint8)
    tri = np.frombuffer(datos[84:].tobytes(), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])["v"]
    _, inv = np.unique(tri.reshape(-1, 3), axis=0, return_inverse=True)
    f = inv.reshape(-1, 3)
    if (f[:, 0] == f[:, 1]).any() or (f[:, 1] == f[:, 2]).any() or (f[:, 0] == f[:, 2]).any():
        return False
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    unicas, cuantas = np.unique(e, axis=0, return_counts=True)
    opuestas = set(map(tuple, e[:, ::-1].tolist()))
    return bool((cuantas == 1).all() and all(tuple(x) in opuestas for x in unicas.tolist()))


def imagen_anillo(carpeta: Path) -> Path:
    """Aro rojo con fondo transparente: el centro es un hueco para calar."""
    img = Image.new("RGBA", (800, 800), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([60, 60, 740, 740], fill=(200, 30, 40, 255))
    d.ellipse([260, 260, 540, 540], fill=(0, 0, 0, 0))
    ruta = carpeta / "anillo.png"
    img.save(ruta)
    return ruta


def main() -> int:
    temporal = Path(tempfile.mkdtemp())
    anillo = imagen_anillo(temporal)
    casos = [
        # (nombre, imagen, parámetros, comprobación extra)
        ("escudo 40 alto", IMAGENES / "escudo.jpg", dict(alto=40), None),
        ("escudo argolla diagonal", IMAGENES / "escudo.jpg", dict(alto=25, argolla="Arriba izquierda"), None),
        ("estrella partes separadas", IMAGENES / "estrella.png", dict(alto=None, ancho=45), None),
        ("estrella margen blanco", IMAGENES / "estrella.png", dict(alto=None, ancho=45, margen=1.5), None),
        ("corazón svg", IMAGENES / "corazon.svg", dict(alto=35), None),
        ("foto rectángulo", IMAGENES / "foto.jpg", dict(alto=40), None),
        ("foto escudo", IMAGENES / "foto.jpg", dict(alto=40, forma="Escudo"), None),
        ("foto círculo base", IMAGENES / "foto.jpg", dict(alto=30, forma="Círculo", tipo_pieza=c.PIEZA_BASE),
         lambda r: r.geo.centro_argolla is None),
        ("imán", IMAGENES / "escudo.jpg", dict(alto=45, tipo_pieza=c.PIEZA_IMAN, capas_piso=14),
         lambda r: r.geo.iman is not None and r.geo.centro_argolla is None),
        ("pin / snap", IMAGENES / "escudo.jpg", dict(alto=40, tipo_pieza=c.PIEZA_PIN, capas_piso=9),
         lambda r: r.geo.pin is not None),
        ("estirado 60x20", IMAGENES / "escudo.jpg", dict(alto=20, ancho=60, proporcional=False),
         lambda r: abs(r.medidas()["cuerpo"][0] - 60) < 0.05),
        ("hueco con pared", anillo, dict(alto=40, huecos=[(0.5, 0.5)], tipo_hueco=c.TIPO_PARED),
         lambda r: len(r.geo.K.interiors) == 1),
        ("hueco ventana", anillo, dict(alto=40, huecos=[(0.5, 0.5)], tipo_hueco=c.TIPO_VENTANA),
         lambda r: len(r.geo.U.interiors) == 2),
        ("hueco solo papel", anillo, dict(alto=40, huecos=[(0.5, 0.5)], tipo_hueco=c.TIPO_PAPEL),
         lambda r: len(r.geo.L.interiors) == 1 and len(r.geo.U.interiors) == 1),
    ]
    fallas = 0
    for nombre, imagen, kw, extra in casos:
        try:
            p = c.Parametros(**kw)
            r = c.procesar(imagen, p)
            stl = temporal / "prueba.stl"
            c.guardar_stl(stl, r.vertices, r.triangulos)
            ancho, alto = r.medidas()["cuerpo"]
            pedido = p.alto or p.ancho
            medido = alto if p.alto else ancho
            ok_medida = math.isclose(medido, pedido, abs_tol=0.05)
            ok_stl = stl_cerrado(stl)
            ok_extra = extra(r) if extra else True
            ok = ok_medida and ok_stl and ok_extra
            detalle = f"cuerpo {ancho:.1f} x {alto:.1f} mm" + ("" if ok_stl else " · STL ABIERTO") + \
                      ("" if ok_extra else " · comprobación extra FALLÓ")
        except Exception:
            ok, detalle = False, traceback.format_exc(limit=2)
        fallas += not ok
        print(f"{'OK ' if ok else 'MAL'}  {nombre:28} {detalle}")

    errores, _ = c.validar(c.Parametros(capa=0.32))
    ok = bool(errores)
    fallas += not ok
    print(f"{'OK ' if ok else 'MAL'}  {'control de capa inválida':28} {errores[:1]}")

    # peso: el volumen de la malla tiene que coincidir con el que sale de las áreas (pieza - hueco)
    r = c.procesar(IMAGENES / "escudo.jpg", c.Parametros(alto=40))
    esperado = r.geo.U.area * (r.p.piso + r.p.profundidad) - r.geo.K.area * r.p.profundidad
    ok = math.isclose(r.volumen, esperado, rel_tol=1e-3) and math.isclose(r.peso, esperado / 1000 * 1.24, rel_tol=1e-3)
    fallas += not ok
    print(f"{'OK ' if ok else 'MAL'}  {'peso de la pieza':28} {r.peso:.2f} g de PLA · resina {r.resina_ml:.2f} ml")

    total = len(casos) + 2
    print(f"\n{total - fallas} de {total} pruebas bien.")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
