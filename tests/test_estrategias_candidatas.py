"""Estrategias candidatas: señales en casos construidos a mano y ausencia de lookahead."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from gaainversiones.estrategias import Senal, crear_estrategia, verificar_sin_lookahead
from tests.utiles import gestor, velas_aleatorias, velas_manuales


def _velas(cierres: list[float]) -> pd.DataFrame:
    return velas_manuales([(c, c + 0.5, c - 0.5, c) for c in cierres])


def _posiciones(senales: pd.Series, senal: Senal) -> list[int]:
    return [i for i, s in enumerate(senales) if s == senal]


@pytest.mark.parametrize(
    ("nombre", "parametros"),
    [
        ("tendencia_media", {"periodo": 20}),
        ("tendencia_media", {"periodo": 50, "banda": 0.02}),
        ("ruptura_donchian", {"entrada": 20, "salida": 10}),
        ("reversion_rsi", {"rsi_periodo": 2, "rsi_entrada": 30, "tendencia": 50}),
    ],
)
def test_no_miran_al_futuro(nombre: str, parametros: dict[str, object]) -> None:
    verificar_sin_lookahead(
        crear_estrategia(nombre, parametros), velas_aleatorias(1200, semilla=13)
    )


def test_tendencia_media() -> None:
    cierres = [10.0] * 5 + [12, 13, 14] + [9, 8]
    senales = crear_estrategia("tendencia_media", {"periodo": 5}).calcular(_velas(cierres))
    # SMA5 en la vela 5 = 10,4 < 12: cruce al alza. En la 8, 9 < SMA5 (12): cruce a la baja.
    assert _posiciones(senales, Senal.COMPRAR) == [5]
    assert _posiciones(senales, Senal.VENDER) == [8]


def test_tendencia_media_con_banda_ignora_cruces_pequenos() -> None:
    cierres = [10.0] * 5 + [10.3, 10.0, 10.3]
    estrategia = crear_estrategia("tendencia_media", {"periodo": 5, "banda": 0.05})
    assert set(estrategia.calcular(_velas(cierres))) == {Senal.MANTENER}


def test_ruptura_donchian() -> None:
    cierres = [10.0, 10.5, 10.2, 10.4, 11.5, 12.0, 11.0, 9.0]
    estrategia = crear_estrategia("ruptura_donchian", {"entrada": 4, "salida": 2})
    senales = estrategia.calcular(_velas(cierres))
    # Vela 4: 11,5 supera el máximo previo (10,5 + 0,5 = 11). La 5 sigue rompiendo: no repite.
    assert _posiciones(senales, Senal.COMPRAR) == [4]
    # Vela 7: 9 pierde el mínimo de las 2 anteriores (10,5).
    assert _posiciones(senales, Senal.VENDER) == [7]
    with pytest.raises(ValueError, match="salida debe ser menor"):
        crear_estrategia("ruptura_donchian", {"entrada": 10, "salida": 10})


def test_reversion_rsi() -> None:
    subida = [100.0 + i for i in range(60)]  # tendencia alcista
    cierres = [*subida, 150.0, 145.0, 165.0]  # dos días de caída fuerte y rebote
    estrategia = crear_estrategia(
        "reversion_rsi", {"rsi_periodo": 2, "rsi_entrada": 20, "tendencia": 50, "salida_media": 3}
    )
    senales = estrategia.calcular(_velas(cierres))
    assert Senal.COMPRAR in set(senales.iloc[60:62])
    assert senales.iloc[62] == Senal.VENDER


def test_sin_take_profit() -> None:
    decision = gestor(tp_atr=0).evaluar_entrada(capital=1000, efectivo=1000, precio=100, atr=2)
    assert decision.aprobada and math.isinf(decision.distancia_objetivo)
