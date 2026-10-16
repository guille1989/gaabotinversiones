"""Datos sintéticos y un exchange falso para los tests."""

from __future__ import annotations

from typing import Any, cast

import ccxt
import numpy as np
import pandas as pd

from gaainversiones.config import ConfigRiesgo
from gaainversiones.datos.fuentes import velas_a_dataframe
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.estrategias.base import Estrategia, ParametrosEstrategia, Senal, registrar
from gaainversiones.riesgo.gestor import GestorRiesgo, LimitesMercado

HORA_MS = 3_600_000
COSTES = ModeloCostes(comision_maker=0.0025, comision_taker=0.004, deslizamiento=0.0005)
LIMITES = LimitesMercado(cantidad_minima=0.00005, importe_minimo=0.5, decimales_cantidad=8)
MEDIANOCHE = pd.Timestamp("2024-01-01", tz="UTC")
# Inicio alineado a la hora (14-nov-2023 22:00 UTC).
INICIO_MS = 1_700_000_000_000 - 1_700_000_000_000 % HORA_MS


def generar_filas(
    n: int, *, inicio: int = INICIO_MS, tf_ms: int = HORA_MS, semilla: int = 1
) -> list[list[float]]:
    """Velas ``[ts, o, h, l, c, v]`` coherentes de un paseo aleatorio reproducible."""
    rng = np.random.default_rng(semilla)
    cierre = 30_000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    apertura = np.concatenate([[30_000.0], cierre[:-1]])
    maximo = np.maximum(apertura, cierre) * (1 + rng.uniform(0, 0.005, n))
    minimo = np.minimum(apertura, cierre) * (1 - rng.uniform(0, 0.005, n))
    volumen = rng.uniform(1, 10, n)
    return [
        [float(inicio + i * tf_ms), apertura[i], maximo[i], minimo[i], cierre[i], volumen[i]]
        for i in range(n)
    ]


def velas_aleatorias(
    n: int, *, tf_ms: int = HORA_MS, semilla: int = 1, inicio: int = INICIO_MS
) -> pd.DataFrame:
    """Velas sintéticas en el formato común."""
    return velas_a_dataframe(generar_filas(n, inicio=inicio, tf_ms=tf_ms, semilla=semilla))


def velas_manuales(
    ohlc: list[tuple[float, float, float, float]],
    *,
    inicio: pd.Timestamp = MEDIANOCHE,
    tf_ms: int = HORA_MS,
) -> pd.DataFrame:
    """Velas escritas a mano como (apertura, máximo, mínimo, cierre), volumen 1."""
    base = int(inicio.value // 1_000_000)
    return velas_a_dataframe(
        [[base + i * tf_ms, o, h, lo, c, 1.0] for i, (o, h, lo, c) in enumerate(ohlc)]
    )


def gestor(**cambios: Any) -> GestorRiesgo:
    """Capa de riesgo de test: ATR(2), 1 % por operación, stop 2 ATR, objetivo 3 ATR."""
    return GestorRiesgo(ConfigRiesgo(**{"atr_periodo": 2, **cambios}), COSTES, LIMITES)


class ParametrosFijos(ParametrosEstrategia):
    compras: tuple[int, ...] = ()
    ventas: tuple[int, ...] = ()


@registrar
class SenalesFijas(Estrategia):
    """Estrategia de test: compra y vende en las posiciones de vela indicadas."""

    nombre = "senales_fijas_test"
    Parametros = ParametrosFijos

    @property
    def velas_necesarias(self) -> int:
        return 1

    def calcular(self, velas: pd.DataFrame) -> pd.Series:
        p = cast(ParametrosFijos, self.parametros)
        valores = np.full(len(velas), Senal.MANTENER, dtype=object)
        for i in p.compras:
            if i < len(velas):
                valores[i] = Senal.COMPRAR
        for i in p.ventas:
            if i < len(velas):
                valores[i] = Senal.VENDER
        return self.serie_de_senales(valores, velas.index)


class ExchangeFalso:
    """Imita ``fetch_ohlcv`` de ccxt con distintos comportamientos de exchanges reales.

    Args:
        filas: velas disponibles en el "exchange" (la última puede estar en curso).
        tope: máximo de velas por respuesta aunque se pida más.
        ignora_since: devuelve siempre las últimas velas (como la API de Kraken).
        ventana: solo devuelve velas en [since, since + limit*tf) (como Bitstamp).
        solape: incluye velas anteriores a ``since`` (respuestas solapadas).
        fallos: número de llamadas iniciales que lanzan ``error``.
    """

    def __init__(
        self,
        filas: list[list[float]],
        *,
        id: str = "falso",
        tope: int | None = None,
        ignora_since: bool = False,
        ventana: bool = False,
        solape: int = 0,
        fallos: int = 0,
        error: type[Exception] = ccxt.NetworkError,
        tf_ms: int = HORA_MS,
    ) -> None:
        self.id = id
        self.filas = sorted(filas, key=lambda f: f[0])
        self.tope = tope
        self.ignora_since = ignora_since
        self.ventana = ventana
        self.solape = solape
        self.fallos = fallos
        self.error = error
        self.tf_ms = tf_ms
        self.llamadas: list[int | None] = []

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[Any]]:
        self.llamadas.append(since)
        if self.fallos > 0:
            self.fallos -= 1
            raise self.error("fallo simulado")
        n = min(x for x in (limit, self.tope, 10_000) if x is not None)
        if self.ignora_since or since is None:
            return [list(f) for f in self.filas[-n:]]
        if self.ventana:
            fin = since + n * self.tf_ms
            return [list(f) for f in self.filas if since <= f[0] < fin]
        posicion = next((i for i, f in enumerate(self.filas) if f[0] >= since), len(self.filas))
        posicion = max(0, posicion - self.solape)
        return [list(f) for f in self.filas[posicion : posicion + n]]
