"""Visor 3D simple para tkinter: gira, hace zoom y mueve la pieza (sin dependencias de OpenGL).

Dibuja con el algoritmo del pintor (de atrás hacia adelante) usando OpenCV, con sombreado según la
luz y la altura, y las aristas marcadas para que se lea la forma.
"""
from __future__ import annotations

import math
import tkinter as tk

import cv2
import numpy as np
from PIL import Image, ImageTk

COLOR_PIEZA = np.array([170, 110, 70], np.float32)   # BGR: azul como en la vista de la base
COLOR_HUECO = np.array([235, 215, 200], np.float32)   # BGR: celeste, el piso donde va la etiqueta
COLOR_IMAN = np.array([90, 160, 230], np.float32)     # BGR: naranja, huecos de imán o de la cabeza del pin
COLOR_FONDO = (246, 246, 246)
LUZ = np.array([-0.3, 0.4, 0.87], np.float32)
LUZ /= np.linalg.norm(LUZ)
ANGULO_ARISTA = math.radians(30)
PISTA = "Rueda: zoom · izq.: mover · der.: girar"


def _rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], np.float32)


def _rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float32)


def _rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float32)


def vista_isometrica():
    return _rot_x(math.radians(-55)) @ _rot_z(math.radians(-25))


class Malla:
    """Triángulos listos para dibujar: normales, alturas y aristas marcadas."""

    def __init__(self, vertices: np.ndarray, triangulos: np.ndarray, piso: float | None = None):
        v = vertices.astype(np.float32)
        centro = (v.min(0) + v.max(0)) / 2
        self.alto = float(v[:, 2].max() - v[:, 2].min()) or 1.0
        self.z0 = float(v[:, 2].min())
        self.v = v - centro
        self.t = triangulos.astype(np.int64)
        self.radio = float(np.linalg.norm(v.max(0) - v.min(0)) / 2) or 1.0
        a, b, c = (self.v[self.t[:, i]] for i in range(3))
        n = np.cross(b - a, c - a)
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        self.n = n
        # colores como en la vista de la base: piso del hueco celeste, el resto azul
        z = vertices[self.t, 2].mean(1) - self.z0
        arriba = n[:, 2] > 0.9
        if piso is None:
            piso_hueco = arriba & (z > 0.01) & (z < self.alto - 0.01)
            rebaje = np.zeros(len(z), bool)
        else:
            piso_hueco = arriba & (np.abs(z - piso) < 0.01)
            rebaje = arriba & (z > 0.01) & (z < piso - 0.01)      # fondo del rebaje de la cabeza del pin
        techo_iman = (n[:, 2] < -0.9) & (z > 0.01)               # cara hacia abajo que no es la base: imán
        self.color = np.where(piso_hueco[:, None], COLOR_HUECO, COLOR_PIEZA).astype(np.float32)
        self.color[techo_iman | rebaje] = COLOR_IMAN
        self.aristas = self._aristas_marcadas()

    def _aristas_marcadas(self):
        """Aristas entre caras que forman un ángulo marcado (bordes de la pieza), por triángulo."""
        e = np.concatenate([self.t[:, [0, 1]], self.t[:, [1, 2]], self.t[:, [2, 0]]])
        cara = np.tile(np.arange(len(self.t)), 3)
        clave = np.sort(e, axis=1)
        orden = np.lexsort((clave[:, 1], clave[:, 0]))
        clave, cara, e = clave[orden], cara[orden], e[orden]
        iguales = np.all(clave[1:] == clave[:-1], axis=1)
        i = np.flatnonzero(iguales)
        c1, c2 = cara[i], cara[i + 1]
        cos = np.einsum("ij,ij->i", self.n[c1], self.n[c2])
        marcadas = cos < math.cos(ANGULO_ARISTA)
        por_cara = {}
        for (p, q), f1, f2 in zip(e[i][marcadas], c1[marcadas], c2[marcadas]):
            por_cara.setdefault(int(f1), []).append((p, q))
            por_cara.setdefault(int(f2), []).append((p, q))
        return por_cara


def dibujar(malla: Malla, R, ancho, alto, zoom=1.0, desplazamiento=(0, 0), ss=1):
    """Imagen BGR de la pieza vista con la rotación R (proyección ortográfica)."""
    W, H = ancho * ss, alto * ss
    img = np.full((H, W, 3), COLOR_FONDO, np.uint8)
    escala = min(W, H) * 0.92 / (2 * malla.radio) * zoom
    P = malla.v @ R.T
    xy = np.empty((len(P), 2), np.float32)
    xy[:, 0] = W / 2 + desplazamiento[0] * ss + escala * P[:, 0]
    xy[:, 1] = H / 2 + desplazamiento[1] * ss - escala * P[:, 1]
    n = malla.n @ R.T
    visibles = np.flatnonzero(n[:, 2] > 1e-4)                 # se descartan las caras de espaldas
    prof = P[malla.t[visibles], 2].mean(1)
    visibles = visibles[np.argsort(prof)]                      # de atrás hacia adelante
    luz = 0.6 + 0.4 * np.clip(n[visibles] @ LUZ, 0, 1)
    colores = (malla.color[visibles] * luz[:, None]).clip(0, 255).astype(np.uint8)
    pts = np.round(xy * 4).astype(np.int32)                    # 2 bits de precisión subpixel
    borde = tuple(int(x) for x in (COLOR_PIEZA * 0.35))
    grosor = max(1, ss)
    for f, col in zip(visibles, colores):
        cv2.fillConvexPoly(img, pts[malla.t[f]], col.tolist(), lineType=cv2.LINE_8, shift=2)
        for p, q in malla.aristas.get(int(f), ()):
            cv2.line(img, tuple(pts[p]), tuple(pts[q]), borde, grosor, lineType=cv2.LINE_AA, shift=2)
    if ss > 1:
        img = cv2.resize(img, (ancho, alto), interpolation=cv2.INTER_AREA)
    return img


class Visor3D:
    """Canvas de tkinter con la pieza: botón izquierdo mueve, botón derecho gira, rueda acerca."""

    def __init__(self, canvas: tk.Canvas, v_zoom: tk.StringVar | None = None):
        self.c = canvas
        self.v_zoom = v_zoom
        self.malla: Malla | None = None
        self.texto = ""
        self.R = np.eye(3, dtype=np.float32)
        self.zoom = 1.0
        self.desplazamiento = [0.0, 0.0]
        self.ultimo = None
        self.foto = None
        self._pendiente = None
        canvas.bind("<Configure>", lambda _: self.dibujar())
        canvas.bind("<ButtonPress-1>", self._presionar)
        canvas.bind("<B1-Motion>", self._mover)
        canvas.bind("<ButtonRelease-1>", self._soltar)
        for boton in (2, 3):   # clic derecho: 2 en macOS, 3 en Windows/Linux (el del medio también gira)
            canvas.bind(f"<ButtonPress-{boton}>", self._presionar)
            canvas.bind(f"<B{boton}-Motion>", self._girar)
            canvas.bind(f"<ButtonRelease-{boton}>", self._soltar)
        canvas.bind("<MouseWheel>", self._rueda)
        canvas.bind("<Button-4>", lambda e: self._rueda(e, +1))   # rueda en Linux
        canvas.bind("<Button-5>", lambda e: self._rueda(e, -1))
        canvas.bind("<Double-Button-1>", lambda _: self.frontal())

    def cargar(self, vertices, triangulos, texto="", piso=None):
        self.malla = Malla(vertices, triangulos, piso)
        self.texto = texto
        self.dibujar()

    def frontal(self):
        self.R, self.zoom, self.desplazamiento = np.eye(3, dtype=np.float32), 1.0, [0.0, 0.0]
        self.dibujar()

    def ajustar(self):
        """100 % y centrada, sin cambiar el giro."""
        self.zoom, self.desplazamiento = 1.0, [0.0, 0.0]
        self.dibujar()

    def atras(self):
        self.R, self.zoom, self.desplazamiento = _rot_y(math.pi), 1.0, [0.0, 0.0]
        self.dibujar()

    def isometrica(self):
        self.R, self.zoom, self.desplazamiento = vista_isometrica(), 1.0, [0.0, 0.0]
        self.dibujar()

    def dibujar(self, rapido=False):
        if self.v_zoom is not None:
            self.v_zoom.set(f"{self.zoom * 100:.0f} %")
        self.c.delete("all")
        w, h = self.c.winfo_width(), self.c.winfo_height()
        if not self.malla or w < 20 or h < 20:
            return
        img = dibujar(self.malla, self.R, w, h, self.zoom, self.desplazamiento, ss=1 if rapido else 2)
        self.foto = ImageTk.PhotoImage(Image.fromarray(img[:, :, ::-1]))
        self.c.create_image(0, 0, image=self.foto, anchor="nw")
        if self.texto:
            self.c.create_text(w / 2, h - 28, text=self.texto, fill="#333")
        self.c.create_text(8, h - 6, anchor="sw", fill="#999", text=PISTA)

    def _presionar(self, ev):
        self.ultimo = (ev.x, ev.y)

    def _girar(self, ev):
        if not self.ultimo:
            return
        dx, dy = ev.x - self.ultimo[0], ev.y - self.ultimo[1]
        self.ultimo = (ev.x, ev.y)
        self.R = _rot_y(dx * 0.01) @ _rot_x(dy * 0.01) @ self.R
        self.dibujar(rapido=True)

    def _mover(self, ev):
        if not self.ultimo:
            return
        self.desplazamiento[0] += ev.x - self.ultimo[0]
        self.desplazamiento[1] += ev.y - self.ultimo[1]
        self.ultimo = (ev.x, ev.y)
        self.dibujar(rapido=True)

    def _soltar(self, _):
        self.ultimo = None
        self.dibujar()

    def _rueda(self, ev, sentido=None):
        sentido = sentido if sentido is not None else ev.delta
        self.zoom = min(max(self.zoom * (1.2 if sentido > 0 else 1 / 1.2), 0.3), 10)
        self.dibujar()
