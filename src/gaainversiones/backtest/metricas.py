"""Métricas de rendimiento de una curva de capital y de sus operaciones.

Convenciones:
    - Rendimientos diarios a partir del capital al final de cada día UTC.
    - Anualización con 365 días (el mercado cripto no cierra).
    - Tasa libre de riesgo 0 en Sharpe y Sortino.
    - La caída máxima es negativa (-0,25 = -25 %).
    - Un valor que no se puede calcular (p. ej. Sharpe sin variación) es NaN.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from gaainversiones.backtest.motor import ResultadoBacktest
from gaainversiones.cuenta import Operacion

DIAS_POR_ANIO = 365
NAN = float("nan")


@dataclass(frozen=True)
class Metricas:
    capital_inicial: float
    capital_final: float
    rentabilidad_total: float
    cagr: float
    caida_maxima: float
    sharpe: float
    sortino: float
    calmar: float
    operaciones: int
    pct_ganadoras: float
    profit_factor: float
    ganancia_media: float
    perdida_media: float
    exposicion: float
    comisiones: float
    dias: float
    referencia: bool = False  # comprar y mantener: sin operaciones que medir

    def valor(self, nombre: str) -> float:
        """Valor de una métrica por nombre (para optimizar)."""
        return float(getattr(self, nombre))

    def como_dict(self) -> dict[str, float]:
        return {k: float(v) for k, v in asdict(self).items()}


def rendimientos_diarios(
    equity: pd.Series, capital_inicial: float, inicio: pd.Timestamp
) -> pd.Series:
    """Rendimientos diarios de una curva de capital (índice: fechas de cierre)."""
    diaria = equity.resample("1D").last().ffill()
    base = pd.Series([capital_inicial], index=pd.DatetimeIndex([inicio.floor("1D")]))
    serie = pd.concat([base, diaria])
    serie = serie[~serie.index.duplicated(keep="last")]
    return serie.pct_change().dropna()


def caida_maxima(equity: pd.Series, capital_inicial: float) -> float:
    """Mayor caída desde un máximo previo (negativa)."""
    valores = np.concatenate([[capital_inicial], equity.to_numpy(dtype=float)])
    maximos = np.maximum.accumulate(valores)
    return float(np.min(valores / maximos - 1))


def calcular_metricas(
    equity: pd.Series,
    capital_inicial: float,
    inicio: pd.Timestamp,
    operaciones: Sequence[Operacion] = (),
    en_mercado: pd.Series | None = None,
    *,
    referencia: bool = False,
) -> Metricas:
    """Calcula todas las métricas de una curva de capital.

    Args:
        equity: capital al cierre de cada vela.
        capital_inicial: capital al empezar (antes de la primera vela).
        inicio: momento en que empieza el periodo (apertura de la primera vela).
        operaciones: operaciones cerradas del periodo.
        en_mercado: booleano por vela, True si había posición al cierre.
        referencia: marca la curva como comprar y mantener (sus métricas por
            operación no se muestran).
    """
    final = float(equity.iloc[-1])
    rentabilidad = final / capital_inicial - 1
    dias = (pd.Timestamp(equity.index[-1]) - inicio) / pd.Timedelta(days=1)
    anios = dias / 365.25
    if final <= 0:
        cagr = -1.0
    elif anios > 0:
        cagr = (final / capital_inicial) ** (1 / anios) - 1
    else:
        cagr = NAN

    r = rendimientos_diarios(equity, capital_inicial, inicio)
    desviacion = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    # Por debajo de esta variación (ruido de coma flotante) el ratio no tiene sentido.
    minimo = 1e-12
    sharpe = float(r.mean()) / desviacion * math.sqrt(DIAS_POR_ANIO) if desviacion > minimo else NAN
    bajista = float(np.sqrt(np.mean(np.minimum(r.to_numpy(), 0.0) ** 2))) if len(r) else 0.0
    sortino = float(r.mean()) / bajista * math.sqrt(DIAS_POR_ANIO) if bajista > minimo else NAN
    caida = caida_maxima(equity, capital_inicial)
    calmar = cagr / abs(caida) if caida < 0 and math.isfinite(cagr) else NAN

    resultados = np.array([op.resultado for op in operaciones], dtype=float)
    ganancias = resultados[resultados > 0]
    perdidas = resultados[resultados <= 0]
    if len(resultados) == 0:
        profit_factor = NAN
    elif perdidas.sum() < 0:
        profit_factor = float(ganancias.sum()) / abs(float(perdidas.sum()))
    else:
        profit_factor = math.inf
    return Metricas(
        capital_inicial=capital_inicial,
        capital_final=final,
        rentabilidad_total=rentabilidad,
        cagr=cagr,
        caida_maxima=caida,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        operaciones=len(resultados),
        pct_ganadoras=float(len(ganancias) / len(resultados)) if len(resultados) else NAN,
        profit_factor=profit_factor,
        ganancia_media=float(ganancias.mean()) if len(ganancias) else NAN,
        perdida_media=float(perdidas.mean()) if len(perdidas) else NAN,
        exposicion=float(en_mercado.mean()) if en_mercado is not None and len(en_mercado) else NAN,
        comisiones=float(sum(op.comisiones for op in operaciones)),
        dias=float(dias),
        referencia=referencia,
    )


def metricas_resultado(
    resultado: ResultadoBacktest,
    desde: pd.Timestamp | None = None,
    hasta: pd.Timestamp | None = None,
) -> tuple[Metricas, Metricas]:
    """Métricas de la estrategia y de comprar y mantener, opcionalmente en un subperiodo.

    El subperiodo se toma de la misma simulación continua: ``desde``/``hasta`` filtran
    las velas por apertura y las operaciones por fecha de entrada.
    """
    curva, referencia = resultado.curva, resultado.referencia
    inicio, capital, capital_ref = resultado.inicio, resultado.capital_inicial, None
    seleccion = np.ones(len(curva), dtype=bool)
    if desde is not None and desde > resultado.inicio:
        previas = curva.index <= desde
        if previas.any():
            capital = float(curva["equity"][previas].iloc[-1])
            capital_ref = float(referencia[previas].iloc[-1])
        seleccion &= curva.index > desde
        inicio = desde
    if hasta is not None:
        seleccion &= curva.index <= hasta
    if not seleccion.any():
        raise ValueError("El subperiodo no contiene velas")
    tramo = curva[seleccion]
    operaciones = [
        op
        for op in resultado.operaciones
        if (desde is None or op.fecha_entrada >= desde)
        and (hasta is None or op.fecha_entrada < hasta)
    ]
    estrategia = calcular_metricas(
        tramo["equity"], capital, inicio, operaciones, tramo["en_mercado"]
    )
    tramo_ref = referencia[seleccion]
    referencia_metricas = calcular_metricas(
        tramo_ref,
        capital_ref if capital_ref is not None else resultado.capital_inicial,
        inicio,
        en_mercado=pd.Series(True, index=tramo_ref.index),
        referencia=True,
    )
    return estrategia, referencia_metricas
