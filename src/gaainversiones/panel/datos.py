"""Lectura de datos para el panel web, siempre en SOLO LECTURA.

El panel nunca escribe: abre las bases SQLite con ``mode=ro``, así que no puede alterar
el estado del bot ni bloquearlo, y funciona igual con el bot parado.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from gaainversiones.config import Config
from gaainversiones.indicadores import sma
from gaainversiones.tiempo import ms_a_fecha, timeframe_a_ms

_FECHAS = {
    "ejecuciones": ("fecha",),
    "operaciones": ("fecha_entrada", "fecha_salida"),
    "eventos": ("fecha",),
    "capital": ("fecha",),
}


def conexion_lectura(ruta: Path) -> sqlite3.Connection:
    """Conexión SQLite de solo lectura (cualquier escritura falla)."""
    return sqlite3.connect(f"file:{ruta.resolve().as_posix()}?mode=ro", uri=True)


def leer_tabla(ruta: Path, tabla: str) -> pd.DataFrame:
    """Tabla completa del almacén del bot, con las fechas convertidas a UTC."""
    if tabla not in _FECHAS:
        raise ValueError(f"Tabla desconocida: {tabla}")
    if not ruta.exists():
        return pd.DataFrame()
    con = conexion_lectura(ruta)
    try:
        datos = pd.read_sql_query(f"SELECT * FROM {tabla}", con)  # noqa: S608 (tabla validada)
    finally:
        con.close()
    for columna in _FECHAS[tabla]:
        datos[columna] = pd.to_datetime(datos[columna], utc=True, format="ISO8601")
    return datos


@dataclass(frozen=True)
class EstadoBot:
    """Foto del bot para la cabecera del panel."""

    existe: bool
    en_marcha: bool = False
    latido: pd.Timestamp | None = None
    estado: str = "—"
    motivo: str = ""
    capital: float = 0.0
    efectivo: float = 0.0
    maximo: float = 0.0
    ultima_vela_cierre: pd.Timestamp | None = None
    ultimo_precio: float | None = None
    posicion: dict[str, object] | None = None

    @property
    def caida(self) -> float:
        return self.capital / self.maximo - 1 if self.maximo else 0.0

    @property
    def latente(self) -> float | None:
        """Resultado no realizado de la posición al último cierre (comisión de entrada incluida)."""
        if not self.posicion or self.ultimo_precio is None:
            return None
        cantidad = float(self.posicion["cantidad"])  # type: ignore[arg-type]
        coste = cantidad * float(self.posicion["precio_entrada"]) + float(  # type: ignore[arg-type]
            self.posicion["comision_entrada"]  # type: ignore[arg-type]
        )
        return cantidad * self.ultimo_precio - coste


def leer_estado(config: Config, modo: str, ahora_ms: int) -> EstadoBot:
    ruta = config.ruta_db_estado(modo)
    if not ruta.exists():
        return EstadoBot(existe=False)
    con = conexion_lectura(ruta)
    try:
        meta = dict(con.execute("SELECT clave, valor FROM meta").fetchall())
        cuenta = con.execute(
            "SELECT efectivo, equity_maximo, ultimo_equity, estado, motivo_estado FROM cuenta"
        ).fetchone()
        posicion = con.execute(
            "SELECT cantidad, precio_entrada, stop, objetivo, fecha_entrada, comision_entrada "
            "FROM posicion"
        ).fetchone()
        precio = con.execute("SELECT precio FROM capital ORDER BY fecha DESC LIMIT 1").fetchone()
    finally:
        con.close()
    if cuenta is None:
        return EstadoBot(existe=False)
    latido = int(meta.get("latido") or 0)
    en_marcha = bool(latido) and ahora_ms - latido < 3 * config.bot.intervalo_s * 1000
    ultima = meta.get("ultima_vela")
    tf = timeframe_a_ms(config.mercado.temporalidad)
    return EstadoBot(
        existe=True,
        en_marcha=en_marcha,
        latido=ms_a_fecha(latido) if latido else None,
        estado=cuenta[3],
        motivo=cuenta[4],
        capital=float(cuenta[2]),
        efectivo=float(cuenta[0]),
        maximo=float(cuenta[1]),
        ultima_vela_cierre=ms_a_fecha(int(ultima) + tf) if ultima else None,
        ultimo_precio=float(precio[0]) if precio else None,
        posicion=(
            dict(
                zip(
                    (
                        "cantidad",
                        "precio_entrada",
                        "stop",
                        "objetivo",
                        "fecha_entrada",
                        "comision_entrada",
                    ),
                    posicion,
                    strict=True,
                )
            )
            if posicion
            else None
        ),
    )


def niveles_estrategia(
    velas: pd.DataFrame, nombre: str, parametros: dict[str, object]
) -> pd.DataFrame:
    """Líneas de referencia de la estrategia para dibujar sobre el precio.

    - ``tendencia_media``: la media y las bandas de entrada (superior) y salida (inferior).
    - ``cruce_medias_rsi``: las dos medias.
    - Otras: ninguna.
    """
    cierre = velas["cierre"]
    lineas = pd.DataFrame(index=velas.index)
    if nombre == "tendencia_media":
        periodo = int(parametros.get("periodo", 100))  # type: ignore[call-overload]
        banda = float(parametros.get("banda", 0.0))  # type: ignore[arg-type]
        media = sma(cierre, periodo)
        lineas[f"Media {periodo}"] = media
        if banda > 0:
            lineas["Entrada (banda superior)"] = media * (1 + banda)
            lineas["Salida (banda inferior)"] = media * (1 - banda)
    elif nombre == "cruce_medias_rsi":
        for clave, defecto in (("media_rapida", 20), ("media_lenta", 50)):
            periodo = int(parametros.get(clave, defecto))  # type: ignore[call-overload]
            lineas[f"Media {periodo}"] = sma(cierre, periodo)
    return lineas


@dataclass(frozen=True)
class Informe:
    tipo: str  # backtest | walkforward
    estrategia: str
    fecha: pd.Timestamp
    ruta: Path

    @property
    def titulo(self) -> str:
        return f"{self.fecha:%Y-%m-%d %H:%M} · {self.tipo} · {self.estrategia}"


_PATRON_INFORME = re.compile(r"^(backtest|walkforward)_(.+)_(\d{8}-\d{6})\.html$")


def listar_informes(carpeta: Path) -> list[Informe]:
    """Informes HTML generados por el laboratorio, del más reciente al más antiguo."""
    if not carpeta.exists():
        return []
    informes = []
    for ruta in carpeta.glob("*.html"):
        coincidencia = _PATRON_INFORME.match(ruta.name)
        if coincidencia:
            tipo, estrategia, marca = coincidencia.groups()
            fecha = pd.Timestamp(pd.to_datetime(marca, format="%Y%m%d-%H%M%S"), tz="UTC")
            informes.append(Informe(tipo, estrategia, fecha, ruta))
    return sorted(informes, key=lambda i: i.fecha, reverse=True)


def html_autonomo(ruta: Path) -> str:
    """El informe con la librería de gráficos incrustada (para mostrarlo dentro del panel)."""
    html = ruta.read_text(encoding="utf-8")
    libreria = ruta.parent / "plotly.min.js"
    etiqueta = '<script src="plotly.min.js"></script>'
    if etiqueta in html and libreria.exists():
        html = html.replace(etiqueta, f"<script>{libreria.read_text(encoding='utf-8')}</script>")
    return html
