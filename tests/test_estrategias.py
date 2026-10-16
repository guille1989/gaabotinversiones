"""Estrategias: registro, parámetros, señales y ausencia de lookahead."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gaainversiones.estrategias import (
    REGISTRO,
    ErrorLookahead,
    Estrategia,
    Senal,
    crear_estrategia,
    verificar_sin_lookahead,
)
from gaainversiones.estrategias.cruce_medias_rsi import CruceMediasRsi
from tests.utiles import velas_aleatorias, velas_manuales


def test_registro_y_creacion() -> None:
    assert "cruce_medias_rsi" in REGISTRO
    estrategia = crear_estrategia("cruce_medias_rsi", {"media_rapida": 10})
    assert isinstance(estrategia, CruceMediasRsi)
    assert estrategia.describir() == (
        "cruce_medias_rsi(media_rapida=10, media_lenta=50, rsi_periodo=14, rsi_max_entrada=70.0)"
    )
    assert estrategia.didactica


@pytest.mark.parametrize(
    ("parametros", "mensaje"),
    [
        ({"media_rapida": 50, "media_lenta": 20}, "menor que media_lenta"),
        ({"media_rapida": 1}, "media_rapida"),
        ({"rsi_max_entrada": 120}, "rsi_max_entrada"),
        ({"media_rapda": 10}, "media_rapda"),  # errata
    ],
)
def test_parametros_no_validos(parametros: dict[str, object], mensaje: str) -> None:
    with pytest.raises(ValueError, match=mensaje):
        crear_estrategia("cruce_medias_rsi", parametros)


def test_estrategia_desconocida() -> None:
    with pytest.raises(ValueError, match="Disponibles"):
        crear_estrategia("no_existe")


def _cierres_a_velas(cierres: list[float]) -> pd.DataFrame:
    return velas_manuales([(c, c + 1, c - 1, c) for c in cierres])


def test_cruces_generan_senales() -> None:
    # Medias de 2 y 3 velas: baja, luego sube (cruce alcista) y luego baja (cruce bajista).
    cierres = [10.0, 9, 8, 7, 8, 10, 12, 11, 9, 7, 6]
    estrategia = CruceMediasRsi(media_rapida=2, media_lenta=3, rsi_periodo=2, rsi_max_entrada=100)
    senales = estrategia.calcular(_cierres_a_velas(cierres))
    compras = [i for i, s in enumerate(senales) if s == Senal.COMPRAR]
    ventas = [i for i, s in enumerate(senales) if s == Senal.VENDER]
    # SMA2/SMA3 en i=4: 7.5 vs 7.67 (debajo); i=5: 9 vs 8.33 (encima) -> cruce alcista en 5.
    # i=8: 10 vs 10.67 (debajo; en 7: 11.5 vs 11) -> cruce bajista en 8.
    assert compras == [5]
    assert ventas == [8]


def test_filtro_rsi_bloquea_compras_en_sobrecompra() -> None:
    cierres = [10.0, 9, 8, 7, 8, 10, 12, 11, 9, 7, 6]
    estrategia = CruceMediasRsi(media_rapida=2, media_lenta=3, rsi_periodo=2, rsi_max_entrada=60)
    senales = estrategia.calcular(_cierres_a_velas(cierres))
    assert Senal.COMPRAR not in set(senales)  # el RSI en el cruce es alto: no compra
    assert (senales == Senal.VENDER).sum() == 1  # la venta no depende del RSI


def test_sin_historia_suficiente_mantiene() -> None:
    estrategia = CruceMediasRsi()
    velas = velas_aleatorias(estrategia.velas_necesarias - 1)
    assert (estrategia.calcular(velas) == Senal.MANTENER).all()
    assert estrategia.senal_actual(velas) == Senal.MANTENER


def test_senal_actual_es_la_ultima() -> None:
    estrategia = CruceMediasRsi(media_rapida=5, media_lenta=20, rsi_periodo=5)
    velas = velas_aleatorias(300, semilla=3)
    senales = estrategia.calcular(velas)
    for corte in (100, 200, 300):
        assert estrategia.senal_actual(velas.iloc[:corte]) == senales.iloc[corte - 1]


def test_cruce_medias_no_mira_al_futuro() -> None:
    velas = velas_aleatorias(1500, semilla=11)
    for parametros in ({}, {"media_rapida": 5, "media_lenta": 15, "rsi_periodo": 5}):
        verificar_sin_lookahead(crear_estrategia("cruce_medias_rsi", parametros), velas, puntos=40)


class EstrategiaTramposa(Estrategia):
    """Compra si el cierre SIGUIENTE será mayor: usa el futuro y debe detectarse."""

    nombre = "tramposa_test"

    @property
    def velas_necesarias(self) -> int:
        return 1

    def calcular(self, velas: pd.DataFrame) -> pd.Series:
        sube = (velas["cierre"].shift(-1) > velas["cierre"]).to_numpy()
        valores = np.where(sube, Senal.COMPRAR, Senal.MANTENER)
        return self.serie_de_senales(valores, velas.index)


def test_lookahead_detectado() -> None:
    with pytest.raises(ErrorLookahead, match="tramposa_test"):
        verificar_sin_lookahead(EstrategiaTramposa(), velas_aleatorias(300))
