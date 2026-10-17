"""Silueta3D — ventana: elegir imagen, poner la medida, ver la pieza en vivo y crear los archivos."""
from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import asdict, fields
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from PIL import Image, ImageTk
from shapely.geometry import Point, Polygon

import llavero_core as core
from visor3d import Visor3D

AQUI = Path(__file__).resolve().parent
SALIDA = AQUI.parent / "salida"
CONFIG = AQUI / "config.json"
ALTO_VISTA = 1400   # resolución de la vista previa (alta, para poder hacer zoom)
ANCHO_UNIDAD = 30   # ancho fijo (caracteres) del texto de unidades: si cambia, se reacomoda toda la ventana
ESPERA_VIVO = 350   # ms sin cambios antes de actualizar la vista en vivo
ZOOM_MAX = 12
ES_MAC = sys.platform == "darwin"
ATAJO = "Cmd" if ES_MAC else "Ctrl"   # tecla de los atajos (deshacer / rehacer)

CAMPOS = {f.name for f in fields(core.Parametros)} - {"huecos"}
TEXTOS = {"forma", "argolla", "tipo_hueco", "tipo_pieza", "material"}
BOOLEANOS = {"proporcional"}
DESCRIPCION_TIPO = {
    core.PIEZA_LLAVERO: "Con argolla para la cadenita.",
    core.PIEZA_IMAN: "Hueco atrás para pegar un imán (heladera, etc.).",
    core.PIEZA_PIN: "Cabeza escondida adentro y vástago por atrás.",
    core.PIEZA_BASE: "La pieza sola, sin argolla ni imán.",
}
POSICIONES = [k for k in core.POSICIONES_ARGOLLA if k != "Sin argolla"]
ENTEROS = {"lineas_pared", "capas_piso", "capas_hueco", "lineas_argolla", "tolerancia", "dpi"}
NOMBRES = {"pin_cabeza": "Cabeza del pin", "pin_cabeza_espesor": "Espesor de la cabeza",
           "pin_agujero": "Agujero del pin", "pin_ajuste": "Ajuste del pin",
           "iman_diametro": "Diámetro del imán", "iman_espesor": "Espesor del imán", "iman_ajuste": "Ajuste del imán",
           "alto": "Alto", "ancho": "Ancho", "margen": "Margen blanco", "boquilla": "Boquilla", "capa": "Capa",
           "lineas_pared": "Pared", "capas_piso": "Piso", "capas_hueco": "Profundidad del hueco",
           "holgura": "Holgura", "agujero": "Agujero argolla", "lineas_argolla": "Pared argolla",
           "sangrado": "Sangrado", "suavizado": "Suavizado", "tolerancia": "Tolerancia de fondo", "dpi": "dpi"}
PISTA_ETIQUETA = "Rueda: zoom · arrastrar: mover"
AYUDA_TIPOS = ("Agujero con pared: atraviesa la pieza y queda vacío, con su pared dentro del hueco (no tapa el "
               "dibujo). Si el hueco es muy angosto para la pared, queda recortado solo en el papel y te avisa "
               "a partir de qué tamaño entraría.\n"
               "Ventana: se perfora el papel y el piso, sin pared; al echar la resina se pega cinta debajo "
               "y queda una ventana transparente.\nSolo en el papel: se ve el piso del llavero.")


def a_numero(texto: str, entero=False):
    texto = texto.strip().replace(",", ".")
    if not texto:
        return None
    return int(float(texto)) if entero else float(texto)


class Ayuda:
    """Globito de ayuda al dejar el mouse quieto sobre un control.

    Aparece con demora, se va al salir, al hacer clic, con la rueda, al mover/minimizar la ventana
    y solo a los pocos segundos. Nunca hay más de uno abierto.
    """
    actual = None

    def __init__(self, widget, texto):
        self.widget, self.texto = widget, texto
        self.ventana = self._mostrar_id = self._cerrar_id = None
        widget.bind("<Enter>", self._programar, add="+")
        for evento in ("<Leave>", "<ButtonPress>", "<MouseWheel>", "<Destroy>", "<KeyPress>"):
            widget.bind(evento, self.ocultar, add="+")

    def _programar(self, _=None):
        self._cancelar()
        self._mostrar_id = self.widget.after(500, self.mostrar)

    def _cancelar(self):
        for nombre in ("_mostrar_id", "_cerrar_id"):
            ident = getattr(self, nombre)
            if ident:
                try:
                    self.widget.after_cancel(ident)
                except tk.TclError:
                    pass
                setattr(self, nombre, None)

    def mostrar(self):
        self._mostrar_id = None
        try:
            x, y = self.widget.winfo_pointerxy()
            debajo = self.widget.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            return
        if debajo is None or not str(debajo).startswith(str(self.widget)):
            return                       # el mouse ya no está encima
        if Ayuda.actual and Ayuda.actual is not self:
            Ayuda.actual.ocultar()
        if self.ventana:
            return
        self.ventana = tk.Toplevel(self.widget)
        self.ventana.wm_overrideredirect(True)
        self.ventana.wm_geometry(f"+{self.widget.winfo_rootx() + 20}"
                                 f"+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
        tk.Label(self.ventana, text=self.texto, background="#ffffe0", relief="solid", borderwidth=1,
                 wraplength=340, justify="left", padx=6, pady=4).pack()
        Ayuda.actual = self
        self._cerrar_id = self.widget.after(8000, self.ocultar)

    def ocultar(self, _=None):
        self._cancelar()
        if self.ventana:
            try:
                self.ventana.destroy()
            except tk.TclError:
                pass
            self.ventana = None
        if Ayuda.actual is self:
            Ayuda.actual = None

    @classmethod
    def ocultar_todas(cls, _=None):
        if cls.actual:
            cls.actual.ocultar()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Silueta3D")
        root.geometry("1260x800")
        root.minsize(1020, 660)

        self.cola: queue.Queue = queue.Queue()
        self.ocupado = False
        self.cache_imagen: tuple[str, core.Imagen] | None = None
        self.ultima_carpeta: Path | None = None
        self.ultimo_res: core.Resultado | None = None

        # vista previa con zoom
        self.vista_pil: Image.Image | None = None
        self.mapa: dict | None = None      # pixel de la vista previa -> mm
        self.foto = None
        self.zoom = 1.0
        self.centro = (0.0, 0.0)           # punto de la vista previa que queda en el medio
        self.encuadre = (0.0, 0.0, 1.0)    # (x0, y0, escala) de lo que se muestra
        self.arrastre = None

        # huecos calados, con deshacer / rehacer
        self.huecos: list = []
        self.deshacer_pila: list = []
        self.rehacer_pila: list = []

        self.v = {nombre: tk.BooleanVar() if nombre in BOOLEANOS else tk.StringVar() for nombre in CAMPOS}
        self.v_ruta = tk.StringVar()
        self.v_nombre = tk.StringVar()
        self.v_estado = tk.StringVar(value="Elegí una imagen (JPG, PNG o SVG).")
        self.v_modo_hueco = tk.BooleanVar(value=False)
        self.unidades = {}
        self.v_mm = {k: tk.StringVar() for k in ("lineas_pared", "capas_piso", "capas_hueco", "lineas_argolla",
                                                 "iman_ajuste", "pin_ajuste")}
        self._piso_sin_accesorio = None
        self.medida_guia = "alto"     # con «Proporcional», la medida que escribió el usuario
        self._fijando = False         # True mientras el programa completa la otra medida   # capas de piso antes de pasar a imán o pin (se recuperan al salir)
        self._control_pendiente = None
        self._previa_programada = None
        self.previa_pendiente = False
        self.crear_pendiente = False
        self._cargar_config()
        self._capa_ant = float(self.v["capa"].get())
        self._armar()
        if not self.v["alto"].get().strip() and self.v["ancho"].get().strip():
            self.medida_guia = "ancho"
        for nombre in ("alto", "ancho"):
            self.v[nombre].trace_add("write", lambda *_, n=nombre: self._medida_editada(n))
        for var in self.v.values():
            var.trace_add("write", lambda *_: self._programar_control())
        self._controlar()
        # si la ventana se mueve, se minimiza o pierde el foco, no puede quedar ningún globito suelto
        for evento in ("<Configure>", "<Unmap>", "<FocusOut>"):
            self.root.bind(evento, lambda e: Ayuda.ocultar_todas() if e.widget is self.root else None, add="+")
        modificador = "Command" if ES_MAC else "Control"
        for tecla, accion in (("z", self.deshacer), ("Z", self.deshacer), ("y", self.rehacer), ("Y", self.rehacer)):
            self.root.bind_all(f"<{modificador}-{tecla}>", lambda _, a=accion: a())
        self.root.after(100, self._revisar_cola)
        self.root.protocol("WM_DELETE_WINDOW", self._cerrar)

    def _cerrar(self):
        """Al cerrar se guardan las preferencias (boquilla, medidas, ajustes) para la próxima vez."""
        try:
            self._guardar_config(self._leer(estricto=False))
        except ValueError:
            pass
        self.root.destroy()

    # ------------------------------------------------------------------ interfaz
    def _armar(self):
        medir = tkfont.nametofont("TkDefaultFont").measure
        self.ancho_nombres = max(medir(t) for t in ("Profundidad del hueco", "Tolerancia de fondo",
                                                    "Holgura etiqueta", "Margen blanco")) + 12
        izq = ttk.Frame(self.root, padding=12)
        izq.grid(row=0, column=0, sticky="ns")
        der = ttk.Frame(self.root, padding=(0, 12, 12, 12))
        der.grid(row=0, column=1, sticky="nsew")
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # impresora: define qué valores son posibles
        caja = ttk.LabelFrame(izq, text="Impresora 3D", padding=8)
        caja.pack(fill="x")
        ttk.Label(caja, text="Boquilla").pack(side="left")
        boquilla = ttk.Combobox(caja, textvariable=self.v["boquilla"], state="readonly", width=5,
                                values=[f"{b:g}" for b in core.BOQUILLAS])
        boquilla.pack(side="left", padx=(6, 2))
        boquilla.bind("<<ComboboxSelected>>", lambda _: self._cambio_boquilla())
        ttk.Label(caja, text="mm").pack(side="left")
        ttk.Label(caja, text="Capa").pack(side="left", padx=(14, 0))
        self.cb_capa = ttk.Combobox(caja, textvariable=self.v["capa"], state="readonly", width=5)
        self.cb_capa.pack(side="left", padx=(6, 2))
        self.cb_capa.bind("<<ComboboxSelected>>", lambda _: self._cambio_capa())
        ttk.Label(caja, text="mm").pack(side="left")
        ttk.Label(caja, text="Material").pack(side="left", padx=(14, 0))
        material = ttk.Combobox(caja, textvariable=self.v["material"], values=list(core.DENSIDADES),
                                state="readonly", width=7)
        material.pack(side="left", padx=(6, 0))
        Ayuda(material, "Filamento con el que se imprime: se usa para calcular el peso de la pieza.")
        Ayuda(boquilla, "Diámetro de la boquilla. Define el ancho de línea (pared = líneas × boquilla) "
                        "y qué alturas de capa son válidas. En el slicer usá ancho de línea = boquilla.")
        Ayuda(self.cb_capa, "Altura de capa: solo se ofrecen las válidas para la boquilla (25 % a 75 % del "
                            "diámetro, en múltiplos de 0.04).")
        self._actualizar_capas()

        libreta = ttk.Notebook(izq)
        libreta.pack(fill="x", pady=(10, 0))
        pest1 = ttk.Frame(libreta, padding=8)
        pest2 = ttk.Frame(libreta, padding=8)
        libreta.add(pest1, text="Diseño")
        libreta.add(pest2, text="Ajustes")

        # --- pestaña Diseño
        caja = ttk.LabelFrame(pest1, text="Imagen", padding=8)
        caja.pack(fill="x")
        fila = ttk.Frame(caja)
        fila.pack(fill="x")
        ttk.Entry(fila, textvariable=self.v_ruta, width=30).pack(side="left", fill="x", expand=True)
        ttk.Button(fila, text="Elegir…", command=self.elegir).pack(side="left", padx=(6, 0))
        fila = ttk.Frame(caja)
        fila.pack(fill="x", pady=(8, 0))
        ttk.Label(fila, text="Nombre proyecto:").pack(side="left")
        e = ttk.Entry(fila, textvariable=self.v_nombre)
        e.pack(side="left", fill="x", expand=True, padx=(6, 0))
        Ayuda(e, "Nombre de la carpeta y de los archivos que se crean en «salida».")

        caja = ttk.LabelFrame(pest1, text="Tipo de pieza", padding=8)
        caja.pack(fill="x", pady=(10, 0))
        tipo_pieza = ttk.Combobox(caja, textvariable=self.v["tipo_pieza"], values=core.TIPOS_PIEZA,
                                  state="readonly")
        tipo_pieza.pack(fill="x")
        Ayuda(tipo_pieza, "Llavero: con argolla para la cadenita.\n"
                          "Imán: hueco redondo atrás para pegar un imán (el piso se engrosa solo).\n"
                          "Pin / Snap: rebaje adentro para la cabeza (queda tapada por la etiqueta) y "
                          "agujero pasante para el vástago que sale por atrás.\n"
                          "Base: la pieza sola, sin argolla ni imán.\n"
                          "Las opciones de cada uno están en la pestaña Ajustes.")
        self.v_desc_tipo = tk.StringVar()
        ttk.Label(caja, textvariable=self.v_desc_tipo, foreground="#666").pack(fill="x", pady=(4, 0))

        caja = ttk.LabelFrame(pest1, text="Medida", padding=8)
        caja.pack(fill="x", pady=(10, 0))
        self._campo(caja, 0, "Alto", "alto", 10, 300, 1)
        self._campo(caja, 1, "Ancho", "ancho", 10, 300, 1)
        casilla = ttk.Checkbutton(caja, text="Proporcional", variable=self.v["proporcional"])
        casilla.grid(row=2, column=1, columnspan=2, sticky="w", pady=(2, 0))
        Ayuda(casilla, "Marcada: escribís el alto o el ancho y el otro se calcula solo, sin deformar el diseño.\n"
                       "Desmarcada: se usan las dos medidas tal cual y la imagen se estira para llenarlas.")
        self.v_pista_medida = tk.StringVar()
        ttk.Label(caja, textvariable=self.v_pista_medida, foreground="#666").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        caja = ttk.LabelFrame(pest1, text="Forma", padding=8)
        caja.pack(fill="x", pady=(10, 0))
        forma = ttk.Combobox(caja, textvariable=self.v["forma"], values=core.FORMAS, state="readonly", width=20)
        self._fila(caja, 0, "Contorno", forma, "")
        Ayuda(forma, "Automática: sigue la silueta del diseño. Si no hay un fondo claro (por ejemplo una foto), "
                     "elegí círculo, rectángulo redondeado o escudo.")
        Ayuda(self._campo(caja, 1, "Margen blanco", "margen", 0, 5, 0.5),
              "Borde blanco opcional alrededor del diseño, tipo sticker. 0 = sigue el borde del diseño.")

        # --- pestaña Ajustes
        caja = ttk.LabelFrame(pest2, text="Pieza 3D", padding=8)
        caja.pack(fill="x")
        Ayuda(self._giro(caja, 0, "Pared", "lineas_pared", 1, 6, "líneas"),
              "Cantidad de líneas de la pared perimetral (y de la pared de los huecos). Recomendado: 2 "
              "(3 en llaveros grandes o si querés más resistencia).")
        Ayuda(self._giro(caja, 1, "Piso", "capas_piso", 1, 30, "capas"),
              "Espesor del fondo. Recomendado ~1.2 mm (6 capas de 0.2): rígido y no se transparenta.")
        Ayuda(self._giro(caja, 2, "Profundidad del hueco", "capas_hueco", 1, 30, "capas"),
              "Altura de la pared sobre el piso: papel (~0.2 mm) + resina. Recomendado ~1.2 mm.")
        Ayuda(self._campo(caja, 3, "Holgura etiqueta", "holgura", 0, 1, 0.05),
              "Espacio por lado entre la etiqueta y la pared, para que entre aunque el corte se corra un poco.")

        contenedor = ttk.Frame(pest2)
        contenedor.pack(fill="x", pady=(8, 0))
        self.recuadros_pieza = {}

        caja = ttk.LabelFrame(contenedor, text="Argolla", padding=8)
        posicion = ttk.Combobox(caja, textvariable=self.v["argolla"], values=POSICIONES, state="readonly", width=16)
        self._fila(caja, 0, "Posición", posicion, "")
        Ayuda(posicion, "Dónde va la argolla.")
        Ayuda(self._giro(caja, 1, "Pared", "lineas_argolla", 2, 15, "líneas"),
              "Cantidad de líneas alrededor del agujero (el diámetro exterior se calcula solo). "
              "Conviene 4 o más para que la cadenita no la corte.")
        Ayuda(self._campo(caja, 2, "Agujero", "agujero", 1, 10, 0.5, "mm de diámetro"),
              "Diámetro del agujero para la cadenita.")
        self.recuadros_pieza[core.PIEZA_LLAVERO] = caja

        caja = ttk.LabelFrame(contenedor, text="Imán", padding=8)
        Ayuda(self._campo(caja, 0, "Diámetro", "iman_diametro", 3, 40, 1),
              "Diámetro del imán redondo (por ejemplo 10 mm). Medilo con calibre.")
        Ayuda(self._campo(caja, 1, "Espesor", "iman_espesor", 1, 6, 0.5),
              "Espesor del imán (por ejemplo 2 mm). El piso se engrosa solo para que entre.")
        Ayuda(self._campo(caja, 2, "Ajuste", "iman_ajuste", 0, 1, 0.05, self.v_mm["iman_ajuste"]),
              "Cuánto más grande que el imán es el hueco (en el diámetro), para que entre sin forzar. "
              "Si queda flojo, bajalo; si no entra, subilo.")
        self.recuadros_pieza[core.PIEZA_IMAN] = caja

        caja = ttk.LabelFrame(contenedor, text="Pin / Snap", padding=8)
        Ayuda(self._campo(caja, 0, "Cabeza", "pin_cabeza", 2, 30, 0.5, "mm de diámetro"),
              "Diámetro de la cabeza del pin o del snap. Va en un rebaje del lado de adentro y queda "
              "tapada por la etiqueta.")
        Ayuda(self._campo(caja, 1, "Espesor cabeza", "pin_cabeza_espesor", 0.5, 4, 0.5),
              "Espesor de la cabeza. El piso se engrosa solo para que entre.")
        Ayuda(self._campo(caja, 2, "Agujero", "pin_agujero", 0.5, 8, 0.1, "mm de diámetro"),
              "Diámetro del vástago o aguja que sale por atrás.")
        Ayuda(self._campo(caja, 3, "Ajuste", "pin_ajuste", 0, 1, 0.05, self.v_mm["pin_ajuste"]),
              "Cuánto más grandes que la pieza metálica son el rebaje y el agujero, para que entren sin forzar.")
        self.recuadros_pieza[core.PIEZA_PIN] = caja

        caja = ttk.LabelFrame(contenedor, text="Base", padding=8)
        ttk.Label(caja, text="La pieza sola, sin argolla ni imán.", foreground="#666").pack(anchor="w")
        self.recuadros_pieza[core.PIEZA_BASE] = caja

        caja = ttk.LabelFrame(pest2, text="Etiqueta", padding=8)
        caja.pack(fill="x", pady=(8, 0))
        Ayuda(self._campo(caja, 0, "Sangrado", "sangrado", 0, 2, 0.1),
              "Cuánto se extiende la imagen más allá de la línea de corte (se descarta al cortar).")
        Ayuda(self._campo(caja, 1, "Suavizado", "suavizado", 0, 2, 0.1),
              "Redondea detalles más finos que ~2× este valor. 0 = sin suavizar.")
        Ayuda(self._campo(caja, 2, "Tolerancia de fondo", "tolerancia", 5, 150, 5, ""),
              "Cuánto puede variar el color del fondo (también para la varita de huecos). "
              "Subir si queda fondo, bajar si se come el diseño.")
        ttk.Button(pest2, text="Valores recomendados para esta boquilla", command=self.restablecer).pack(
            fill="x", pady=(8, 0))

        # control de la configuración
        self.control = tk.Label(izq, justify="left", anchor="w", wraplength=300, padx=4, pady=4)
        self.control.pack(fill="x", pady=(8, 0))

        # acciones
        fila = ttk.Frame(izq)
        fila.pack(fill="x", pady=(6, 0))
        self.b_crear = ttk.Button(fila, text="Crear archivos", command=self.crear)
        self.b_crear.pack(side="left", fill="x", expand=True)
        Ayuda(self.b_crear, "Guarda exactamente lo que se ve en la vista (SVG, PNG y STL) en la carpeta salida.")
        self.b_abrir = ttk.Button(izq, text="Abrir carpeta", command=self.abrir_carpeta, state="disabled")
        self.b_abrir.pack(fill="x", pady=(6, 0))
        self.barra = ttk.Progressbar(izq, mode="determinate")
        self.barra.pack(fill="x", pady=(8, 0))

        # --- dos áreas separadas: etiqueta (papel) y pieza 3D, cada una con sus herramientas
        estilo = ttk.Style()
        negrita = tkfont.nametofont("TkDefaultFont").copy()
        negrita.configure(weight="bold")
        self._fuente_titulos = negrita
        estilo.configure("Area.TLabelframe.Label", font=negrita)
        estilo.configure("Pista.TLabel", foreground="#777")
        areas = ttk.Frame(der)
        areas.pack(fill="both", expand=True)
        areas.columnconfigure(0, weight=1, uniform="a")
        areas.columnconfigure(1, weight=1, uniform="a")
        areas.rowconfigure(0, weight=1)

        # etiqueta
        area = ttk.LabelFrame(areas, text=" Etiqueta (papel) ", style="Area.TLabelframe", padding=6)
        area.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        barra = ttk.Frame(area)
        barra.pack(fill="x", pady=(0, 6))
        self.b_modo = ttk.Checkbutton(barra, text="✚ Marcar huecos", variable=self.v_modo_hueco,
                                      style="Toolbutton", command=self._cambio_modo)
        self.b_modo.pack(side="left")
        Ayuda(self.b_modo, "Activado: un clic sobre una zona del color del fondo (por ejemplo entre el brazo y el "
                           "cuerpo) la selecciona toda, como la varita mágica. Clic sobre el círculo celeste la "
                           "vuelve a rellenar.")
        # íconos: los nativos de Windows si está su fuente; si no (macOS, Linux), símbolos comunes
        if "Segoe MDL2 Assets" in tkfont.families():
            estilo.configure("Icono.TButton", font=("Segoe MDL2 Assets", 11), padding=(2, 2))
            iconos = ("\uE7A7", "\uE7A6", "\uE74D")
        else:
            estilo.configure("Icono.TButton", padding=(2, 0))
            iconos = ("↶", "↷", "✕")
        self.b_quitar = ttk.Button(barra, text=iconos[2], width=3, style="Icono.TButton",
                                   command=self.quitar_huecos, state="disabled")
        self.b_quitar.pack(side="right")
        Ayuda(self.b_quitar, "Quitar todos los huecos calados")
        self.b_rehacer = ttk.Button(barra, text=iconos[1], width=3, style="Icono.TButton",
                                    command=self.rehacer, state="disabled")
        self.b_rehacer.pack(side="right", padx=(0, 2))
        Ayuda(self.b_rehacer, f"Rehacer ({ATAJO}+Y)")
        self.b_deshacer = ttk.Button(barra, text=iconos[0], width=3, style="Icono.TButton",
                                     command=self.deshacer, state="disabled")
        self.b_deshacer.pack(side="right", padx=(6, 2))
        Ayuda(self.b_deshacer, f"Deshacer el último hueco ({ATAJO}+Z)")
        tipo = ttk.Combobox(barra, textvariable=self.v["tipo_hueco"], values=core.TIPOS_HUECO,
                            state="readonly", width=12)
        tipo.pack(side="left", fill="x", expand=True, padx=(6, 0))
        Ayuda(tipo, "Tipo de hueco:\n" + AYUDA_TIPOS)

        self.lienzo = tk.Canvas(area, background="white", highlightthickness=1, highlightbackground="#ccc")
        self.lienzo.pack(fill="both", expand=True)
        self.lienzo.bind("<Configure>", lambda _: self._mostrar_vista())
        self.lienzo.bind("<ButtonPress-1>", self._presionar)
        self.lienzo.bind("<B1-Motion>", self._arrastrar)
        self.lienzo.bind("<ButtonRelease-1>", self._soltar)
        for boton in (2, 3):   # clic derecho (2 en macOS, 3 en Windows/Linux) o del medio: mover
            self.lienzo.bind(f"<ButtonPress-{boton}>", self._presionar)
            self.lienzo.bind(f"<B{boton}-Motion>", self._arrastrar)
            self.lienzo.bind(f"<ButtonRelease-{boton}>", lambda _: setattr(self, "arrastre", None))
        self.lienzo.bind("<MouseWheel>", self._rueda)
        self.lienzo.bind("<Button-4>", lambda e: self._rueda(e, +1))   # rueda en Linux
        self.lienzo.bind("<Button-5>", lambda e: self._rueda(e, -1))

        pie = ttk.Frame(area)
        pie.pack(fill="x", pady=(6, 0))
        b = ttk.Button(pie, text="Ajustar", width=8, command=self.zoom_ajustar)
        b.pack(side="right")
        Ayuda(b, "Ver la etiqueta entera. Rueda del mouse: zoom · arrastrar: mover")
        self.v_zoom = tk.StringVar(value="100 %")
        ttk.Label(pie, textvariable=self.v_zoom, width=6, anchor="e").pack(side="right", padx=(0, 6))

        # pieza 3D
        area = ttk.LabelFrame(areas, text=" Pieza 3D (STL) ", style="Area.TLabelframe", padding=6)
        area.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        barra = ttk.Frame(area)
        barra.pack(fill="x", pady=(0, 6))
        lienzo3d = tk.Canvas(area, background="#f6f6f6", highlightthickness=1, highlightbackground="#ccc",
                             cursor="fleur")
        self.v_zoom3d = tk.StringVar(value="100 %")
        self.visor = Visor3D(lienzo3d, self.v_zoom3d)
        b = ttk.Button(barra, text="Frontal", command=self.visor.frontal)
        b.pack(side="left")
        Ayuda(b, "Vista de frente, como se ve el llavero (también con doble clic sobre la pieza).")
        b = ttk.Button(barra, text="Atrás", command=self.visor.atras)
        b.pack(side="left", padx=(4, 0))
        Ayuda(b, "Ver la pieza de atrás (por ejemplo, el hueco del imán).")
        b = ttk.Button(barra, text="Isométrica", command=self.visor.isometrica)
        b.pack(side="left", padx=(4, 0))
        Ayuda(b, "Vista en perspectiva, para ver el espesor y las paredes.")
        lienzo3d.pack(fill="both", expand=True)
        pie = ttk.Frame(area)
        pie.pack(fill="x", pady=(6, 0))
        b = ttk.Button(pie, text="Ajustar", width=8, command=self.visor.ajustar)
        b.pack(side="right")
        Ayuda(b, "Volver al 100 % y centrar la pieza (mantiene el giro).")
        ttk.Label(pie, textvariable=self.v_zoom3d, width=6, anchor="e").pack(side="right", padx=(0, 6))
        self.v_peso = tk.StringVar(value="")
        peso = ttk.Label(pie, textvariable=self.v_peso)
        peso.pack(side="left")
        Ayuda(peso, "Peso aproximado de la pieza impresa con relleno 100 % (volumen del STL × densidad del "
                    "filamento) y resina para llenar el hueco hasta el borde (sin contar el domo).")

        # resultado
        area = ttk.LabelFrame(der, text=" Resultado ", style="Area.TLabelframe", padding=(8, 4))
        area.pack(fill="x", pady=(8, 0))
        fondo = estilo.lookup("TFrame", "background") or "#f0f0f0"
        self.t_estado = tk.Text(area, height=5, wrap="word", relief="flat", background=fondo, borderwidth=0,
                                font="TkDefaultFont", cursor="arrow", takefocus=0, highlightthickness=0)
        self.t_estado.pack(fill="x")
        self.v_estado.trace_add("write", lambda *_: self._mostrar_estado())
        self._mostrar_estado()

    def _mostrar_estado(self):
        t = self.t_estado
        t.config(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", self.v_estado.get())
        t.config(state="disabled")

    def _fila(self, padre, fila, texto, spin, unidad):
        """Misma disposición en todos los recuadros: nombre | flechitas | unidad (o equivalencia en mm)."""
        padre.columnconfigure(0, minsize=self.ancho_nombres)
        padre.columnconfigure(2, weight=1)
        ttk.Label(padre, text=texto).grid(row=fila, column=0, sticky="w", pady=2, padx=(0, 8))
        spin.grid(row=fila, column=1, sticky="ew" if isinstance(spin, ttk.Combobox) else "w", pady=2,
                  columnspan=2 if isinstance(spin, ttk.Combobox) and not unidad else 1)
        if isinstance(spin, ttk.Combobox) and not unidad:
            return spin
        etiqueta = ttk.Label(padre, foreground="#666", width=ANCHO_UNIDAD)
        if isinstance(unidad, tk.StringVar):
            etiqueta.config(textvariable=unidad)
        else:
            etiqueta.config(text=unidad)
        etiqueta.grid(row=fila, column=2, sticky="w", padx=(6, 0))
        return spin

    def _campo(self, padre, fila, texto, campo, desde, hasta, paso, unidad="mm"):  # unidad: texto o StringVar
        """Campo numérico con flechitas (también se puede escribir)."""
        decimales = 0 if paso >= 1 else len(f"{paso:g}".split(".")[1])
        s = ttk.Spinbox(padre, textvariable=self.v[campo], from_=desde, to=hasta, increment=paso, width=7,
                        format=f"%.{decimales}f")
        return self._fila(padre, fila, texto, s, unidad)

    def _giro(self, padre, fila, texto, campo, desde, hasta, unidad):
        """Cantidad entera (líneas o capas) con flechitas y su equivalente en mm."""
        s = ttk.Spinbox(padre, textvariable=self.v[campo], from_=desde, to=hasta, increment=1, width=7)
        self.unidades[campo] = unidad
        return self._fila(padre, fila, texto, s, self.v_mm[campo])

    # ------------------------------------------------------------------ config y control
    def _poner(self, p: core.Parametros, solo=None):
        for k, val in asdict(p).items():
            if k not in self.v or (solo and k not in solo):
                continue
            if k in BOOLEANOS:
                self.v[k].set(bool(val))
            else:
                self.v[k].set("" if val is None else f"{val:g}" if isinstance(val, float) else str(val))

    def _cargar_config(self):
        p = core.recomendados(0.4)
        try:
            datos = json.loads(CONFIG.read_text(encoding="utf-8"))
            for k, val in datos.items():
                if k in CAMPOS:
                    setattr(p, k, val)
        except (OSError, ValueError):
            pass
        if p.tipo_hueco not in core.TIPOS_HUECO:
            p.tipo_hueco = core.TIPO_PARED
        if p.boquilla not in core.BOQUILLAS:
            p.boquilla = 0.4
        if p.argolla not in POSICIONES:   # configuraciones viejas guardaban "Sin argolla" como posición
            p.tipo_pieza, p.argolla = core.PIEZA_BASE, "Arriba centro"
        if p.tipo_pieza not in core.TIPOS_PIEZA:
            p.tipo_pieza = core.PIEZA_LLAVERO
        self._poner(p)

    def _guardar_config(self, p: core.Parametros):
        try:
            datos = {k: v for k, v in asdict(p).items() if k != "huecos"}
            CONFIG.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def restablecer(self):
        try:
            boquilla = float(self.v["boquilla"].get())
        except ValueError:
            boquilla = 0.4
        self._poner(core.recomendados(boquilla), solo={
            "capa", "lineas_pared", "capas_piso", "capas_hueco", "holgura", "agujero", "lineas_argolla",
            "sangrado", "suavizado", "tolerancia"})
        self._capa_ant = float(self.v["capa"].get())
        self._actualizar_capas()

    def _actualizar_capas(self):
        try:
            capas = core.alturas_de_capa(float(self.v["boquilla"].get()))
        except ValueError:
            return
        if hasattr(self, "cb_capa"):
            self.cb_capa.config(values=[f"{c:g}" for c in capas])

    def _cambio_boquilla(self):
        """Nueva boquilla: se ofrecen sus alturas de capa; si la actual no sirve, se elige la recomendada."""
        self._actualizar_capas()
        boquilla = float(self.v["boquilla"].get())
        capas = core.alturas_de_capa(boquilla)
        try:
            capa = float(self.v["capa"].get())
        except ValueError:
            capa = None
        if capa not in capas:
            self._cambio_capa(core.recomendados(boquilla).capa)

    def _cambio_capa(self, nueva=None):
        """Nueva altura de capa: se mantienen los mm del piso y del hueco (cambia la cantidad de capas)."""
        anterior = self._capa_ant
        if nueva is not None:
            self.v["capa"].set(f"{nueva:g}")
        try:
            capa = float(self.v["capa"].get())
            piso, hueco = int(self.v["capas_piso"].get()), int(self.v["capas_hueco"].get())
        except ValueError:
            return
        if anterior and abs(anterior - capa) > 1e-9:
            self.v["capas_piso"].set(str(max(1, round(piso * anterior / capa))))
            self.v["capas_hueco"].set(str(max(1, round(hueco * anterior / capa))))
        self._capa_ant = capa

    def _leer(self, estricto=True) -> core.Parametros:
        p = core.Parametros()
        p.huecos = list(self.huecos)
        for nombre, var in self.v.items():
            if nombre in BOOLEANOS:
                setattr(p, nombre, bool(var.get()))
                continue
            texto = var.get()
            if nombre in TEXTOS:
                setattr(p, nombre, texto)
                continue
            try:
                val = a_numero(texto, entero=nombre in ENTEROS)
            except ValueError:
                raise ValueError(f"{NOMBRES.get(nombre, nombre)}: «{texto}» no es un número válido.")
            if val is None and nombre not in ("alto", "ancho"):
                if estricto:
                    raise ValueError(f"Falta completar «{NOMBRES.get(nombre, nombre)}».")
                val = getattr(core.Parametros(), nombre)
            setattr(p, nombre, val)
        if p.proporcional:   # manda solo la medida que escribió el usuario; la otra se muestra calculada
            setattr(p, "ancho" if self.medida_guia == "alto" else "alto", None)
        return p

    def _medida_editada(self, nombre):
        # manda la última medida en la que el usuario escribió un valor (borrar no cambia cuál manda)
        if not self._fijando and self.v[nombre].get().strip():
            self.medida_guia = nombre

    def _parametros(self) -> core.Parametros:
        p = self._leer()
        errores, _ = core.validar(p)
        if errores:
            raise ValueError("\n".join(errores))
        return p

    def _programar_control(self):
        if self._control_pendiente:
            self.root.after_cancel(self._control_pendiente)
        self._control_pendiente = self.root.after(150, self._controlar)

    def _controlar(self):
        """Control en vivo: equivalencias en mm y si la configuración es válida para la boquilla."""
        self._control_pendiente = None
        try:
            p = self._leer()
        except ValueError as e:
            self.control.config(text=f"✖ {e}", fg="#b00020", bg="#fdecee")
            return
        self.v_desc_tipo.set(DESCRIPCION_TIPO.get(p.tipo_pieza, ""))
        self.v_pista_medida.set("Del cuerpo, sin la argolla. Escribí una y la otra se calcula sola." if p.proporcional
                                else "Del cuerpo, sin la argolla. Se usan las dos: la imagen se estira.")
        for tipo, recuadro in self.recuadros_pieza.items():
            if tipo == p.tipo_pieza:
                recuadro.pack(fill="x")
            else:
                recuadro.pack_forget()
        if self._ajustar_piso(p):
            return                        # cambió el piso: el control se vuelve a correr solo
        u = self.unidades
        self.v_mm["lineas_pared"].set(f"{u['lineas_pared']} = {p.pared:g} mm")
        self.v_mm["capas_piso"].set(f"{u['capas_piso']} = {p.piso:g} mm")
        self.v_mm["capas_hueco"].set(f"{u['capas_hueco']} = {p.profundidad:g} mm")
        self.v_mm["lineas_argolla"].set(f"{u['lineas_argolla']} = {p.lineas_argolla * p.boquilla:g} mm "
                                        f"· Ø ext. {p.argolla_ext:g} mm")
        self.v_mm["iman_ajuste"].set(f"mm · hueco Ø {p.iman_diametro + p.iman_ajuste:g} × "
                                     f"{p.iman_espesor + core.HOLGURA_Z_IMAN:g} mm")
        self.v_mm["pin_ajuste"].set(f"mm · rebaje Ø {p.pin_cabeza + p.pin_ajuste:g}, "
                                    f"agujero Ø {p.pin_agujero + p.pin_ajuste:g}")
        errores, avisos = core.validar(p)
        if errores:
            self.control.config(text="✖ " + "\n✖ ".join(errores), fg="#b00020", bg="#fdecee")
        elif avisos:
            self.control.config(text="⚠ " + "\n⚠ ".join(avisos), fg="#8a5a00", bg="#fff6e0")
        else:
            self.control.config(text=f"✔ Configuración válida para boquilla de {p.boquilla:g} mm: pared "
                                     f"{p.lineas_pared} líneas ({p.pared:g} mm), alto total "
                                     f"{p.piso + p.profundidad:g} mm.", fg="#1b6e20", bg="#e9f6ea")
        if not errores:
            self._programar_previa()

    def _ajustar_piso(self, p) -> bool:
        """Con imán o pin, el piso se engrosa lo necesario; al volver a argolla o base recupera el que tenía."""
        if p.piso_minimo:
            necesarias = math.ceil(p.piso_minimo / p.capa - 1e-6)
            if p.capas_piso < necesarias:
                if self._piso_sin_accesorio is None:
                    self._piso_sin_accesorio = p.capas_piso
                self.v["capas_piso"].set(str(necesarias))
                return True
        elif self._piso_sin_accesorio is not None:
            anterior, self._piso_sin_accesorio = self._piso_sin_accesorio, None
            if anterior != p.capas_piso:
                self.v["capas_piso"].set(str(anterior))
                return True
        return False

    def _programar_previa(self):
        """Vista en vivo: se actualiza sola un instante después del último cambio."""
        if not Path(self.v_ruta.get().strip()).is_file():
            return
        if self._previa_programada:
            self.root.after_cancel(self._previa_programada)
        self._previa_programada = self.root.after(ESPERA_VIVO, lambda: self.previa(auto=True))

    # ------------------------------------------------------------------ acciones
    def elegir(self):
        ruta = filedialog.askopenfilename(
            title="Elegí la imagen del llavero",
            filetypes=[("Imágenes", "*.png *.jpg *.jpeg *.svg *.webp *.bmp"), ("Todos", "*.*")])
        if not ruta:
            return
        self.v_ruta.set(ruta)
        self.v_nombre.set(Path(ruta).stem)
        self.previa()

    def _imagen(self, ruta):
        if self.cache_imagen and self.cache_imagen[0] == ruta:
            return self.cache_imagen[1]
        imagen = core.cargar_imagen(ruta)
        self.cache_imagen = (ruta, imagen)
        return imagen

    def _preparar(self, silencioso=False):
        ruta = self.v_ruta.get().strip()
        if not ruta or not Path(ruta).is_file():
            if not silencioso:
                messagebox.showwarning("Falta la imagen", "Elegí una imagen primero.")
            return None
        if self.cache_imagen and self.cache_imagen[0] != ruta:
            # imagen nueva: los huecos marcados y el zoom eran de la anterior
            self.huecos, self.deshacer_pila, self.rehacer_pila = [], [], []
            self.vista_pil = None
        try:
            return ruta, self._parametros()
        except ValueError as e:
            if not silencioso:
                messagebox.showwarning("Revisá los datos", str(e))
            return None

    def _al_dia(self, ruta, p) -> bool:
        """¿La vista ya muestra exactamente esta imagen con estos valores?"""
        return bool(self.ultimo_res and self.cache_imagen and self.cache_imagen[0] == ruta
                    and self.ultimo_res.p == p)

    def previa(self, auto=False):
        self._previa_programada = None
        if self.ocupado:                 # se actualiza apenas termine lo que está en curso
            self.previa_pendiente = True
            return
        datos = self._preparar(silencioso=auto)
        if not datos:
            return
        ruta, p = datos
        if self._al_dia(ruta, p):
            return

        def tarea():
            res = core.procesar(self._imagen(ruta), p)
            return res, self._vista_etiqueta(res)

        self._en_segundo_plano("⟳ Actualizando la vista…", tarea, self._al_previsualizar)

    def crear(self):
        if self._previa_programada:
            self.root.after_cancel(self._previa_programada)
            self._previa_programada = None
        if self.ocupado:                 # primero termina la vista en curso, después se crea con ese resultado
            self.crear_pendiente = True
            self.v_estado.set("⟳ Terminando la vista para crear los archivos…")
            return
        datos = self._preparar()
        if not datos:
            return
        ruta, p = datos
        nombre = core.nombre_seguro(self.v_nombre.get() or Path(ruta).stem)
        if (SALIDA / nombre).exists() and not messagebox.askyesno(
                "Ya existe", f"Ya hay una carpeta «{nombre}» en salida.\n¿Querés reemplazar sus archivos?"):
            return
        self._guardar_config(p)

        if self._al_dia(ruta, p):        # se guarda exactamente lo que se está viendo
            res_visto, vista_vista = self.ultimo_res, (self.vista_pil, self.mapa)

            def tarea():
                return res_visto, vista_vista, core.guardar(res_visto, ruta, SALIDA, nombre)
        else:
            def tarea():
                res = core.procesar(self._imagen(ruta), p)
                carpeta = core.guardar(res, ruta, SALIDA, nombre)
                return res, self._vista_etiqueta(res), carpeta

        self._en_segundo_plano("Creando archivos…", tarea, self._al_crear)

    def abrir_carpeta(self):
        if self.ultima_carpeta and self.ultima_carpeta.exists():
            try:
                if sys.platform == "win32":
                    os.startfile(self.ultima_carpeta)
                else:
                    subprocess.Popen(["open" if ES_MAC else "xdg-open", str(self.ultima_carpeta)])
            except OSError:
                messagebox.showinfo("Carpeta", f"Los archivos están en:\n{self.ultima_carpeta}")

    @staticmethod
    def _vista_etiqueta(res):
        img, mapa = core.vista_previa(res, alto_px=ALTO_VISTA, con_mapa=True)
        return img.crop((0, 0, round(mapa["ancho_panel"]), img.height)), mapa

    # ------------------------------------------------------------------ resultados
    def _texto_resultado(self, res: core.Resultado) -> str:
        m = res.medidas()
        texto = (f"Cuerpo {m['cuerpo'][0]:.1f} × {m['cuerpo'][1]:.1f} mm  ·  "
                 + (f"con argolla {m['total'][0]:.1f} × {m['total'][1]:.1f} mm  ·  " if res.geo.centro_argolla
                    else f"imán {res.p.iman_diametro:g} × {res.p.iman_espesor:g} mm atrás  ·  " if res.geo.iman
                    else f"pin / snap (cabeza Ø {res.p.pin_cabeza:g} mm)  ·  " if res.geo.pin
                    else "base sola  ·  ") +
                 f"etiqueta {m['etiqueta'][0]:.1f} × {m['etiqueta'][1]:.1f} mm  ·  alto {m['altura']:.1f} mm  ·  "
                 f"{res.peso:.1f} g de {res.p.material}  ·  resina ~{res.resina_ml:.2f} ml")
        if res.avisos:
            texto += "\n⚠ " + "\n⚠ ".join(res.avisos)
        return texto

    def _al_previsualizar(self, resultado):
        res, (img, mapa) = resultado
        anterior = self.vista_pil.size if self.vista_pil else None
        self.vista_pil, self.mapa, self.ultimo_res = img, mapa, res
        if anterior is None:
            self.zoom, self.centro = 1.0, (img.width / 2, img.height / 2)
        elif anterior != img.size:  # mantener el mismo lugar enfocado
            fx, fy = img.width / anterior[0], img.height / anterior[1]
            self.centro = (self.centro[0] * fx, self.centro[1] * fy)
        self._actualizar_botones()
        self._mostrar_vista()
        m = res.medidas()
        self._completar_medida(res, m)
        self.visor.cargar(res.vertices, res.triangulos, piso=res.p.piso, texto=
                          f"Cuerpo {m['cuerpo'][0]:.1f} × {m['cuerpo'][1]:.1f} mm · alto {m['altura']:.1f} mm")
        self.v_peso.set(f"⚖ {res.peso:.1f} g de {res.p.material} · resina ~{res.resina_ml:.2f} ml")
        self.v_estado.set(self._texto_resultado(res))

    def _completar_medida(self, res, m):
        """Con «Proporcional», muestra la otra medida tal como salió (sin volver a calcular)."""
        if not (res.p.proporcional and self.v["proporcional"].get()):
            return
        guia = "alto" if res.p.alto and not res.p.ancho else "ancho" if res.p.ancho and not res.p.alto else None
        if guia != self.medida_guia:
            return                        # mientras tanto el usuario empezó a escribir en la otra
        otra = "ancho" if guia == "alto" else "alto"
        valor = f"{m['cuerpo'][0 if otra == 'ancho' else 1]:.1f}"
        if self.v[otra].get() != valor:
            self._fijando = True
            try:
                self.v[otra].set(valor)
            finally:
                self._fijando = False

    def _al_crear(self, resultado):
        res, vista, carpeta = resultado
        self.ultima_carpeta = carpeta
        self.b_abrir.config(state="normal")
        self._al_previsualizar((res, vista))
        self.v_estado.set(f"✔ Listo: {carpeta}\n" + self._texto_resultado(res))

    # ------------------------------------------------------------------ vista con zoom
    def _escala_base(self):
        W, H = self.vista_pil.size
        return min(max(self.lienzo.winfo_width(), 10) / W, max(self.lienzo.winfo_height(), 10) / H)

    def _mostrar_vista(self, rapido=False):
        self.lienzo.delete("all")
        if not self.vista_pil:
            return
        cw, ch = self.lienzo.winfo_width(), self.lienzo.winfo_height()
        if cw < 20 or ch < 20:
            return
        W, H = self.vista_pil.size
        escala = self._escala_base() * self.zoom
        vx0 = self.centro[0] - cw / 2 / escala
        vy0 = self.centro[1] - ch / 2 / escala
        bx0, by0 = max(0.0, vx0), max(0.0, vy0)
        bx1, by1 = min(W, vx0 + cw / escala), min(H, vy0 + ch / escala)
        self.encuadre = (vx0, vy0, escala)
        self.v_zoom.set(f"{self.zoom * 100:.0f} %")
        if bx1 <= bx0 or by1 <= by0:
            return
        tam = (max(1, round((bx1 - bx0) * escala)), max(1, round((by1 - by0) * escala)))
        filtro = Image.BILINEAR if rapido else Image.LANCZOS
        img = self.vista_pil.resize(tam, filtro, box=(bx0, by0, bx1, by1))
        self.foto = ImageTk.PhotoImage(img)
        self.lienzo.create_image(round((bx0 - vx0) * escala), round((by0 - vy0) * escala),
                                 image=self.foto, anchor="nw")
        self.lienzo.create_text(8, ch - 6, anchor="sw", fill="#999", text=PISTA_ETIQUETA)

    def _limitar_centro(self):
        W, H = self.vista_pil.size
        self.centro = (min(max(self.centro[0], 0), W), min(max(self.centro[1], 0), H))

    def zoom_en(self, factor, punto=None):
        if not self.vista_pil:
            return
        cw, ch = self.lienzo.winfo_width(), self.lienzo.winfo_height()
        px, py = punto if punto else (cw / 2, ch / 2)
        vx0, vy0, escala = self.encuadre
        fijo = (vx0 + px / escala, vy0 + py / escala)   # el punto bajo el mouse queda quieto
        self.zoom = min(max(self.zoom * factor, 1.0), ZOOM_MAX)
        nueva = self._escala_base() * self.zoom
        self.centro = (fijo[0] - (px - cw / 2) / nueva, fijo[1] - (py - ch / 2) / nueva)
        if self.zoom == 1.0:
            self.centro = (self.vista_pil.width / 2, self.vista_pil.height / 2)
        self._limitar_centro()
        self._mostrar_vista()

    def zoom_ajustar(self):
        if self.vista_pil:
            self.zoom, self.centro = 1.0, (self.vista_pil.width / 2, self.vista_pil.height / 2)
            self._mostrar_vista()

    def _rueda(self, ev, sentido=None):
        sentido = sentido if sentido is not None else ev.delta
        self.zoom_en(1.25 if sentido > 0 else 1 / 1.25, (ev.x, ev.y))

    def _presionar(self, ev):
        self.arrastre = (ev.x, ev.y, self.centro, False)

    def _arrastrar(self, ev):
        if not (self.arrastre and self.vista_pil):
            return
        x0, y0, centro0, _ = self.arrastre
        if abs(ev.x - x0) + abs(ev.y - y0) > 4:
            escala = self.encuadre[2]
            self.centro = (centro0[0] - (ev.x - x0) / escala, centro0[1] - (ev.y - y0) / escala)
            self._limitar_centro()
            self.arrastre = (x0, y0, centro0, True)
            self._mostrar_vista(rapido=True)

    def _soltar(self, ev):
        arrastre, self.arrastre = self.arrastre, None
        if arrastre and arrastre[3]:
            self._mostrar_vista()  # redibujar con mejor calidad
        elif arrastre and self.v_modo_hueco.get():
            self._clic_hueco(ev)

    def _cambio_modo(self):
        self.lienzo.config(cursor="crosshair" if self.v_modo_hueco.get() else "")

    # ------------------------------------------------------------------ huecos
    def _clic_hueco(self, ev):
        res, mapa = self.ultimo_res, self.mapa
        if not (res and mapa):
            return
        vx0, vy0, escala = self.encuadre
        px, py = vx0 + ev.x / escala, vy0 + ev.y / escala
        x, y = (px - mapa["cx"]) / mapa["esc"], (mapa["cy"] - py) / mapa["esc"]
        punto = Point(x, y)

        # clic sobre un círculo marcado -> se quita
        radio = 10 / (escala * mapa["esc"])
        quitar = [h for h in self.huecos if math.dist(res.imagen_a_mm(*h), (x, y)) < radio]
        # clic dentro de un hueco ya calado -> se quitan sus puntos
        for g in getattr(res.geo.L, "geoms", [res.geo.L]):
            for anillo in g.interiors:
                hueco = Polygon(anillo).buffer(res.p.holgura + 0.5)
                if hueco.contains(punto):
                    quitar += [h for h in self.huecos if hueco.contains(Point(res.imagen_a_mm(*h)))]
        if quitar:
            nuevos = [h for h in self.huecos if h not in quitar]
        elif any(Polygon(g.exterior).contains(punto) for g in getattr(res.geo.L, "geoms", [res.geo.L])):
            nuevos = self.huecos + [res.mm_a_imagen(x, y)]
        else:
            self.v_estado.set("Hacé clic dentro de la figura, sobre el hueco que querés calar.")
            return
        self._cambiar_huecos(nuevos)

    def _cambiar_huecos(self, nuevos):
        self.deshacer_pila.append(self.huecos)
        self.rehacer_pila.clear()
        self.huecos = nuevos
        self.previa()

    def quitar_huecos(self):
        if self.huecos:
            self._cambiar_huecos([])

    def deshacer(self):
        if self.deshacer_pila:
            self.rehacer_pila.append(self.huecos)
            self.huecos = self.deshacer_pila.pop()
            self.previa()

    def rehacer(self):
        if self.rehacer_pila:
            self.deshacer_pila.append(self.huecos)
            self.huecos = self.rehacer_pila.pop()
            self.previa()

    def _actualizar_botones(self):
        estado = lambda cond: "normal" if cond else "disabled"
        self.b_deshacer.config(state=estado(self.deshacer_pila))
        self.b_rehacer.config(state=estado(self.rehacer_pila))
        self.b_quitar.config(state=estado(self.huecos))

    # ------------------------------------------------------------------ hilos
    def _en_segundo_plano(self, mensaje, tarea, al_terminar):
        self.v_estado.set(mensaje)
        self.ocupado = True
        self._actualizar_botones()
        self.barra.config(mode="indeterminate")
        self.barra.start(12)

        def correr():
            try:
                self.cola.put((al_terminar, tarea(), None))
            except Exception as e:  # se muestra en la ventana
                self.cola.put((al_terminar, None, e))

        threading.Thread(target=correr, daemon=True).start()

    def _revisar_cola(self):
        try:
            while True:
                al_terminar, resultado, error = self.cola.get_nowait()
                self.barra.stop()
                self.barra.config(mode="determinate", value=0)
                self.ocupado = False
                self._actualizar_botones()
                if error:
                    self.v_estado.set(f"✖ {error}")
                    if not isinstance(error, ValueError):
                        messagebox.showerror("Error", f"{type(error).__name__}: {error}")
                else:
                    al_terminar(resultado)
                if self.crear_pendiente:
                    self.crear_pendiente = False
                    self.crear()
                elif self.previa_pendiente:
                    self.previa_pendiente = False
                    self.previa(auto=True)
        except queue.Empty:
            pass
        self.root.after(100, self._revisar_cola)


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # nitidez en pantallas con escalado
    except Exception:
        pass
    root = tk.Tk()
    tema = {"win32": "vista", "darwin": "aqua"}.get(sys.platform, "clam")   # clam se ve mejor en Linux
    try:
        ttk.Style().theme_use(tema)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
