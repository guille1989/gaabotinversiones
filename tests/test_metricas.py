"""Métricas sobre series con resultado conocido."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from gaainversiones.backtest.metricas import caida_maxima, calcular_metricas, metricas_resultado
from gaainversiones.backtest.motor import ejecutar_backtest
from gaainversiones.cuenta import Operacion
from gaainversiones.estrategias import crear_estrategia
from tests.utiles import gestor, velas_aleatorias

INICIO = pd.Timestamp("2024-01-01", tz="UTC")


def _diaria(valores: list[float] | np.ndarray) -> pd.Series:
    indice = pd.DatetimeIndex([INICIO + pd.Timedelta(days=k + 1) for k in range(len(valores))])
    return pd.Series(np.asarray(valores, dtype=float), index=indice)


def _operacion(resultado: float) -> Operacion:
    return Operacion(INICIO, 100, INICIO + pd.Timedelta(days=1), 100, 1, 100, 0.5, resultado, "x")


def test_crecimiento_constante() -> None:
    equity = _diaria(1000 * 1.001 ** np.arange(1, 366))
    m = calcular_metricas(equity, 1000, INICIO)
    assert m.rentabilidad_total == pytest.approx(1.001**365 - 1)
    assert m.cagr == pytest.approx((1.001**365) ** (365.25 / 365) - 1)
    assert m.caida_maxima == 0
    assert math.isnan(m.sharpe)  # sin variación no hay Sharpe
    assert math.isnan(m.calmar)
    assert m.dias == pytest.approx(365)


def test_caida_maxima_conocida() -> None:
    assert caida_maxima(_diaria([120, 90, 130, 117]), 100) == pytest.approx(90 / 120 - 1)
    assert caida_maxima(_diaria([90, 95]), 100) == pytest.approx(-0.10)  # cuenta el capital inicial


def test_sharpe_y_sortino() -> None:
    rendimientos = np.tile([0.01, -0.005], 100)
    equity = _diaria(1000 * np.cumprod(1 + rendimientos))
    m = calcular_metricas(equity, 1000, INICIO)
    esperado = rendimientos.mean() / rendimientos.std(ddof=1) * math.sqrt(365)
    assert m.sharpe == pytest.approx(esperado)
    bajista = math.sqrt(np.mean(np.minimum(rendimientos, 0) ** 2))
    assert m.sortino == pytest.approx(rendimientos.mean() / bajista * math.sqrt(365))


def test_metricas_de_operaciones() -> None:
    operaciones = [_operacion(r) for r in (10, -5, 20, -5)]
    m = calcular_metricas(_diaria([1000, 1020]), 1000, INICIO, operaciones)
    assert m.operaciones == 4
    assert m.pct_ganadoras == 0.5
    assert m.profit_factor == pytest.approx(3.0)
    assert m.ganancia_media == pytest.approx(15)
    assert m.perdida_media == pytest.approx(-5)
    assert m.comisiones == pytest.approx(2.0)
    sin_perdidas = calcular_metricas(_diaria([1000, 1010]), 1000, INICIO, [_operacion(10)])
    assert math.isinf(sin_perdidas.profit_factor)
    vacia = calcular_metricas(_diaria([1000, 1000]), 1000, INICIO)
    assert vacia.operaciones == 0 and math.isnan(vacia.profit_factor)


def test_capital_a_cero() -> None:
    m = calcular_metricas(_diaria([500, 0]), 1000, INICIO)
    assert m.cagr == -1 and m.caida_maxima == -1


def test_subperiodos_encadenan_con_el_total() -> None:
    velas = velas_aleatorias(2000, semilla=8)
    estrategia = crear_estrategia(
        "cruce_medias_rsi", {"media_rapida": 5, "media_lenta": 20, "rsi_periodo": 5}
    )
    r = ejecutar_backtest(
        velas, estrategia, gestor(atr_periodo=14), "1h", 1000.0, detener_en_caida_max=False
    )
    corte = velas.index[1000]
    total, total_ref = metricas_resultado(r)
    dentro, _ = metricas_resultado(r, hasta=corte)
    fuera, fuera_ref = metricas_resultado(r, desde=corte)
    assert (1 + dentro.rentabilidad_total) * (1 + fuera.rentabilidad_total) == pytest.approx(
        1 + total.rentabilidad_total
    )
    assert dentro.operaciones + fuera.operaciones == total.operaciones
    assert fuera_ref.capital_inicial == pytest.approx(
        r.referencia[r.referencia.index <= corte].iloc[-1]
    )
    assert total_ref.referencia and not total.referencia
    assert total.exposicion == pytest.approx(r.curva["en_mercado"].mean())
