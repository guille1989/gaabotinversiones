"""Fuentes de velas OHLCV: API de exchanges (vía ccxt) y CSV oficial de Kraken.

Formato común: ``DataFrame`` con índice ``fecha`` (UTC), columnas ``COLUMNAS`` en
float, ordenado y sin marcas de tiempo duplicadas. Solo se devuelven velas cerradas.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from gaainversiones.exchange import con_reintentos
from gaainversiones.tiempo import (
    ahora_ms,
    indice_a_ms,
    minutos_a_timeframe,
    ms_a_fecha,
    ms_a_indice,
    timeframe_a_ms,
    vela_cerrada,
)

log = logging.getLogger(__name__)

COLUMNAS: tuple[str, ...] = ("apertura", "maximo", "minimo", "cierre", "volumen")


def dataframe_vacio() -> pd.DataFrame:
    """DataFrame de velas sin filas, con las columnas e índice estándar."""
    return pd.DataFrame(
        {columna: pd.Series(dtype=float) for columna in COLUMNAS}, index=ms_a_indice([])
    )


def velas_a_dataframe(filas: np.ndarray | Iterable[Sequence[Any]]) -> pd.DataFrame:
    """Convierte filas ``[ts_ms, apertura, maximo, minimo, cierre, volumen]`` al formato común.

    Ordena por fecha y, si una marca de tiempo se repite, se queda con la última aparición.
    Los valores ``None`` se convierten en NaN.
    """
    if isinstance(filas, np.ndarray):
        matriz = filas.astype(float, copy=False)
    else:
        lista = [list(fila[:6]) for fila in filas]
        if not lista:
            return dataframe_vacio()
        matriz = np.array(lista, dtype=float)
    if len(matriz) == 0:
        return dataframe_vacio()
    velas = pd.DataFrame(
        matriz[:, 1:6], columns=list(COLUMNAS), index=ms_a_indice(matriz[:, 0].astype("int64"))
    )
    return velas[~velas.index.duplicated(keep="last")].sort_index()


class FuenteVelas(Protocol):
    """Cualquier origen de velas históricas."""

    nombre: str

    def descargar(
        self, simbolo: str, temporalidad: str, desde_ms: int, hasta_ms: int | None = None
    ) -> pd.DataFrame:
        """Velas cerradas con fecha en ``[desde_ms, hasta_ms)``."""
        ...


class FuenteCCXT:
    """Descarga paginada de velas desde la API de un exchange vía ccxt.

    Pagina hacia delante desde ``desde_ms``: cada petición empieza en la vela siguiente
    a la última recibida, así que no quedan huecos entre páginas ni duplicados.
    Descarta la vela en curso (aún abierta), que ccxt suele devolver como última.
    """

    def __init__(
        self,
        exchange: Any,
        *,
        limite: int = 1000,
        intentos: int = 5,
        reloj: Callable[[], int] = ahora_ms,
        dormir: Callable[[float], None] = time.sleep,
        silenciosa: bool = False,
    ) -> None:
        """``silenciosa``: para el bot, que pide la misma ventana cada vela y no necesita
        avisos de que el histórico empieza más tarde de lo pedido."""
        self.exchange = exchange
        self.nombre: str = exchange.id
        self.limite = limite
        self._intentos = intentos
        self._reloj = reloj
        self._dormir = dormir
        self._silenciosa = silenciosa

    def _pedir_pagina(self, simbolo: str, temporalidad: str, desde_ms: int) -> list[list[Any]]:
        pagina: list[list[Any]] = con_reintentos(
            lambda: self.exchange.fetch_ohlcv(
                simbolo, temporalidad, since=desde_ms, limit=self.limite
            ),
            descripcion=f"{self.nombre} velas {simbolo} {temporalidad}",
            intentos=self._intentos,
            dormir=self._dormir,
        )
        return sorted(pagina, key=lambda fila: fila[0])

    def descargar(
        self, simbolo: str, temporalidad: str, desde_ms: int, hasta_ms: int | None = None
    ) -> pd.DataFrame:
        """Descarga las velas cerradas con fecha en ``[desde_ms, hasta_ms)``."""
        tf_ms = timeframe_a_ms(temporalidad)
        ahora = self._reloj()
        fin = ahora if hasta_ms is None else min(hasta_ms, ahora)
        recogidas: dict[int, list[Any]] = {}
        cursor = desde_ms
        paginas = 0
        while cursor < fin:
            pagina = self._pedir_pagina(simbolo, temporalidad, cursor)
            paginas += 1
            if not pagina:
                # Tramo sin datos (p. ej. antes de que el par cotizara): saltar una página.
                cursor += self.limite * tf_ms
                continue
            for fila in pagina:
                ts = int(fila[0])
                if desde_ms <= ts < fin and vela_cerrada(ts, tf_ms, ahora):
                    recogidas[ts] = list(fila[:6])
            siguiente = int(pagina[-1][0]) + tf_ms
            if siguiente <= cursor:
                log.warning(
                    "%s no avanza al paginar (pedido desde %s); se detiene la descarga",
                    self.nombre,
                    ms_a_fecha(cursor),
                )
                break
            cursor = siguiente
            if paginas % 20 == 0:
                log.info(
                    "%s: %d velas descargadas (hasta %s)",
                    self.nombre,
                    len(recogidas),
                    ms_a_fecha(cursor),
                )

        velas = velas_a_dataframe(recogidas.values())
        incompletas = velas.isna().any(axis=1)
        if incompletas.any():
            log.warning(
                "%s: descartadas %d velas con valores vacíos", self.nombre, int(incompletas.sum())
            )
            velas = velas[~incompletas]
        if (
            not self._silenciosa
            and not velas.empty
            and indice_a_ms(velas.index)[0] > desde_ms + tf_ms
        ):
            pista = (
                " (la API de Kraken solo sirve las últimas 720 velas: importa su CSV oficial)"
                if self.nombre == "kraken"
                else ""
            )
            log.warning(
                "%s no tiene velas desde %s; la primera disponible es %s%s",
                self.nombre,
                ms_a_fecha(desde_ms),
                velas.index[0],
                pista,
            )
        log.log(
            logging.DEBUG if self._silenciosa else logging.INFO,
            "%s %s %s: %d velas en %d peticiones",
            self.nombre,
            simbolo,
            temporalidad,
            len(velas),
            paginas,
        )
        return velas


_PATRON_NOMBRE_KRAKEN = re.compile(r"_(\d+)\.csv$", re.IGNORECASE)


def leer_csv_kraken(ruta: Path, temporalidad: str | None = None) -> tuple[pd.DataFrame, str]:
    """Lee un fichero del histórico OHLCVT oficial de Kraken.

    Formato: sin cabecera; columnas ``timestamp`` (segundos), open, high, low, close,
    volume y trades (esta última se ignora). El nombre lleva el par y el intervalo en
    minutos, p. ej. ``XBTEUR_240.csv`` son velas de 4 horas.

    Returns:
        Las velas en el formato común y la temporalidad deducida o indicada.
    """
    if temporalidad is None:
        coincidencia = _PATRON_NOMBRE_KRAKEN.search(ruta.name)
        if not coincidencia:
            raise ValueError(
                f"No se puede deducir la temporalidad de {ruta.name!r}; indícala expresamente"
            )
        temporalidad = minutos_a_timeframe(int(coincidencia.group(1)))
    tf_ms = timeframe_a_ms(temporalidad)

    tabla = pd.read_csv(ruta, header=None, usecols=range(6), names=["ts", *COLUMNAS])
    tabla = tabla.apply(pd.to_numeric, errors="coerce")
    tabla = tabla[tabla["ts"].notna()]  # descarta una posible fila de cabecera
    if tabla.isna().any().any():
        raise ValueError(f"{ruta.name}: hay filas con valores no numéricos o vacíos")
    if tabla.empty:
        return dataframe_vacio(), temporalidad

    ts_ms = tabla["ts"].to_numpy(dtype="int64") * 1000
    if not temporalidad.endswith("w") and (ts_ms % tf_ms != 0).any():
        raise ValueError(
            f"{ruta.name}: las marcas de tiempo no encajan con velas de {temporalidad}"
        )
    matriz = np.column_stack([ts_ms, tabla[list(COLUMNAS)].to_numpy(dtype=float)])
    return velas_a_dataframe(matriz), temporalidad
