"""Indicadores: valores calculados a mano, implementaciones de referencia y causalidad."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from gaainversiones import indicadores as ind
from tests.utiles import generar_filas

NAN = float("nan")


def _iguales(a: pd.Series, b: list[float] | np.ndarray, tol: float = 1e-9) -> None:
    np.testing.assert_allclose(
        a.to_numpy(dtype=float), np.asarray(b, dtype=float), rtol=tol, atol=tol, equal_nan=True
    )


# --- Implementaciones de referencia, escritas con bucles simples ---------------------------


def _rma_ref(valores: list[float], n: int) -> list[float]:
    salida = [NAN] * len(valores)
    inicio = next(i for i, v in enumerate(valores) if not math.isnan(v))
    semilla = inicio + n - 1
    if semilla >= len(valores):
        return salida
    salida[semilla] = sum(valores[inicio : semilla + 1]) / n
    for i in range(semilla + 1, len(valores)):
        salida[i] = salida[i - 1] + (valores[i] - salida[i - 1]) / n
    return salida


def _rsi_ref(cierres: list[float], n: int) -> list[float]:
    ganancias = [NAN] + [max(cierres[i] - cierres[i - 1], 0.0) for i in range(1, len(cierres))]
    perdidas = [NAN] + [max(cierres[i - 1] - cierres[i], 0.0) for i in range(1, len(cierres))]
    g, p = _rma_ref(ganancias, n), _rma_ref(perdidas, n)
    salida = []
    for gi, pi in zip(g, p, strict=True):
        if math.isnan(gi):
            salida.append(NAN)
        elif pi == 0:
            salida.append(50.0 if gi == 0 else 100.0)
        else:
            salida.append(100 - 100 / (1 + gi / pi))
    return salida


def _atr_ref(
    maximos: list[float], minimos: list[float], cierres: list[float], n: int
) -> list[float]:
    rangos = [maximos[0] - minimos[0]]
    for i in range(1, len(cierres)):
        rangos.append(
            max(
                maximos[i] - minimos[i],
                abs(maximos[i] - cierres[i - 1]),
                abs(minimos[i] - cierres[i - 1]),
            )
        )
    return _rma_ref(rangos, n)


@pytest.fixture
def velas() -> pd.DataFrame:
    filas = np.array(generar_filas(400, semilla=7))
    return pd.DataFrame(filas[:, 1:], columns=["apertura", "maximo", "minimo", "cierre", "volumen"])


# --- Valores calculados a mano -------------------------------------------------------------


def test_sma_a_mano() -> None:
    _iguales(ind.sma(pd.Series([1.0, 2, 3, 4, 5]), 3), [NAN, NAN, 2, 3, 4])


def test_ema_a_mano() -> None:
    # Semilla = media(1,2,3) = 2; alfa = 0.5 -> 2 + 0.5*(4-2) = 3; 3 + 0.5*(5-3) = 4
    _iguales(ind.ema(pd.Series([1.0, 2, 3, 4, 5]), 3), [NAN, NAN, 2, 3, 4])


def test_rsi_a_mano() -> None:
    # Variaciones +1 -1 +1 -1; periodo 2:
    #   t2: g=0.5, p=0.5 -> 50 | t3: g=0.75, p=0.25 -> 75 | t4: g=0.375, p=0.625 -> 37.5
    _iguales(ind.rsi(pd.Series([1.0, 2, 1, 2, 1]), 2), [NAN, NAN, 50, 75, 37.5])


def test_rsi_casos_limite() -> None:
    assert ind.rsi(pd.Series(np.arange(1.0, 30)), 14).dropna().eq(100).all()
    assert ind.rsi(pd.Series(np.arange(30.0, 1, -1)), 14).dropna().eq(0).all()
    assert ind.rsi(pd.Series([5.0] * 30), 14).dropna().eq(50).all()


def test_rango_verdadero_a_mano() -> None:
    maximo = pd.Series([10.0, 12, 11])
    minimo = pd.Series([8.0, 9, 7])
    cierre = pd.Series([9.0, 11, 8])
    # t0: 10-8 = 2 | t1: max(3, |12-9|, |9-9|) = 3 | t2: max(4, |11-11|, |7-11|) = 4
    _iguales(ind.rango_verdadero(maximo, minimo, cierre), [2, 3, 4])
    # ATR(2): semilla media(2,3) = 2.5; t2: 2.5 + (4-2.5)/2 = 3.25
    _iguales(ind.atr(maximo, minimo, cierre, 2), [NAN, 2.5, 3.25])


# --- Contra las implementaciones de referencia ---------------------------------------------


@pytest.mark.parametrize("periodo", [2, 14, 50])
def test_contra_referencia(velas: pd.DataFrame, periodo: int) -> None:
    cierre = velas["cierre"]
    _iguales(ind.rma(cierre, periodo), _rma_ref(cierre.tolist(), periodo))
    _iguales(ind.rsi(cierre, periodo), _rsi_ref(cierre.tolist(), periodo))
    _iguales(
        ind.atr(velas["maximo"], velas["minimo"], cierre, periodo),
        _atr_ref(velas["maximo"].tolist(), velas["minimo"].tolist(), cierre.tolist(), periodo),
    )
    alfa_ema = 2 / (periodo + 1)
    esperado = [NAN] * (periodo - 1) + [cierre.iloc[:periodo].mean()]
    for x in cierre.iloc[periodo:]:
        esperado.append(esperado[-1] + alfa_ema * (x - esperado[-1]))
    _iguales(ind.ema(cierre, periodo), esperado)


def test_rsi_entre_0_y_100(velas: pd.DataFrame) -> None:
    valores = ind.rsi(velas["cierre"], 14).dropna()
    assert len(valores) == len(velas) - 14
    assert valores.between(0, 100).all()


# --- Causalidad: el futuro no cambia el pasado ---------------------------------------------


@pytest.mark.parametrize("corte", [30, 150, 399])
def test_indicadores_causales(velas: pd.DataFrame, corte: int) -> None:
    """El valor en la vela ``corte`` es el mismo con o sin las velas posteriores."""
    alterado = velas.copy()
    alterado.iloc[corte + 1 :] *= 1.5  # futuro manipulado
    for datos in (velas.iloc[: corte + 1], alterado):
        h, lo, c = datos["maximo"], datos["minimo"], datos["cierre"]
        calculos = {
            "sma": ind.sma(c, 20),
            "ema": ind.ema(c, 20),
            "rsi": ind.rsi(c, 14),
            "atr": ind.atr(h, lo, c, 14),
        }
        h0, l0, c0 = velas["maximo"], velas["minimo"], velas["cierre"]
        completos = {
            "sma": ind.sma(c0, 20),
            "ema": ind.ema(c0, 20),
            "rsi": ind.rsi(c0, 14),
            "atr": ind.atr(h0, l0, c0, 14),
        }
        for nombre, serie in calculos.items():
            _iguales(serie.iloc[: corte + 1], completos[nombre].iloc[: corte + 1].to_numpy())


# --- Errores y encadenado ------------------------------------------------------------------


def test_periodo_no_valido() -> None:
    for periodo in (0, -3, 2.5, True):
        with pytest.raises(ValueError):
            ind.ema(pd.Series([1.0, 2, 3]), periodo)  # type: ignore[arg-type]


def test_nan_intermedio_es_error() -> None:
    with pytest.raises(ValueError, match="NaN intermedios"):
        ind.ema(pd.Series([1.0, 2, NAN, 4, 5]), 2)


def test_encadenar_indicadores_respeta_nan_iniciales() -> None:
    serie = pd.Series(np.arange(1.0, 21))
    doble = ind.ema(ind.sma(serie, 5), 3)
    assert doble.iloc[:6].isna().all()  # 4 NaN de la SMA + 2 de la EMA
    assert doble.iloc[6:].notna().all()


def test_historia_insuficiente_devuelve_nan() -> None:
    assert ind.ema(pd.Series([1.0, 2]), 5).isna().all()
    assert ind.rsi(pd.Series([1.0, 2, 3]), 14).isna().all()
