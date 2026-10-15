"""Tests contra APIs reales. Solo se ejecutan con ``pytest --red``."""

from __future__ import annotations

import pytest

from gaainversiones.datos import FuenteCCXT, detectar_huecos, validar_velas
from gaainversiones.exchange import crear_exchange
from gaainversiones.tiempo import ahora_ms, indice_a_ms, timeframe_a_ms

pytestmark = pytest.mark.red


@pytest.mark.parametrize("exchange", ["bitstamp", "kraken"])
def test_descarga_real_ultimos_dias(exchange: str) -> None:
    tf_ms = timeframe_a_ms("1h")
    ahora = ahora_ms()
    velas = FuenteCCXT(crear_exchange(exchange)).descargar("BTC/EUR", "1h", ahora - 72 * tf_ms)
    assert 60 <= len(velas) <= 72
    assert validar_velas(velas, "1h") == []
    assert detectar_huecos(velas, "1h") == []
    assert indice_a_ms(velas.index)[-1] + tf_ms <= ahora  # la última está cerrada
