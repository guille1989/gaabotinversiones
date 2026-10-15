"""Descarga paginada y lectura del CSV de Kraken."""

from __future__ import annotations

import logging
from pathlib import Path

import ccxt
import numpy as np
import pytest

from gaainversiones.datos.fuentes import FuenteCCXT, leer_csv_kraken, velas_a_dataframe
from gaainversiones.tiempo import indice_a_ms
from tests.utiles import HORA_MS, INICIO_MS, ExchangeFalso, generar_filas

N = 2500
FILAS = generar_filas(N)
# "Ahora" cae a mitad de la última vela: está abierta y no debe descargarse.
AHORA = INICIO_MS + (N - 1) * HORA_MS + HORA_MS // 2


def _fuente(exchange: ExchangeFalso, **kwargs: object) -> FuenteCCXT:
    esperas: list[float] = []
    fuente = FuenteCCXT(exchange, reloj=lambda: AHORA, dormir=esperas.append, **kwargs)  # type: ignore[arg-type]
    fuente.esperas = esperas  # type: ignore[attr-defined]
    return fuente


def _comprobar_completa(velas: object, desde_idx: int = 0, hasta_idx: int = N - 1) -> None:
    """Velas [desde_idx, hasta_idx) de FILAS, sin huecos ni duplicados, con sus valores."""
    import pandas as pd

    assert isinstance(velas, pd.DataFrame)
    ts = indice_a_ms(velas.index)
    esperado = np.array([f[0] for f in FILAS[desde_idx:hasta_idx]], dtype="int64")
    np.testing.assert_array_equal(ts, esperado)
    assert not velas.index.has_duplicates
    np.testing.assert_allclose(
        velas.to_numpy(), np.array([f[1:] for f in FILAS[desde_idx:hasta_idx]])
    )


def test_paginacion_completa_y_sin_vela_abierta() -> None:
    exchange = ExchangeFalso(FILAS)
    velas = _fuente(exchange).descargar("BTC/EUR", "1h", INICIO_MS)
    _comprobar_completa(velas)  # todas menos la última, que sigue abierta
    assert len(exchange.llamadas) == 3  # 1000 + 1000 + 500
    assert exchange.llamadas[1] == INICIO_MS + 1000 * HORA_MS  # cada página sigue a la anterior


def test_exchange_con_tope_menor_que_el_limite() -> None:
    velas = _fuente(ExchangeFalso(FILAS, tope=300)).descargar("BTC/EUR", "1h", INICIO_MS)
    _comprobar_completa(velas)


def test_respuestas_solapadas_no_duplican() -> None:
    velas = _fuente(ExchangeFalso(FILAS, solape=5)).descargar("BTC/EUR", "1h", INICIO_MS)
    _comprobar_completa(velas)


def test_rango_parcial_hasta_exclusivo() -> None:
    desde, hasta = INICIO_MS + 100 * HORA_MS, INICIO_MS + 1700 * HORA_MS
    velas = _fuente(ExchangeFalso(FILAS)).descargar("BTC/EUR", "1h", desde, hasta)
    _comprobar_completa(velas, 100, 1700)


def test_tramo_sin_datos_antes_del_alta_del_par() -> None:
    # Se pide desde 3000 velas antes de que existan datos; el exchange responde por ventanas.
    desde = INICIO_MS - 3000 * HORA_MS
    exchange = ExchangeFalso(FILAS, ventana=True)
    velas = _fuente(exchange).descargar("BTC/EUR", "1h", desde)
    _comprobar_completa(velas)


def test_reintenta_errores_de_red() -> None:
    fuente = _fuente(ExchangeFalso(FILAS, fallos=2, error=ccxt.RequestTimeout))
    _comprobar_completa(fuente.descargar("BTC/EUR", "1h", INICIO_MS))
    assert fuente.esperas == [2.0, 4.0]  # type: ignore[attr-defined]


def test_agota_reintentos() -> None:
    fuente = _fuente(ExchangeFalso(FILAS, fallos=10), intentos=3)
    with pytest.raises(ccxt.NetworkError):
        fuente.descargar("BTC/EUR", "1h", INICIO_MS)
    assert len(fuente.esperas) == 2  # type: ignore[attr-defined]


def test_errores_que_no_son_de_red_no_se_reintentan() -> None:
    exchange = ExchangeFalso(FILAS, fallos=1, error=ccxt.BadSymbol)
    with pytest.raises(ccxt.BadSymbol):
        _fuente(exchange).descargar("XXX/EUR", "1h", INICIO_MS)
    assert len(exchange.llamadas) == 1


def test_exchange_que_ignora_since_termina_y_avisa(caplog: pytest.LogCaptureFixture) -> None:
    exchange = ExchangeFalso(FILAS, id="kraken", ignora_since=True, tope=720)
    with caplog.at_level(logging.WARNING):
        velas = _fuente(exchange).descargar("BTC/EUR", "1h", INICIO_MS)
    assert len(velas) == 719  # las 720 últimas menos la abierta
    assert len(exchange.llamadas) == 1
    assert "720" in caplog.text and "CSV" in caplog.text


def test_velas_a_dataframe_ordena_y_elimina_duplicados() -> None:
    filas = [
        [3 * HORA_MS, 1, 1, 1, 1, 1],
        [HORA_MS, 2, 2, 2, 2, 2],
        [3 * HORA_MS, 9, 9, 9, 9, None],
    ]
    velas = velas_a_dataframe(filas)
    assert list(indice_a_ms(velas.index)) == [HORA_MS, 3 * HORA_MS]
    assert velas["apertura"].iloc[1] == 9  # gana la última aparición
    assert np.isnan(velas["volumen"].iloc[1])  # None -> NaN


# --- CSV oficial de Kraken -----------------------------------------------------------------


def _escribir_csv(ruta: Path, filas: list[list[float]], cabecera: bool = False) -> Path:
    lineas = ["timestamp,open,high,low,close,volume,trades"] if cabecera else []
    lineas += [f"{int(f[0] // 1000)},{f[1]},{f[2]},{f[3]},{f[4]},{f[5]},17" for f in filas]
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return ruta


@pytest.mark.parametrize("cabecera", [False, True])
def test_leer_csv_kraken(tmp_path: Path, cabecera: bool) -> None:
    ruta = _escribir_csv(tmp_path / "XBTEUR_60.csv", FILAS[:50], cabecera)
    velas, temporalidad = leer_csv_kraken(ruta)
    assert temporalidad == "1h"
    np.testing.assert_array_equal(indice_a_ms(velas.index), [int(f[0]) for f in FILAS[:50]])
    np.testing.assert_allclose(velas.to_numpy(), np.array([f[1:] for f in FILAS[:50]]))


def test_csv_kraken_deduce_temporalidades() -> None:
    from gaainversiones.tiempo import minutos_a_timeframe

    assert [minutos_a_timeframe(m) for m in (1, 15, 60, 240, 720, 1440, 10080)] == [
        "1m",
        "15m",
        "1h",
        "4h",
        "12h",
        "1d",
        "1w",
    ]


def test_csv_kraken_desalineado(tmp_path: Path) -> None:
    ruta = _escribir_csv(tmp_path / "XBTEUR_240.csv", FILAS[:10])  # velas de 1h como si fueran 4h
    with pytest.raises(ValueError, match="no encajan"):
        leer_csv_kraken(ruta)


def test_csv_kraken_sin_intervalo_en_el_nombre(tmp_path: Path) -> None:
    ruta = _escribir_csv(tmp_path / "datos.csv", FILAS[:10])
    with pytest.raises(ValueError, match="deducir"):
        leer_csv_kraken(ruta)
    velas, _ = leer_csv_kraken(ruta, "1h")
    assert len(velas) == 10
