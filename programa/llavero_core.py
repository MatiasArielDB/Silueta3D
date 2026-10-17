"""Silueta3D — núcleo: imagen -> etiqueta para plotter + pieza para impresora 3D.

Imagen (jpg/png/svg) -> etiqueta para imprimir y cortar (SVG/PNG) + base hueca para imprimir en 3D (STL).

Geometría (todo en mm, eje Y hacia arriba):
    etiqueta  L = silueta suavizada (+ margen blanco opcional)       -> línea de corte del plotter
    hueco     K = L + holgura                                         -> donde entra la etiqueta
    exterior  O = K + pared   (+ argolla)                             -> contorno de la pieza 3D
La pieza 3D es O extruido a (piso + profundidad) menos K vaciado desde la altura del piso.

Huecos calados (marcados por el usuario): en la etiqueta se cortan; según el tipo, en la base quedan
como agujero con pared, ventana en el piso o nada (solo papel).
"""
from __future__ import annotations

import argparse
import base64
import io
import math
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import manifold3d as m3d
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy import ndimage
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import polylabel, unary_union

FORMAS = ["Automática (silueta)", "Círculo", "Rectángulo redondeado", "Escudo"]
POSICIONES_ARGOLLA = {
    "Arriba centro": 90,
    "Arriba izquierda": 135,
    "Arriba derecha": 45,
    "Izquierda": 180,
    "Derecha": 0,
    "Abajo": 270,
    "Sin argolla": None,
}
QUAD = 16           # segmentos por cuarto de círculo en los desfases
MIN_CORTE = 0.5     # ancho mínimo de un hueco que el plotter puede recortar en el papel
APOYO_VENTANA = 0.15  # la ventana del piso es un poco más chica que el corte del papel
PASO_Z = 0.04       # alturas de capa en múltiplos de 0.04 (paso completo del eje Z de las Creality)
BOQUILLAS = [0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.8]
DENSIDADES = {  # g/cm³ de los filamentos más comunes
    "PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PLA-CF": 1.30,
}
DENSIDAD_RESINA = 1.10   # epoxi / vidrio líquido, g/cm³ aprox.
ESPESOR_PAPEL = 0.2      # mm
TIPO_PARED = "Agujero con pared"              # agujero vacío con su propia pared, como el borde exterior
TIPO_VENTANA = "Ventana (atraviesa la pieza)"  # papel y piso perforados; la resina forma una ventana
TIPO_PAPEL = "Solo en el papel"               # el papel se recorta; se ve el piso del llavero
TIPOS_HUECO = [TIPO_PARED, TIPO_VENTANA, TIPO_PAPEL]
PIEZA_LLAVERO = "Llavero"                     # con argolla para la cadenita
PIEZA_IMAN = "Imán"                           # hueco redondo atrás para pegar un imán
PIEZA_PIN = "Pin / Snap"                      # cabeza escondida adentro + agujero pasante para el vástago
PIEZA_BASE = "Base (sin argolla ni imán)"
TIPOS_PIEZA = [PIEZA_LLAVERO, PIEZA_IMAN, PIEZA_PIN, PIEZA_BASE]
HOLGURA_Z_IMAN = 0.2   # el hueco del imán es un poco más profundo que el imán
DPI_MINIMO = 150    # por debajo se avisa que la imagen puede salir borrosa


@dataclass
class Parametros:
    alto: float | None = 40.0      # medida del cuerpo del llavero (sin argolla), mm
    ancho: float | None = None
    proporcional: bool = True      # False: con alto y ancho, la imagen se estira a la medida exacta
    forma: str = FORMAS[0]
    tipo_pieza: str = PIEZA_LLAVERO    # argolla, imán o base sola
    argolla: str = "Arriba centro"     # posición de la argolla
    margen: float = 0.0            # borde blanco opcional alrededor del diseño
    boquilla: float = 0.4          # diámetro de la boquilla = ancho de línea en el slicer
    material: str = "PLA"          # para estimar el peso
    capa: float = 0.2              # altura de capa
    lineas_pared: int = 2          # pared = líneas × boquilla
    capas_piso: int = 6            # piso = capas × altura de capa
    capas_hueco: int = 6           # profundidad del hueco = capas × altura de capa (papel + resina)
    holgura: float = 0.3           # por lado, entre etiqueta y pared
    agujero: float = 3.0           # diámetro del agujero de la argolla
    lineas_argolla: int = 5        # pared de la argolla = líneas × boquilla (5 × 0.4 = 2 mm)
    iman_diametro: float = 10.0    # imán de neodimio redondo
    iman_espesor: float = 2.0
    iman_ajuste: float = 0.2       # se suma al diámetro del hueco para que el imán entre
    pin_cabeza: float = 8.0        # diámetro de la cabeza del pin / snap
    pin_cabeza_espesor: float = 1.0
    pin_agujero: float = 1.2       # diámetro del vástago que sale por atrás
    pin_ajuste: float = 0.2        # se suma a los diámetros para que entre sin forzar
    sangrado: float = 0.5          # color impreso que sobrepasa la línea de corte
    suavizado: float = 0.3         # elimina detalles más finos que ~2x este valor
    tolerancia: int = 40           # diferencia de color para considerar "fondo"
    tipo_hueco: str = TIPO_PARED
    dpi: int = 300
    huecos: list = field(default_factory=list)  # puntos (fx, fy) 0..1 sobre la imagen, a calar

    @property
    def pared(self) -> float:
        return round(self.lineas_pared * self.boquilla, 4)

    @property
    def piso(self) -> float:
        return round(self.capas_piso * self.capa, 4)

    @property
    def profundidad(self) -> float:
        return round(self.capas_hueco * self.capa, 4)

    @property
    def argolla_ext(self) -> float:   # diámetro exterior de la argolla
        return round(self.agujero + 2 * self.lineas_argolla * self.boquilla, 4)

    @property
    def techo_iman(self) -> float:    # plástico que queda entre el imán y el hueco de la etiqueta
        return max(3 * self.capa, 0.6)

    @property
    def piso_minimo_iman(self) -> float:
        return self.iman_espesor + HOLGURA_Z_IMAN + self.techo_iman

    @property
    def piso_minimo_pin(self) -> float:   # rebaje de la cabeza + plástico debajo (mismo mínimo que el techo del imán)
        return self.pin_cabeza_espesor + HOLGURA_Z_IMAN + self.techo_iman

    @property
    def piso_minimo(self) -> float:
        """Piso mínimo para el accesorio elegido (0 si no necesita nada)."""
        return {PIEZA_IMAN: self.piso_minimo_iman, PIEZA_PIN: self.piso_minimo_pin}.get(self.tipo_pieza, 0.0)

    @property
    def min_ventana(self) -> float:   # 2 líneas
        return 2 * self.boquilla

    @property
    def min_pasante(self) -> float:   # agujeros más chicos se cierran al imprimir
        return max(1.0, 2.5 * self.boquilla)


def alturas_de_capa(boquilla: float) -> list:
    """Alturas de capa válidas: entre 25 % y 75 % de la boquilla, en múltiplos de PASO_Z."""
    desde = max(1, math.ceil(0.25 * boquilla / PASO_Z - 1e-9))
    hasta = math.floor(0.75 * boquilla / PASO_Z + 1e-9)
    return [round(n * PASO_Z, 2) for n in range(desde, hasta + 1)]


def recomendados(boquilla: float = 0.4) -> "Parametros":
    """Valores recomendados para una boquilla: pared de 2 líneas, piso y hueco de ~1.2 mm, argolla de 4+ líneas."""
    capa = min(alturas_de_capa(boquilla), key=lambda c: abs(c - boquilla / 2))
    p = Parametros(boquilla=boquilla, capa=capa)
    p.capas_piso = p.capas_hueco = max(3, round(1.2 / capa))
    p.lineas_argolla = max(4, round(2.0 / boquilla))   # ~2 mm de pared, nunca menos de 4 líneas
    return p


def validar(p: "Parametros"):
    """Controla que la configuración tenga sentido para la boquilla. Devuelve (errores, avisos)."""
    errores, avisos = [], []
    d = p.boquilla
    if not p.alto and not p.ancho:
        errores.append("Poné el alto o el ancho del llavero (en mm).")
    capas = alturas_de_capa(d)
    if not capas or not (capas[0] - 1e-6 <= p.capa <= capas[-1] + 1e-6):
        errores.append(f"Capa de {p.capa:g} mm no sirve con boquilla de {d:g} mm: usá entre "
                       f"{capas[0]:g} y {capas[-1]:g} mm.")
    if p.lineas_pared < 1:
        errores.append("La pared necesita al menos 1 línea.")
    elif p.lineas_pared == 1:
        avisos.append(f"Pared de 1 línea ({p.pared:g} mm): muy frágil y la resina se puede filtrar. Recomendado: 2.")
    elif p.lineas_pared >= 4:
        avisos.append(f"Pared de {p.lineas_pared} líneas ({p.pared:g} mm): muy gruesa, le quita lugar a la imagen.")
    if p.capas_piso < 1 or p.capas_hueco < 1:
        errores.append("El piso y el hueco necesitan al menos 1 capa.")
    if p.piso < 0.8:
        avisos.append(f"Piso de {p.piso:g} mm: puede quedar flexible o transparentarse. Recomendado: 1 a 1.2 mm.")
    if p.profundidad < 0.8:
        avisos.append(f"Hueco de {p.profundidad:g} mm: poco lugar para papel + resina. Recomendado: 1 a 1.4 mm.")
    elif p.profundidad > 2.4:
        avisos.append(f"Hueco de {p.profundidad:g} mm: más profundo de lo necesario (usa mucha resina).")
    if p.agujero < 2 * d:
        errores.append(f"El agujero de la argolla es muy chico para boquilla de {d:g} mm (mínimo {2 * d:g} mm).")
    if p.tipo_pieza == PIEZA_IMAN and (p.iman_diametro <= 0 or p.iman_espesor <= 0):
        errores.append("Indicá el diámetro y el espesor del imán.")
    if p.tipo_pieza == PIEZA_PIN:
        if p.pin_cabeza <= 0 or p.pin_cabeza_espesor <= 0 or p.pin_agujero <= 0:
            errores.append("Indicá las medidas de la cabeza y del agujero del pin.")
        elif p.pin_agujero >= p.pin_cabeza:
            errores.append("El agujero del pin tiene que ser más chico que la cabeza.")
        elif p.pin_agujero < 2 * d:
            avisos.append(f"Agujero de {p.pin_agujero:g} mm: con boquilla de {d:g} mm puede salir más chico; "
                          "si el vástago no entra, subí el ajuste.")
    if p.piso_minimo and p.piso < p.piso_minimo - 1e-6:
        capas = math.ceil(p.piso_minimo / p.capa - 1e-6)
        que = "el imán" if p.tipo_pieza == PIEZA_IMAN else "la cabeza del pin"
        errores.append(f"Para {que} el piso necesita al menos {capas} capas ({capas * p.capa:g} mm).")
    if p.tipo_pieza == PIEZA_LLAVERO and p.argolla != "Sin argolla":
        if p.lineas_argolla < 2:
            errores.append("La argolla necesita al menos 2 líneas de pared.")
        elif p.lineas_argolla < 4:
            avisos.append(f"Argolla con {p.lineas_argolla} líneas de pared ({p.lineas_argolla * d:g} mm): "
                          "para que no se corte con la cadenita conviene 4 o más.")
    if p.holgura < 0.15:
        avisos.append(f"Holgura de {p.holgura:g} mm: la etiqueta puede no entrar.")
    elif p.holgura > 0.6:
        avisos.append(f"Holgura de {p.holgura:g} mm: va a quedar una ranura visible alrededor de la etiqueta.")
    return errores, avisos


@dataclass
class Imagen:
    rgba: np.ndarray         # imagen de trabajo (lado largo entre 1000 y 2500 px)
    ancho_original: int
    alto_original: int


@dataclass
class Geometria:
    s: float                 # mm por pixel de la imagen de trabajo
    L: Polygon               # etiqueta (corte), con los huecos calados
    K: Polygon               # hueco de la base (las islas son sus agujeros)
    O: Polygon               # exterior sin argolla
    U: Polygon               # exterior final (con argolla, su agujero y los huecos pasantes)
    centro_argolla: tuple | None
    iman: Polygon | None = None      # hueco del imán (atrás)
    pin: tuple | None = None         # (rebaje de la cabeza adentro, agujero pasante)
    sy: float | None = None          # mm por pixel en vertical, si la imagen está estirada (si no, = s)

    @property
    def s_y(self) -> float:
        return self.sy or self.s


@dataclass
class Resultado:
    imagen: Imagen
    geo: Geometria
    impresion: np.ndarray    # RGBA a p.dpi
    caja_impresion: tuple    # (x0, y_arriba, ancho_mm, alto_mm)
    vertices: np.ndarray
    triangulos: np.ndarray
    avisos: list
    p: Parametros

    def medidas(self) -> dict:
        def dims(g):
            x0, y0, x1, y1 = g.bounds
            return x1 - x0, y1 - y0
        return {
            "cuerpo": dims(self.geo.O),
            "total": dims(self.geo.U),
            "etiqueta": dims(self.geo.L),
            "altura": self.p.piso + self.p.profundidad,
        }

    @property
    def volumen(self) -> float:
        """Volumen de la pieza en mm³ (a partir de la malla del STL)."""
        v, t = self.vertices, self.triangulos
        a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
        return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6)

    @property
    def peso(self) -> float:
        """Peso aproximado en gramos: se imprime con relleno 100 %, así que es volumen × densidad."""
        return self.volumen / 1000 * DENSIDADES.get(self.p.material, DENSIDADES["PLA"])

    @property
    def resina_ml(self) -> float:
        """Resina para llenar el hueco hasta el borde (menos lo que ocupa el papel), en ml."""
        return max(0.0, self.geo.K.area * self.p.profundidad - self.geo.L.area * ESPESOR_PAPEL) / 1000

    def mm_a_imagen(self, x, y) -> tuple:
        """Punto en mm -> coordenadas normalizadas (0..1) sobre la imagen."""
        h, w = self.imagen.rgba.shape[:2]
        return x / self.geo.s / w, -y / self.geo.s_y / h

    def imagen_a_mm(self, fx, fy) -> tuple:
        h, w = self.imagen.rgba.shape[:2]
        return fx * w * self.geo.s, -fy * h * self.geo.s_y


# ----------------------------------------------------------------------------- imagen

def cargar_imagen(ruta) -> Imagen:
    """Carga la imagen como RGBA, con el lado largo entre 1000 y 2500 px."""
    ruta = Path(ruta)
    if ruta.suffix.lower() == ".svg":
        import pymupdf
        with pymupdf.open(str(ruta)) as doc:
            pag = doc[0]
            zoom = 2000 / max(pag.rect.width, pag.rect.height)
            pix = pag.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
            arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n).copy()
        if arr.shape[2] == 3:
            arr = np.dstack([arr, np.full(arr.shape[:2], 255, np.uint8)])
        alto0, ancho0 = 10 ** 6, 10 ** 6  # vectorial: resolución ilimitada
    else:
        img = ImageOps.exif_transpose(Image.open(ruta)).convert("RGBA")
        arr = np.asarray(img).copy()
        alto0, ancho0 = arr.shape[:2]

    lado = max(arr.shape[:2])
    if lado > 2500 or lado < 1000:
        f = (2500 if lado > 2500 else 1000) / lado
        interp = cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC
        # se escala con alfa premultiplicado para no generar halos oscuros
        a = arr[..., 3:4].astype(np.float32) / 255
        pre = np.dstack([arr[..., :3] * a, arr[..., 3:4]]).astype(np.float32)
        pre = cv2.resize(pre, (round(arr.shape[1] * f), round(arr.shape[0] * f)), interpolation=interp)
        pre = np.clip(pre, 0, 255)
        a2 = np.maximum(pre[..., 3:4] / 255, 1e-6)
        rgb = np.where(a2 > 1e-3, pre[..., :3] / a2, 255)
        arr = np.dstack([np.clip(rgb, 0, 255), pre[..., 3:4]]).astype(np.uint8)
    return Imagen(arr, ancho0, alto0)


def _pixeles_fondo(rgba: np.ndarray, tolerancia: int):
    """Pixeles que "son fondo" por color: transparentes, o parecidos al color del borde."""
    alfa = rgba[..., 3]
    if (alfa < 128).mean() > 0.01:
        return alfa < 128, "fondo transparente"
    rgb = rgba[..., :3].astype(np.float32)
    borde = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    fondo = np.median(borde, axis=0)
    if (np.linalg.norm(borde - fondo, axis=1) < tolerancia).mean() < 0.6:
        return None, "el borde de la imagen no tiene un fondo uniforme"
    return np.linalg.norm(rgb - fondo, axis=2) < tolerancia, "fondo de color uniforme"


def mascara_figura(rgba: np.ndarray, tolerancia: int, semillas=()):
    """Devuelve (figura, huecos, motivo, avisos).

    figura: máscara del diseño sin fondo y con los huecos interiores rellenos (None si no hay fondo claro).
    huecos: zonas de fondo encerradas que el usuario marcó para calar (cada semilla elige una).
    """
    avisos = []
    h, w = rgba.shape[:2]
    cerca, motivo = _pixeles_fondo(rgba, tolerancia)
    if cerca is None:
        if semillas:
            avisos.append("No se pueden calar huecos en una imagen sin fondo uniforme.")
        return None, None, motivo, avisos
    _, lab = cv2.connectedComponents(cerca.astype(np.uint8), connectivity=4)
    en_borde = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    en_borde = en_borde[en_borde != 0]
    fg = ~np.isin(lab, en_borde)

    # limpieza: ruido de compresión, manchitas sueltas y huecos interiores
    k = max(1, round(max(h, w) / 600))
    nucleo = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
    fg = cv2.morphologyEx(fg.astype(np.uint8), cv2.MORPH_OPEN, nucleo)
    n, lab_fg, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    if n <= 1:
        return None, None, "no se encontró ningún diseño", avisos
    areas = stats[1:, cv2.CC_STAT_AREA]
    conservar = 1 + np.flatnonzero(areas >= max(areas.max() * 0.003, 30))
    fg = ndimage.binary_fill_holes(np.isin(lab_fg, conservar))

    fraccion = fg.mean()
    if fraccion > 0.97:
        return None, None, "el diseño ocupa toda la imagen", avisos
    if fraccion < 0.002:
        return None, None, "el diseño es demasiado chico", avisos

    huecos = np.zeros_like(fg)
    radio = max(3, round(max(h, w) / 150))  # tolera clics que caen sobre una línea del dibujo
    for fx, fy in semillas:
        x, y = int(fx * w), int(fy * h)
        if not (0 <= x < w and 0 <= y < h) or not fg[y, x]:
            continue
        ventana = lab[max(0, y - radio):y + radio + 1, max(0, x - radio):x + radio + 1]
        if lab[y, x] != 0 and lab[y, x] not in en_borde:
            etiqueta = lab[y, x]
        else:
            cand = ventana[(ventana != 0) & ~np.isin(ventana, en_borde)]
            etiqueta = np.bincount(cand).argmax() if cand.size else 0
        if etiqueta == 0:
            avisos.append("En uno de los puntos marcados no hay color de fondo para calar.")
            continue
        huecos |= lab == etiqueta
    return fg, huecos & fg, motivo, avisos


def mascara_a_poligono(fg: np.ndarray):
    """Contornos exteriores de la máscara, en coordenadas de pixel continuas (pixel i = [i, i+1])."""
    m = np.pad(fg.astype(np.uint8), 2)
    contornos, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    polys = []
    for c in contornos:
        pts = c[:, 0, :].astype(float) - 2 + 0.5
        if len(pts) >= 3:
            p = Polygon(pts).buffer(0)
            if p.area > 4:
                polys.append(p.simplify(0.5))
    return unary_union(polys)


def forma_base(forma: str, caja, rodea_logo: bool) -> Polygon:
    """Forma geométrica en pixeles. Si rodea_logo, la forma contiene la caja; si no, entra en ella (foto)."""
    x0, y0, x1, y1 = caja
    bw, bh = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if forma == "Círculo":
        r = math.hypot(bw, bh) / 2 * 1.03 if rodea_logo else min(bw, bh) / 2
        return Point(cx, cy).buffer(r, quad_segs=64)
    if forma == "Escudo":
        if rodea_logo:
            W, H = bw * 1.3, bh * 1.45
            top = y0 - bh * 0.07
        else:
            W, H, top = bw, bh, y0
        t = np.linspace(0, 1, 40)[:, None]
        der = (1 - t) ** 2 * [1, 0.45] + 2 * t * (1 - t) * [1, 0.82] + t ** 2 * [0.5, 1]
        izq = (1 - t) ** 2 * [0.5, 1] + 2 * t * (1 - t) * [0, 0.82] + t ** 2 * [0, 0.45]
        pts = np.vstack([[0, 0], [1, 0], der, izq[1:]])
        pts = pts * [W, H] + [cx - W / 2, top]
        r = min(W, H) * 0.04
        return Polygon(pts).buffer(-r).buffer(r, quad_segs=QUAD)
    # Rectángulo redondeado
    pad = 0.06 * max(bw, bh) if rodea_logo else 0
    rect = box(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    r = 0.12 * min(rect.bounds[2] - rect.bounds[0], rect.bounds[3] - rect.bounds[1])
    return rect.buffer(-r).buffer(r, quad_segs=QUAD)


def silueta_px(rgba: np.ndarray, p: Parametros):
    """Polígono base del llavero en pixeles (con los huecos a calar) y avisos."""
    fg, huecos, motivo, avisos = mascara_figura(rgba, p.tolerancia, p.huecos)
    forma = p.forma
    if forma == FORMAS[0] and fg is None:
        forma = "Rectángulo redondeado"
        avisos.append(f"No se pudo detectar la silueta ({motivo}); se usó «{forma}». "
                      "Podés elegir otra forma o ajustar la tolerancia de fondo.")
    if forma == FORMAS[0]:
        S = mascara_a_poligono(fg)
    elif fg is not None:
        ys, xs = np.nonzero(fg)
        S = forma_base(forma, (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1), rodea_logo=True)
    else:
        h, w = rgba.shape[:2]
        S = forma_base(forma, (0, 0, w, h), rodea_logo=False)
    if huecos is not None and huecos.any():
        S = S.difference(mascara_a_poligono(huecos))
    return S, avisos


# ----------------------------------------------------------------------------- geometría

def _abrir(g, r):
    """Quita las partes más finas que 2r."""
    return g.buffer(-r, quad_segs=QUAD).buffer(r, quad_segs=QUAD)


def _un_poligono(g, avisos: list | None = None) -> Polygon:
    """Une partes sueltas en una sola pieza sin huecos.

    Busca el menor cierre morfológico que conecta todas las partes, pero solo agrega las zonas
    que hacen de puente entre partes distintas: el resto del contorno queda intacto.
    """
    if isinstance(g, Polygon):
        return Polygon(g.exterior)
    partes = list(g.geoms)
    x0, y0, x1, y1 = g.bounds
    r = 0.5
    while r <= max(x1 - x0, y1 - y0):
        if isinstance(g.buffer(r, quad_segs=QUAD).buffer(-r, quad_segs=QUAD), Polygon):
            r *= 1.4  # un poco más ancho para que el puente no quede finito
            extra = g.buffer(r, quad_segs=QUAD).buffer(-r, quad_segs=QUAD).difference(g)
            puentes = [e for e in getattr(extra, "geoms", [extra])
                       if sum(e.buffer(0.05).intersects(pt) for pt in partes) >= 2]
            u = unary_union([g, *(e.buffer(0.02) for e in puentes)])
            if isinstance(u, Polygon):
                if avisos is not None:
                    avisos.append(f"El diseño tenía {len(partes)} partes separadas; se unieron con puentes.")
                return Polygon(u.exterior)
            break
        r *= 1.5
    if avisos is not None:
        avisos.append("El diseño tenía partes muy separadas; se usó el contorno envolvente.")
    return g.convex_hull


def _suavizar(g, r: float):
    if r <= 0:
        return g
    g = g.buffer(r, quad_segs=QUAD).buffer(-r, quad_segs=QUAD)   # rellena entradas finas
    g = _abrir(g, r)                                               # quita puntas finas
    if isinstance(g, MultiPolygon):  # la apertura pudo dejar migas sueltas
        partes = sorted(g.geoms, key=lambda x: x.area, reverse=True)
        g = unary_union([x for x in partes if x.area > max(0.5, partes[0].area * 0.002)])
    return g


def _limpiar(g, d=0.003):
    """Separa puntos donde el contorno se toca a sí mismo y quita vértices casi repetidos.

    Sin esto, al guardar el STL (float32) dos vértices muy cercanos se fusionan y la malla deja de ser cerrada.
    """
    g = g.buffer(-d, quad_segs=2).buffer(d, quad_segs=2).simplify(2 * d)
    if isinstance(g, MultiPolygon):
        g = unary_union([x for x in g.geoms if x.area > 0.01])
    return g


def _ancho_maximo(poly) -> float:
    """Diámetro del mayor círculo que entra en el polígono."""
    poly = max(getattr(poly, "geoms", [poly]), key=lambda g: g.area)
    return 2 * poly.exterior.distance(polylabel(poly, 0.01))


def _argolla(O: Polygon, p: Parametros):
    """Agrega el disco de la argolla (sin agujero) unido con un filete. Devuelve (forma, centro)."""
    angulo = POSICIONES_ARGOLLA.get(p.argolla) if p.tipo_pieza == PIEZA_LLAVERO else None
    if angulo is None:
        return O, None
    R, rh = p.argolla_ext / 2, p.agujero / 2
    objetivo = rh + max(0.8, (R - rh) * 0.5)  # distancia del centro de la argolla al cuerpo
    x0, y0, x1, y1 = O.bounds
    c = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
    d = np.array([math.cos(math.radians(angulo)), math.sin(math.radians(angulo))])
    if angulo % 90:
        # diagonal: el punto del contorno más extremo en esa dirección (la "esquina")
        pts = np.asarray(O.exterior.coords)
        centro = Point(*(pts[np.argmax(pts @ d)] + d * objetivo))
    else:
        # lateral: sobre la línea que pasa por el centro, lo más cerca posible del cuerpo
        dist = lambda t: O.distance(Point(*(c + d * t)))
        paso = 0.25
        t = math.hypot(x1 - x0, y1 - y0) + R + objetivo
        while t > 0 and dist(t) > objetivo:
            t -= paso
        lo, hi = t, t + paso
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if dist(mid) <= objetivo else (lo, mid)
        centro = Point(*(c + d * hi))

    U = O.union(centro.buffer(R, quad_segs=32))
    filete = U.buffer(1.0, quad_segs=QUAD).buffer(-1.0, quad_segs=QUAD).intersection(centro.buffer(R + 3))
    return _un_poligono(U.union(filete)), (centro.x, centro.y)


def _cerrar_bahias(L, K, O, p: Parametros, avisos):
    """Rellena las entradas angostas del contorno donde la pared no puede meterse.

    En una entrada más angosta que dos paredes (ej. entre el brazo y el cuerpo, abierta hacia abajo)
    la pared exterior la llena de plástico macizo y queda una "bahía" dentro del hueco. En esas zonas
    se agranda el hueco hasta la pared y la etiqueta las cubre (con el fondo de la imagen).
    """
    agregado = O.buffer(-p.pared, quad_segs=QUAD).difference(K)   # lo que la pared ocupa de más
    bahias = [g for g in getattr(agregado, "geoms", [agregado])
              if g.area > max(0.5, p.pared ** 2)]                # las esquinas comunes son astillas chicas
    if not bahias:
        return L, K
    relleno = unary_union(bahias).buffer(0.01)
    K2 = _un_poligono(K.union(relleno))
    L2 = _un_poligono(L.union(relleno.buffer(p.holgura, quad_segs=QUAD).intersection(
        K2.buffer(-p.holgura, quad_segs=QUAD))))
    if avisos is not None:
        avisos.append(f"Se cubrieron {len(bahias)} entrada(s) angosta(s) del contorno donde la pared no entra "
                      "(el papel las tapa con el fondo de la imagen).")
    return L2.simplify(0.01), K2.simplify(0.01)


def _hueco_con_pared(corte, p: Parametros):
    """Agujero que atraviesa la pieza, con su pared dentro del hueco (sin tapar el dibujo).

    Devuelve (corte del papel, isla, agujero) o None si la pared no entra.
    """
    isla = corte.buffer(-p.holgura, quad_segs=QUAD)
    pasante = _abrir(isla.buffer(-p.pared, quad_segs=QUAD), p.min_pasante / 2)
    if pasante.is_empty or pasante.area < 0.3:
        return None
    return isla.buffer(p.holgura, quad_segs=QUAD).intersection(corte), isla, pasante


def geometria(S_px, s: float, p: Parametros, avisos: list | None = None, sy: float | None = None) -> Geometria:
    S = _suavizar(affinity.scale(S_px, s, -(sy or s), origin=(0, 0)), p.suavizado)
    huecos = [Polygon(i) for g in getattr(S, "geoms", [S]) for i in g.interiors]

    L_ext = _un_poligono(S, avisos)
    if p.margen > 0:
        L_ext = _un_poligono(L_ext.buffer(p.margen, quad_segs=QUAD))
    L_ext = L_ext.simplify(0.01)
    K_ext = _un_poligono(L_ext.buffer(p.holgura, quad_segs=QUAD)).simplify(0.01)
    O_ext = _un_poligono(K_ext.buffer(p.pared, quad_segs=QUAD)).simplify(0.01)
    L_ext, K_ext = _cerrar_bahias(L_ext, K_ext, O_ext, p, avisos)

    cortes, islas, pasantes, ventanas = [], [], [], []
    for h in huecos:
        if p.margen > 0:
            h = h.buffer(-p.margen, quad_segs=QUAD)
        corte = _abrir(h, MIN_CORTE / 2)  # sin las puntas que el plotter no puede cortar
        if corte.is_empty or corte.area < 0.2:
            if avisos is not None and not h.is_empty:
                avisos.append(f"Un hueco marcado es demasiado angosto para cortarlo a este tamaño "
                              f"({_ancho_maximo(h):.1f} mm); quedó relleno.")
            continue
        ancho = _ancho_maximo(corte)
        tipo = p.tipo_hueco
        if tipo == TIPO_PARED:
            hecho = _hueco_con_pared(corte, p)
            if hecho:
                corte_final, isla, pasante = hecho
                islas.append(isla)
                pasantes.append(pasante)
                cortes.append(corte_final)
                continue
            tipo = TIPO_PAPEL
            if avisos is not None:
                necesario = p.min_pasante + 2 * (p.holgura + p.pared)
                # se agranda el hueco hasta que la pared entre, para decir desde qué tamaño se puede
                f = 1.0
                while f < 20 and not _hueco_con_pared(affinity.scale(corte, f, f, origin="centroid"), p):
                    f *= 1.05
                k = 2 * (p.margen + p.holgura + p.pared)
                medida, nombre = (p.alto, "alto") if p.alto else (p.ancho, "ancho")
                desde = math.ceil(((medida - k) * f + k) / 5) * 5
                avisos.append(f"Un hueco mide {ancho:.1f} mm y para tener pared sin tapar el dibujo necesita "
                              f"más de {necesario:.1f} mm; quedó recortado solo en el papel. Con este diseño la "
                              f"pared entra a partir de unos {desde:.0f} mm de {nombre}.")
        if tipo == TIPO_VENTANA:
            ventana = _abrir(corte.buffer(-APOYO_VENTANA, quad_segs=QUAD), p.min_ventana / 2)
            if not ventana.is_empty and ventana.area > 0.2:
                ventanas.append(ventana)
            elif avisos is not None:
                avisos.append(f"Un hueco mide {ancho:.1f} mm, muy angosto para perforar el piso "
                              f"(necesita más de {p.min_ventana + 2 * APOYO_VENTANA:.1f} mm); se recortó solo en el papel.")
        cortes.append(corte)
    if ventanas and avisos is not None:
        avisos.append("Hueco tipo ventana: al echar la resina, pegá cinta de embalar debajo del llavero "
                      "para que no se escape por el hueco; al curar queda una ventana transparente.")

    L = L_ext.difference(unary_union(cortes)) if cortes else L_ext
    K = K_ext.difference(unary_union(islas)) if islas else K_ext
    O = O_ext.difference(unary_union(pasantes)) if pasantes else O_ext
    U, centro = _argolla(O_ext, p)
    restar = pasantes + ventanas
    if centro:
        restar.append(Point(centro).buffer(p.agujero / 2, quad_segs=32))
    if restar:
        U = U.difference(unary_union(restar))
    iman = pin = None
    if p.tipo_pieza == PIEZA_IMAN:
        iman = _lugar_circular(O_ext, pasantes + ventanas, p, p.iman_diametro, p.iman_ajuste, "El imán")
    elif p.tipo_pieza == PIEZA_PIN:
        # la cabeza va adentro, en el piso del hueco de la etiqueta (queda tapada por el papel)
        cabeza = _lugar_circular(K, ventanas, p, p.pin_cabeza, p.pin_ajuste, "La cabeza del pin")
        centro_pin = cabeza.centroid
        pin = (cabeza, centro_pin.buffer((p.pin_agujero + p.pin_ajuste) / 2, quad_segs=24))
    return Geometria(s, _limpiar(L), _limpiar(K), _limpiar(O), _limpiar(U), centro, iman, pin, sy)


def _lugar_circular(zona, agujeros, p: Parametros, diametro, ajuste, nombre):
    """Hueco redondo en el punto más alejado de los bordes (y de los agujeros), con al menos una pared alrededor."""
    radio = (diametro + ajuste) / 2
    zona = zona.buffer(-p.pared, quad_segs=QUAD)
    if agujeros:
        zona = zona.difference(unary_union(agujeros).buffer(p.pared, quad_segs=QUAD))
    if zona.is_empty:
        raise ValueError(f"{nombre} no entra: la pieza es demasiado chica.")
    zona = max(getattr(zona, "geoms", [zona]), key=lambda g: g.area)
    centro = polylabel(zona, 0.01)
    disponible = zona.exterior.distance(centro)
    if zona.interiors:
        disponible = min(disponible, min(Polygon(i).exterior.distance(centro) for i in zona.interiors))
    if disponible < radio:
        raise ValueError(f"{nombre} de {diametro:g} mm no entra en esta pieza: entra hasta "
                         f"{2 * disponible - ajuste:.1f} mm de diámetro (o agrandá la pieza).")
    return centro.buffer(radio, quad_segs=48)


def geometria_a_medida(S_px, p: Parametros, avisos: list) -> Geometria:
    """Busca la escala para que el cuerpo (sin argolla) mida lo pedido. Con alto y ancho, entra en ambos."""
    if not p.alto and not p.ancho:
        raise ValueError("Indicá el alto o el ancho del llavero.")
    k = 2 * (p.margen + p.holgura + p.pared)
    for v in (p.alto, p.ancho):
        if v and v < k + 8:
            raise ValueError(f"La medida es muy chica: tiene que ser mayor a {k + 8:.1f} mm.")
    x0, y0, x1, y1 = S_px.bounds
    if p.alto and p.ancho and not p.proporcional:
        # medida exacta en las dos direcciones: la imagen se estira
        sx, sy = (p.ancho - k) / (x1 - x0), (p.alto - k) / (y1 - y0)
        for _ in range(6):
            gx0, gy0, gx1, gy1 = geometria(S_px, sx, p, sy=sy).O.bounds
            fx = (p.ancho - k) / max(gx1 - gx0 - k, 1e-6)
            fy = (p.alto - k) / max(gy1 - gy0 - k, 1e-6)
            if abs(fx - 1) < 1e-4 and abs(fy - 1) < 1e-4:
                break
            sx, sy = sx * fx, sy * fy
        return geometria(S_px, sx, p, avisos, sy=sy)
    factores = []
    if p.alto:
        factores.append((p.alto - k) / (y1 - y0))
    if p.ancho:
        factores.append((p.ancho - k) / (x1 - x0))
    s = min(factores)
    for _ in range(6):
        geo = geometria(S_px, s, p)
        gx0, gy0, gx1, gy1 = geo.O.bounds
        f = []
        if p.alto:
            f.append((p.alto - k) / max(gy1 - gy0 - k, 1e-6))
        if p.ancho:
            f.append((p.ancho - k) / max(gx1 - gx0 - k, 1e-6))
        f = min(f)
        if abs(f - 1) < 1e-4:
            break
        s *= f
    return geometria(S_px, s, p, avisos)


# ----------------------------------------------------------------------------- 3D

def _cross_section(poly) -> m3d.CrossSection:
    contornos = []
    for g in getattr(poly, "geoms", [poly]):
        g = orient(g, 1.0)
        contornos.append(np.asarray(g.exterior.coords)[:-1])
        contornos.extend(np.asarray(i.coords)[:-1] for i in g.interiors)
    return m3d.CrossSection(contornos, m3d.FillRule.EvenOdd)


def malla_3d(geo: Geometria, p: Parametros):
    altura = p.piso + p.profundidad
    cuerpo = m3d.Manifold.extrude(_cross_section(geo.U), altura)
    hueco = m3d.Manifold.extrude(_cross_section(geo.K), p.profundidad + 1).translate((0, 0, p.piso))
    pieza = cuerpo - hueco
    if geo.iman is not None:
        prof = p.iman_espesor + HOLGURA_Z_IMAN
        pieza = pieza - m3d.Manifold.extrude(_cross_section(geo.iman), prof + 1).translate((0, 0, -1))
    if geo.pin is not None:
        cabeza, agujero = geo.pin
        prof = p.pin_cabeza_espesor + HOLGURA_Z_IMAN
        pieza = pieza - m3d.Manifold.extrude(_cross_section(cabeza), prof + 1).translate((0, 0, p.piso - prof))
        pieza = pieza - m3d.Manifold.extrude(_cross_section(agujero), p.piso + 2).translate((0, 0, -1))
    x0, y0, _, _ = geo.U.bounds
    pieza = pieza.translate((-x0, -y0, 0))
    mesh = pieza.to_mesh()
    # copias: no hay que quedarse con referencias a la memoria interna de manifold
    return np.array(mesh.vert_properties, dtype=np.float64)[:, :3].copy(), np.array(mesh.tri_verts, dtype=np.int64)


def guardar_stl(ruta, v: np.ndarray, t: np.ndarray):
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    n = np.cross(b - a, c - a)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    datos = np.zeros(len(t), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    datos["n"] = n
    datos["v"] = np.stack([a, b, c], axis=1)
    with open(ruta, "wb") as f:
        f.write(b"Silueta3D".ljust(80, b" "))
        f.write(np.uint32(len(t)).tobytes())
        f.write(datos.tobytes())


# ----------------------------------------------------------------------------- impresión

def _a_canvas(coords, x0, y_arriba, mmpp):
    pts = np.asarray(coords)
    return np.column_stack([(pts[:, 0] - x0) / mmpp, (y_arriba - pts[:, 1]) / mmpp])


def _rellenar(mask, poly, x0, y_arriba, mmpp, aa=False):
    tipo = cv2.LINE_AA if aa else cv2.LINE_8
    a_int = lambda anillo: np.round((_a_canvas(anillo.coords, x0, y_arriba, mmpp) - 0.5) * 16).astype(np.int32)
    for g in getattr(poly, "geoms", [poly]):
        cv2.fillPoly(mask, [a_int(g.exterior)], 255, lineType=tipo, shift=4)
        for i in g.interiors:
            cv2.fillPoly(mask, [a_int(i)], 0, lineType=tipo, shift=4)
    return mask


def _extension_suave(img: np.ndarray, fuente: np.ndarray, sigma=3.0):
    """Extiende los colores de `fuente` hacia afuera, promediados (sin vetas)."""
    m = fuente.astype(np.float32)
    f = img.astype(np.float32)
    w = cv2.GaussianBlur(m, (0, 0), sigma)
    wseg = np.maximum(w, 1e-6)
    color = cv2.GaussianBlur(f * m[..., None], (0, 0), sigma) / wseg[..., None]
    ok = w > 0.05
    if ok.any() and not ok.all():
        idx = ndimage.distance_transform_edt(~ok, return_distances=False, return_indices=True)
        color = color[idx[0], idx[1]]
    return color


def imagen_impresion(rgba: np.ndarray, geo: Geometria, p: Parametros):
    """PNG RGBA a p.dpi: la etiqueta más el sangrado alrededor del corte.

    En el sangrado se usa la imagen original tal cual (nítida). Solo donde no hay imagen
    (fondo transparente o fuera del lienzo) se extiende suavemente el color del borde.
    """
    mmpp = 25.4 / p.dpi
    x0, y0, x1, y1 = geo.L.buffer(p.sangrado).bounds
    x0, y0, x1, y1 = x0 - 1, y0 - 1, x1 + 1, y1 + 1
    W, H = math.ceil((x1 - x0) / mmpp), math.ceil((y1 - y0) / mmpp)
    y_arriba = y1

    # fondo transparente -> papel blanco
    a = rgba[..., 3:4].astype(np.float32) / 255
    plano = (rgba[..., :3] * a + 255 * (1 - a)).astype(np.uint8)

    # escalar la imagen a la resolución de impresión y ubicarla en el lienzo
    h, w = plano.shape[:2]
    sx, sy = geo.s, geo.s_y
    nw, nh = max(1, round(w * sx / mmpp)), max(1, round(h * sy / mmpp))
    interp = cv2.INTER_AREA if min(sx, sy) < mmpp else cv2.INTER_CUBIC
    fx, fy = nw / w, nh / h
    M = np.array([[mmpp * fx / sx, 0, (x0 + 0.5 * mmpp) * fx / sx - 0.5],
                  [0, mmpp * fy / sy, (-y_arriba + 0.5 * mmpp) * fy / sy - 0.5]])

    def ubicar(arr, interp_redim, interp_warp, borde, valor=0):
        arr = cv2.resize(arr, (nw, nh), interpolation=interp_redim)
        return cv2.warpAffine(arr, M, (W, H), flags=interp_warp | cv2.WARP_INVERSE_MAP,
                              borderMode=borde, borderValue=valor)

    lienzo = ubicar(plano, interp, cv2.INTER_LINEAR, cv2.BORDER_REPLICATE)
    valido = ubicar(np.full((h, w), 255, np.uint8), cv2.INTER_NEAREST, cv2.INTER_NEAREST,
                    cv2.BORDER_CONSTANT) > 0
    opaco = (ubicar(rgba[..., 3].copy(), cv2.INTER_LINEAR, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT) >= 128) & valido

    etiqueta = _rellenar(np.zeros((H, W), np.uint8), geo.L, x0, y_arriba, mmpp)
    interior = (cv2.erode(etiqueta, np.ones((5, 5), np.uint8)) > 0) & valido
    sin_imagen = ~interior & ~opaco
    if interior.any() and sin_imagen.any():
        extendido = _extension_suave(lienzo, interior & opaco if (interior & opaco).any() else interior)
        lienzo = np.where(sin_imagen[..., None], np.clip(extendido, 0, 255), lienzo).astype(np.uint8)
    alfa = _rellenar(np.zeros((H, W), np.uint8), geo.L.buffer(p.sangrado, quad_segs=QUAD),
                     x0, y_arriba, mmpp, aa=True)
    return np.dstack([lienzo, alfa]), (x0, y_arriba, W * mmpp, H * mmpp)


# ----------------------------------------------------------------------------- SVG

def _path(poly, x0, y_arriba) -> str:
    partes = []
    for g in getattr(poly, "geoms", [poly]):
        for anillo in [g.exterior, *g.interiors]:
            pts = np.asarray(anillo.coords)[:-1]
            xy = " ".join(f"{x - x0:.3f},{y_arriba - y:.3f}" for x, y in pts)
            partes.append(f"M {xy} Z")
    return " ".join(partes)


def _svg(ancho, alto, cuerpo) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
            f'width="{ancho:.3f}mm" height="{alto:.3f}mm" viewBox="0 0 {ancho:.3f} {alto:.3f}">\n'
            f'{cuerpo}</svg>\n')


def _capa(id_, nombre, contenido) -> str:
    return f'<g id="{id_}" inkscape:groupmode="layer" inkscape:label="{nombre}">\n{contenido}\n</g>\n'


def svgs(res: Resultado) -> dict:
    geo = res.geo
    x0, y_arriba, W, H = res.caja_impresion
    corte = _capa("corte", "Corte",
                  f'<path d="{_path(geo.L, x0, y_arriba)}" fill="none" stroke="#FF0000" stroke-width="0.1"/>')
    buf = io.BytesIO()
    Image.fromarray(res.impresion).save(buf, "PNG", dpi=(res.p.dpi, res.p.dpi))
    b64 = base64.b64encode(buf.getvalue()).decode()
    impresion = _capa("impresion", "Impresion",
                      f'<image x="0" y="0" width="{W:.3f}" height="{H:.3f}" preserveAspectRatio="none" '
                      f'xlink:href="data:image/png;base64,{b64}"/>')

    bx0, by0, bx1, by1 = geo.U.bounds
    bx0, by0, bx1, by1 = bx0 - 2, by0 - 2, bx1 + 2, by1 + 2
    base = (f'<path d="{_path(geo.U, bx0, by1)}" fill="none" stroke="#000000" stroke-width="0.15" fill-rule="evenodd"/>\n'
            f'<path d="{_path(geo.K, bx0, by1)}" fill="none" stroke="#0070C0" stroke-width="0.15"/>\n'
            f'<path d="{_path(geo.L, bx0, by1)}" fill="none" stroke="#00A050" stroke-width="0.1" stroke-dasharray="0.6,0.4"/>')
    return {
        "imprimir": _svg(W, H, impresion + corte),
        "corte": _svg(W, H, corte),
        "base3D": _svg(bx1 - bx0, by1 - by0,
                       _capa("exterior", "Exterior (negro) - Hueco (azul) - Etiqueta (verde)", base)),
    }


# ----------------------------------------------------------------------------- vista previa

def _fuente(tam):
    # Windows, macOS y Linux; si no encuentra ninguna, la fuente propia de Pillow (escalable)
    for nombre in ("segoeui.ttf", "arial.ttf", "Arial.ttf", "Helvetica.ttc",
                   "/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(nombre, tam)
        except OSError:
            pass
    try:
        return ImageFont.load_default(tam)
    except TypeError:                    # Pillow anterior a 10.1
        return ImageFont.load_default()


def vista_previa(res: Resultado, alto_px: int = 420, con_mapa: bool = False):
    """Dos paneles: etiqueta (con línea de corte) y base 3D vista desde arriba, a la misma escala.

    Con con_mapa=True devuelve también cómo pasar de pixel de la vista a mm:
    x = (px - cx) / esc, y = (cy - py) / esc, restando ancho_panel a px en el panel derecho.
    """
    ss = 2 if alto_px < 900 else 1  # supermuestreo para suavizar (en alta resolución no hace falta)
    k = ss * alto_px / 420           # grosores y textos proporcionales al tamaño
    geo = res.geo
    bx0, by0, bx1, by1 = geo.U.bounds
    margen = 18 * k
    pie = 30 * k
    panel_h = alto_px * ss
    esc = (panel_h - 2 * margen - pie) / max(by1 - by0, (bx1 - bx0) * 0.9)
    panel_w = max(int((bx1 - bx0) * esc + 2 * margen), int(panel_h * 0.75))
    cx0 = panel_w / 2 - (bx0 + bx1) / 2 * esc
    cy = (panel_h - pie) / 2 + (by0 + by1) / 2 * esc
    img = Image.new("RGB", (panel_w * 2, panel_h), "white")
    d = ImageDraw.Draw(img)

    def a_px(coords, ox):
        return [(ox + cx0 + x * esc, cy - y * esc) for x, y in coords]

    def poligono(poly, ox, relleno, relleno_huecos):
        for g in getattr(poly, "geoms", [poly]):
            d.polygon(a_px(g.exterior.coords, ox), fill=relleno)
            for i in g.interiors:
                d.polygon(a_px(i.coords, ox), fill=relleno_huecos)

    def contorno(poly, ox, color, ancho):
        for g in getattr(poly, "geoms", [poly]):
            for anillo in [g.exterior, *g.interiors]:
                d.line(a_px(anillo.coords, ox), fill=color, width=ancho)

    # panel 1: etiqueta impresa
    tam = max(4, round(12 * k))
    for yy in range(0, panel_h, tam):
        for xx in range(0, panel_w, tam):
            if (xx // tam + yy // tam) % 2:
                d.rectangle([xx, yy, xx + tam, yy + tam], fill=(235, 235, 235))
    # la etiqueta como queda después de cortarla (el sangrado se descarta con el recorte)
    x0, y_arriba, W, H = res.caja_impresion
    tl = a_px([(x0, y_arriba)], 0)[0]
    imp = Image.fromarray(res.impresion).resize((max(1, round(W * esc)), max(1, round(H * esc))), Image.LANCZOS)
    capa = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
    capa.paste(imp, (round(tl[0]), round(tl[1])))
    recorte = Image.new("L", (panel_w, panel_h), 0)
    dr = ImageDraw.Draw(recorte)
    for g in getattr(geo.L, "geoms", [geo.L]):
        dr.polygon(a_px(g.exterior.coords, 0), fill=255)
        for i in g.interiors:
            dr.polygon(a_px(i.coords, 0), fill=0)
    img.paste(capa, (0, 0), recorte)
    contorno(geo.L, 0, (230, 0, 0), max(1, round(k)))
    for fx, fy in res.p.huecos:  # puntos marcados para calar
        px, py = a_px([res.imagen_a_mm(fx, fy)], 0)[0]
        r = 5 * k
        d.ellipse([px - r, py - r, px + r, py + r], outline=(0, 160, 255), width=max(1, round(2 * k)))

    # panel 2: base 3D vista desde arriba (blanco = agujeros que atraviesan la pieza)
    azul, celeste = (70, 110, 170), (200, 215, 235)
    poligono(geo.U, panel_w, azul, azul)
    poligono(geo.K, panel_w, celeste, azul)
    for g in getattr(geo.U, "geoms", [geo.U]):
        for i in g.interiors:
            d.polygon(a_px(i.coords, panel_w), fill="white")
    contorno(geo.L, panel_w, (0, 150, 80), max(1, round(k / 2)))
    for circulo in ([geo.iman] if geo.iman is not None else []) + list(geo.pin or []):
        contorno(circulo, panel_w, (230, 120, 0), max(1, round(k)))
    d.line([(panel_w, 0), (panel_w, panel_h)], fill=(200, 200, 200), width=max(1, round(k)))

    m = res.medidas()
    f = _fuente(round(11 * k))
    d.text((panel_w / 2, panel_h - 18 * k),
           f"Etiqueta {m['etiqueta'][0]:.1f} × {m['etiqueta'][1]:.1f} mm", fill="black", font=f, anchor="mm")
    d.text((panel_w * 1.5, panel_h - 18 * k),
           f"Cuerpo {m['cuerpo'][0]:.1f} × {m['cuerpo'][1]:.1f} mm · alto {m['altura']:.1f} mm",
           fill="black", font=f, anchor="mm")
    if ss > 1:
        img = img.resize((img.width // ss, img.height // ss), Image.LANCZOS)
    if con_mapa:
        return img, {"esc": esc / ss, "cx": cx0 / ss, "cy": cy / ss, "ancho_panel": panel_w / ss}
    return img


# ----------------------------------------------------------------------------- flujo completo

def procesar(entrada, p: Parametros) -> Resultado:
    imagen = entrada if isinstance(entrada, Imagen) else cargar_imagen(entrada)
    errores, avisos_config = validar(p)
    if errores:
        raise ValueError("\n".join(errores))
    S_px, avisos = silueta_px(imagen.rgba, p)
    avisos = avisos_config + avisos
    geo = geometria_a_medida(S_px, p, avisos)
    impresion, caja = imagen_impresion(imagen.rgba, geo, p)
    v, t = malla_3d(geo, p)

    alto_trabajo = imagen.rgba.shape[0]
    dpi = imagen.alto_original / alto_trabajo / max(geo.s, geo.s_y) * 25.4
    if dpi < DPI_MINIMO:
        avisos.append(f"La imagen tiene poca resolución para este tamaño (~{dpi:.0f} dpi); "
                      "puede imprimirse algo borrosa. Conviene una imagen más grande.")
    return Resultado(imagen, geo, impresion, caja, v, t, avisos, p)


def nombre_seguro(nombre: str) -> str:
    nombre = re.sub(r'[<>:"/\\|?*]+', "_", nombre.strip()).strip(" .")
    return nombre or "llavero"


def guardar(res: Resultado, ruta_original, carpeta_salida, nombre: str) -> Path:
    nombre = nombre_seguro(nombre)
    carpeta = Path(carpeta_salida) / nombre
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_original = Path(ruta_original)
    shutil.copy2(ruta_original, carpeta / f"{nombre}_original{ruta_original.suffix.lower()}")

    for clave, texto in svgs(res).items():
        (carpeta / f"{nombre}_{clave}.svg").write_text(texto, encoding="utf-8")
    Image.fromarray(res.impresion).save(carpeta / f"{nombre}_imprimir.png", dpi=(res.p.dpi, res.p.dpi))
    guardar_stl(carpeta / f"{nombre}_stl.stl", res.vertices, res.triangulos)
    vista_previa(res).save(carpeta / f"{nombre}_vista_previa.png")

    m = res.medidas()
    lineas = [
        f"Proyecto: {nombre}  (Silueta3D)",
        f"Imagen: {ruta_original.name}",
        "",
        "MEDIDAS",
        f"  Tipo de pieza:        {res.p.tipo_pieza}",
        f"  Cuerpo (sin argolla): {m['cuerpo'][0]:.2f} x {m['cuerpo'][1]:.2f} mm",
        f"  Total (con argolla):  {m['total'][0]:.2f} x {m['total'][1]:.2f} mm",
        f"  Etiqueta (corte):     {m['etiqueta'][0]:.2f} x {m['etiqueta'][1]:.2f} mm",
        f"  Altura de la pieza:   {m['altura']:.2f} mm",
        f"  Peso aproximado:      {res.peso:.1f} g de {res.p.material} (relleno 100 %)",
        f"  Resina:               ~{res.resina_ml:.2f} ml ({res.resina_ml * DENSIDAD_RESINA:.1f} g) hasta el borde",
        "",
        "PARAMETROS",
        *[f"  {k}: {v}" for k, v in asdict(res.p).items()],
        f"  -> pared {res.p.pared:g} mm, piso {res.p.piso:g} mm, hueco {res.p.profundidad:g} mm",
        "",
        "ARCHIVOS",
        f"  {nombre}_imprimir.svg  imagen + linea de corte (capa 'Corte', roja) para SignMaster",
        f"  {nombre}_imprimir.png  imagen a {res.p.dpi} dpi, mismo tamaño que el SVG",
        f"  {nombre}_corte.svg     solo la linea de corte, mismo lienzo que el PNG",
        f"  {nombre}_base3D.svg    contornos de la base: exterior (negro), hueco (azul), etiqueta (verde)",
        f"  {nombre}_stl.stl       pieza para imprimir en 3D (mm)",
        "",
        f"IMPRESION 3D (boquilla {res.p.boquilla:g} mm)",
        f"  Capa {res.p.capa:g} mm, ancho de linea {res.p.boquilla:g} mm, {res.p.lineas_pared} paredes,",
        "  piso hacia abajo, relleno 100%, sin soportes.",
    ]
    if res.geo.iman is not None:
        lineas += ["", "IMAN",
                   f"  Hueco atras: {res.p.iman_diametro + res.p.iman_ajuste:g} mm de diametro x "
                   f"{res.p.iman_espesor + HOLGURA_Z_IMAN:g} mm de profundidad, para un iman de "
                   f"{res.p.iman_diametro:g} x {res.p.iman_espesor:g} mm.",
                   "  Pegarlo con una gota de pegamento (cianoacrilato o epoxi)."]
    if res.geo.pin is not None:
        q = res.p
        lineas += ["", "PIN / SNAP",
                   f"  Rebaje adentro (debajo de la etiqueta): {q.pin_cabeza + q.pin_ajuste:g} mm de diametro x "
                   f"{q.pin_cabeza_espesor + HOLGURA_Z_IMAN:g} mm, para una cabeza de {q.pin_cabeza:g} x "
                   f"{q.pin_cabeza_espesor:g} mm.",
                   f"  Agujero pasante hacia atras: {q.pin_agujero + q.pin_ajuste:g} mm de diametro.",
                   "  Orden: pasar el pin desde adentro (con una gota de pegamento en la cabeza), pegar la",
                   "  etiqueta encima y despues la resina."]
    if res.avisos:
        lineas += ["", "AVISOS", *[f"  - {a}" for a in res.avisos]]
    (carpeta / f"{nombre}_parametros.txt").write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return carpeta


def main():
    ap = argparse.ArgumentParser(description="Silueta3D: genera la etiqueta (SVG/PNG) y la pieza (STL).")
    ap.add_argument("imagen")
    ap.add_argument("--alto", type=float)
    ap.add_argument("--ancho", type=float)
    ap.add_argument("--nombre")
    ap.add_argument("--forma", default=FORMAS[0], choices=FORMAS)
    ap.add_argument("--argolla", default="Arriba centro", choices=list(POSICIONES_ARGOLLA))
    ap.add_argument("--tipo-pieza", default=PIEZA_LLAVERO, choices=TIPOS_PIEZA)
    ap.add_argument("--iman", default="10x2", help="imán DIAMETROxESPESOR en mm (con --tipo-pieza Imán)")
    ap.add_argument("--pin", default="8x1x1.2", help="pin CABEZAxESPESORxAGUJERO en mm (con --tipo-pieza 'Pin / Snap')")
    ap.add_argument("--margen", type=float, default=0.0)
    ap.add_argument("--tipo-hueco", default=TIPO_PARED, choices=TIPOS_HUECO)
    ap.add_argument("--boquilla", type=float, default=0.4)
    ap.add_argument("--lineas-pared", type=int, default=2)
    ap.add_argument("--hueco", action="append", default=[], metavar="FX,FY",
                    help="punto a calar, en fracción de la imagen (ej. 0.2,0.65); se puede repetir")
    ap.add_argument("--salida", default=str(Path(__file__).resolve().parent.parent / "salida"))
    a = ap.parse_args()
    huecos = [tuple(float(v) for v in h.split(",")) for h in a.hueco]
    d_iman, e_iman = (float(v) for v in a.iman.lower().split("x"))
    c_pin, e_pin, a_pin = (float(v) for v in a.pin.lower().split("x"))
    p = Parametros(alto=a.alto, ancho=a.ancho, forma=a.forma, argolla=a.argolla, tipo_pieza=a.tipo_pieza,
                   iman_diametro=d_iman, iman_espesor=e_iman, pin_cabeza=c_pin, pin_cabeza_espesor=e_pin,
                   pin_agujero=a_pin, margen=a.margen, huecos=huecos,
                   tipo_hueco=a.tipo_hueco)
    r = recomendados(a.boquilla)
    p.boquilla, p.capa, p.capas_piso, p.capas_hueco = r.boquilla, r.capa, r.capas_piso, r.capas_hueco
    p.lineas_argolla, p.lineas_pared = r.lineas_argolla, a.lineas_pared
    if p.piso_minimo:
        p.capas_piso = max(p.capas_piso, math.ceil(p.piso_minimo / p.capa - 1e-6))
    sys.stdout.reconfigure(encoding="utf-8")
    res = procesar(a.imagen, p)
    carpeta = guardar(res, a.imagen, a.salida, a.nombre or Path(a.imagen).stem)
    for av in res.avisos:
        print("AVISO:", av)
    print("Listo:", carpeta)


if __name__ == "__main__":
    main()
