"""Indicadores técnicos como funciones puras sobre ``pd.Series``.

Todos son causales: el valor en la vela t solo depende de datos hasta t (incluida).
Los primeros valores, sin historia suficiente, son NaN; nunca se rellenan hacia atrás.

Convenciones:
    - EMA: sembrada con la SMA de los ``periodo`` primeros valores y alfa = 2/(periodo+1).
    - RMA (suavizado de Wilder): igual, con alfa = 1/periodo. La usan RSI y ATR.
    - RSI: primer valor en la vela ``periodo`` (hacen falta ``periodo`` variaciones).
    - ATR: el primer rango verdadero es máximo - mínimo (no hay cierre previo);
      primer valor en la vela ``periodo - 1``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _comprobar_periodo(periodo: int) -> None:
    if isinstance(periodo, bool) or not isinstance(periodo, (int, np.integer)) or periodo < 1:
        raise ValueError(f"El periodo debe ser un entero >= 1, no {periodo!r}")


def _media_recursiva(serie: pd.Series, periodo: int, alfa: float) -> pd.Series:
    """Media exponencial y_t = y_{t-1} + alfa * (x_t - y_{t-1}).

    Se siembra con la media simple de los ``periodo`` primeros valores válidos.
    Los NaN iniciales se respetan (permite encadenar indicadores); un NaN intermedio
    es un error de datos y lanza ``ValueError``.
    """
    _comprobar_periodo(periodo)
    valores = serie.to_numpy(dtype=float)
    salida = np.full(len(valores), np.nan)
    validos = np.flatnonzero(~np.isnan(valores))
    if len(validos) == 0:
        return pd.Series(salida, index=serie.index, name=serie.name)
    inicio = int(validos[0])
    if np.isnan(valores[inicio:]).any():
        raise ValueError("La serie tiene NaN intermedios: limpia o rellena los datos antes")
    semilla = inicio + periodo - 1
    if semilla < len(valores):
        cola = valores[semilla:].copy()
        cola[0] = valores[inicio : semilla + 1].mean()
        # ewm(adjust=False) arranca en cola[0] y aplica exactamente la recursión de arriba.
        salida[semilla:] = pd.Series(cola).ewm(alpha=alfa, adjust=False).mean().to_numpy()
    return pd.Series(salida, index=serie.index, name=serie.name)


def sma(serie: pd.Series, periodo: int) -> pd.Series:
    """Media móvil simple."""
    _comprobar_periodo(periodo)
    return serie.rolling(periodo, min_periods=periodo).mean()


def ema(serie: pd.Series, periodo: int) -> pd.Series:
    """Media móvil exponencial (alfa = 2 / (periodo + 1))."""
    return _media_recursiva(serie, periodo, 2.0 / (periodo + 1))


def rma(serie: pd.Series, periodo: int) -> pd.Series:
    """Media suavizada de Wilder (alfa = 1 / periodo)."""
    return _media_recursiva(serie, periodo, 1.0 / periodo)


def rsi(cierre: pd.Series, periodo: int = 14) -> pd.Series:
    """Índice de fuerza relativa de Wilder, entre 0 y 100.

    Si no hay pérdidas en la ventana vale 100; si no hay ni ganancias ni pérdidas, 50.
    """
    variacion = cierre.diff()
    media_ganancia = rma(variacion.clip(lower=0), periodo).to_numpy()
    media_perdida = rma((-variacion).clip(lower=0), periodo).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        valor = np.where(
            media_perdida == 0,
            np.where(media_ganancia == 0, 50.0, 100.0),
            100.0 - 100.0 / (1.0 + media_ganancia / media_perdida),
        )
    valor = np.where(np.isnan(media_ganancia) | np.isnan(media_perdida), np.nan, valor)
    return pd.Series(valor, index=cierre.index, name="rsi")


def rango_verdadero(maximo: pd.Series, minimo: pd.Series, cierre: pd.Series) -> pd.Series:
    """Rango verdadero: max(máx - mín, |máx - cierre previo|, |mín - cierre previo|)."""
    cierre_previo = cierre.shift(1)
    rangos = pd.concat(
        [maximo - minimo, (maximo - cierre_previo).abs(), (minimo - cierre_previo).abs()],
        axis=1,
    )
    return rangos.max(axis=1, skipna=True).rename("rango_verdadero")


def atr(maximo: pd.Series, minimo: pd.Series, cierre: pd.Series, periodo: int = 14) -> pd.Series:
    """Average True Range de Wilder (volatilidad en unidades de precio)."""
    return rma(rango_verdadero(maximo, minimo, cierre), periodo).rename("atr")
