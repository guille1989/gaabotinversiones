"""Motor de backtest: ejecución, costes, stops, paradas por riesgo y ausencia de lookahead."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from gaainversiones.backtest.motor import ResultadoBacktest, comprar_y_mantener, ejecutar_backtest
from gaainversiones.datos.calidad import rellenar_huecos
from gaainversiones.estrategias import Senal, crear_estrategia
from gaainversiones.riesgo.gestor import GestorRiesgo
from tests.utiles import COSTES, HORA_MS, gestor, velas_aleatorias, velas_manuales

D, T = 0.0005, 0.004
PLANA = (100.0, 101.0, 99.0, 100.0)  # rango verdadero 2 -> ATR(2) = 2


def _backtest(
    ohlc: list[tuple[float, float, float, float]] | pd.DataFrame,
    compras: tuple[int, ...] = (),
    ventas: tuple[int, ...] = (),
    riesgo: GestorRiesgo | None = None,
    **kwargs: object,
) -> ResultadoBacktest:
    velas = ohlc if isinstance(ohlc, pd.DataFrame) else velas_manuales(ohlc)
    estrategia = crear_estrategia("senales_fijas_test", {"compras": compras, "ventas": ventas})
    return ejecutar_backtest(velas, estrategia, riesgo or gestor(), "1h", 1000.0, **kwargs)  # type: ignore[arg-type]


def _cantidad_esperada(precio_senal: float = 100.0, atr: float = 2.0) -> float:
    entrada = precio_senal * (1 + D)
    perdida_unidad = entrada * (1 + T) - (entrada - 2 * atr) * (1 - D) * (1 - T)
    return math.floor(10 / perdida_unidad * 1e8) / 1e8


def test_operacion_calculada_a_mano() -> None:
    velas = (
        [PLANA] * 7
        + [(100, 103, 99, 102), (102, 103, 101, 102), (102, 103, 101, 102)]
        + [(102, 103, 101, 102)]
    )
    r = _backtest(velas, compras=(5,), ventas=(8,))
    q = _cantidad_esperada()
    compra, venta = r.ejecuciones
    # Señal al cierre de la vela 5 -> compra en la APERTURA de la 6 (no al cierre de la 5).
    assert compra.fecha == r.inicio + pd.Timedelta(hours=6)
    assert compra.precio == pytest.approx(100 * (1 + D))
    assert compra.cantidad == pytest.approx(q)
    assert compra.comision == pytest.approx(100.05 * q * T)
    # Señal de venta al cierre de la 8 -> venta en la apertura de la 9.
    assert venta.fecha == r.inicio + pd.Timedelta(hours=9)
    assert venta.precio == pytest.approx(102 * (1 - D))
    resultado = 101.949 * q * (1 - T) - 100.05 * q * (1 + T)
    (operacion,) = r.operaciones
    assert operacion.resultado == pytest.approx(resultado)
    assert operacion.comisiones == pytest.approx(compra.comision + venta.comision)
    assert operacion.motivo_salida == "señal de venta"
    assert r.capital_final == pytest.approx(1000 + resultado)
    # Al cierre de la vela 7 la posición se valora al cierre (102).
    efectivo = 1000 - 100.05 * q * (1 + T)
    assert r.curva["equity"].iloc[7] == pytest.approx(efectivo + q * 102)
    assert r.curva["en_mercado"].tolist() == [False] * 6 + [True] * 3 + [False] * 2


def test_stop_dentro_de_la_vela() -> None:
    velas = [PLANA] * 7 + [(99, 100, 95, 97)] + [PLANA] * 2
    r = _backtest(velas, compras=(5,))
    (operacion,) = r.operaciones
    stop = 100.05 - 4
    assert operacion.motivo_salida == "stop-loss"
    assert operacion.precio_salida == pytest.approx(stop * (1 - D))
    # La pérdida es el 1 % del capital: el tamaño ya contaba con los costes.
    assert operacion.resultado == pytest.approx(-10, abs=0.01)


def test_hueco_por_debajo_del_stop_se_ejecuta_a_la_apertura() -> None:
    velas = [PLANA] * 7 + [(90, 91, 88, 89)] + [PLANA] * 2
    (operacion,) = _backtest(velas, compras=(5,)).operaciones
    assert operacion.precio_salida == pytest.approx(90 * (1 - D))
    assert operacion.resultado < -10  # el hueco hace perder más de lo previsto


def test_stop_en_la_vela_de_entrada() -> None:
    velas = [PLANA] * 6 + [(100, 101, 95, 100)] + [PLANA] * 3
    (operacion,) = _backtest(velas, compras=(5,)).operaciones
    assert operacion.fecha_entrada == operacion.fecha_salida
    assert operacion.motivo_salida == "stop-loss"


def test_take_profit_al_cierre_y_venta_en_la_apertura_siguiente() -> None:
    velas = [PLANA] * 7 + [(100, 108, 99, 107), (107.5, 108, 106, 107)] + [PLANA]
    r = _backtest(velas, compras=(5,))
    (operacion,) = r.operaciones
    assert operacion.motivo_salida == "take-profit"
    assert operacion.fecha_salida == r.inicio + pd.Timedelta(hours=8)
    assert operacion.precio_salida == pytest.approx(107.5 * (1 - D))


def test_stop_y_objetivo_en_la_misma_vela_gana_el_stop() -> None:
    velas = [PLANA] * 7 + [(100, 110, 95, 107)] + [PLANA] * 2
    (operacion,) = _backtest(velas, compras=(5,)).operaciones
    assert operacion.motivo_salida == "stop-loss"


def test_liquidacion_al_final() -> None:
    velas = [PLANA] * 7 + [(100, 103, 99, 102)] * 3
    r = _backtest(velas, compras=(5,))
    (operacion,) = r.operaciones
    assert operacion.motivo_salida == "fin del backtest"
    assert operacion.precio_salida == pytest.approx(102 * (1 - D))
    assert r.capital_final == pytest.approx(r.curva["efectivo"].iloc[-1])


def test_vela_sintetica_aplaza_la_orden() -> None:
    velas = velas_manuales([PLANA] * 10)
    velas = rellenar_huecos(velas.drop(velas.index[6]), "1h")
    assert velas["sintetica"].iloc[6]
    r = _backtest(velas, compras=(5,))
    assert r.ejecuciones[0].fecha == velas.index[7]


def test_hueco_al_alza_reduce_la_compra_al_efectivo() -> None:
    riesgo = gestor(riesgo_por_operacion=0.05, perdida_diaria_max=0.1, caida_max=0.2, sl_atr=0.5)
    velas = [PLANA] * 6 + [(150, 151, 149, 150)] + [(150, 151, 149, 150)] * 2
    r = _backtest(velas, compras=(5,), riesgo=riesgo)
    compra = r.ejecuciones[0]
    asequible = math.floor(1000 / (150 * (1 + D) * (1 + T)) * 1e8) / 1e8
    assert compra.cantidad == pytest.approx(asequible)
    assert r.curva["efectivo"].min() >= 0


def _escenario_hueco(riesgo: GestorRiesgo, **kwargs: object) -> ResultadoBacktest:
    """Compra en la vela 6, hueco a 70 en la 7 (pérdida ~6 %), compras en la 8 y la 30."""
    velas = [PLANA] * 7 + [(70, 71, 69, 70)] + [(70, 71, 69, 70)] * 30
    return _backtest(velas, compras=(5, 8, 30), riesgo=riesgo, **kwargs)


def test_perdida_diaria_pausa_hasta_el_dia_siguiente() -> None:
    r = _escenario_hueco(gestor())
    assert any("pausado" in e for _, e in r.eventos)
    compras = [e.fecha for e in r.ejecuciones if e.lado == "compra"]
    # La de la vela 8 (mismo día) no se ejecuta; la de la 30 (día siguiente) sí.
    assert compras == [r.inicio + pd.Timedelta(hours=6), r.inicio + pd.Timedelta(hours=31)]
    assert r.estado_final == "activo"


def test_caida_maxima_detiene_definitivamente() -> None:
    riesgo = gestor(perdida_diaria_max=0.04, caida_max=0.05)
    r = _escenario_hueco(riesgo)
    assert r.estado_final == "detenido"
    assert any("detenido" in e for _, e in r.eventos)
    assert [e.lado for e in r.ejecuciones] == ["compra", "venta"]
    # Con la parada desactivada (solo estudio) vuelve a operar al día siguiente.
    estudio = _escenario_hueco(riesgo, detener_en_caida_max=False)
    assert [e.lado for e in estudio.ejecuciones].count("compra") == 2


def test_comprar_y_mantener() -> None:
    valores = comprar_y_mantener(100, np.array([100.0, 110.0]), 1000, COSTES)
    unidades = 1000 / (100.05 * (1 + T))
    assert valores[0] == pytest.approx(unidades * 100)
    assert valores[-1] == pytest.approx(unidades * 110 * (1 - D) * (1 - T))


def test_periodo_desde_hasta() -> None:
    velas = velas_manuales([PLANA] * 20)
    r = _backtest(velas, desde=velas.index[5], hasta=velas.index[15])
    assert len(r.curva) == 10
    assert r.inicio == velas.index[5]
    assert r.curva.index[0] == velas.index[5] + pd.Timedelta(hours=1)  # índice = cierre
    with pytest.raises(ValueError, match="No hay velas"):
        _backtest(velas, desde=velas.index[-1] + pd.Timedelta(days=1))


# --- Sin lookahead ---------------------------------------------------------------------------


@pytest.fixture
def aleatorio() -> tuple[pd.DataFrame, ResultadoBacktest]:
    velas = velas_aleatorias(1200, semilla=5)
    estrategia = crear_estrategia(
        "cruce_medias_rsi", {"media_rapida": 5, "media_lenta": 20, "rsi_periodo": 5}
    )
    return velas, ejecutar_backtest(velas, estrategia, gestor(atr_periodo=14), "1h", 1000.0)


@pytest.mark.parametrize("corte", [400, 800])
def test_el_futuro_no_cambia_el_pasado(
    aleatorio: tuple[pd.DataFrame, ResultadoBacktest], corte: int
) -> None:
    """Manipular las velas posteriores a ``corte`` no altera nada de lo ocurrido hasta ahí."""
    velas, original = aleatorio
    alteradas = velas.copy()
    rng = np.random.default_rng(99)
    factor = rng.uniform(0.7, 1.4, len(velas) - corte - 1)[:, None]
    alteradas.iloc[corte + 1 :] = alteradas.iloc[corte + 1 :].to_numpy() * factor
    estrategia = crear_estrategia(
        "cruce_medias_rsi", {"media_rapida": 5, "media_lenta": 20, "rsi_periodo": 5}
    )
    manipulado = ejecutar_backtest(alteradas, estrategia, gestor(atr_periodo=14), "1h", 1000.0)

    limite = velas.index[corte]
    antes = [e for e in original.ejecuciones if e.fecha <= limite]
    assert len(antes) >= 6, "el escenario debe tener operaciones antes del corte"
    assert [e for e in manipulado.ejecuciones if e.fecha <= limite] == antes
    pd.testing.assert_frame_equal(
        manipulado.curva.iloc[: corte + 1], original.curva.iloc[: corte + 1]
    )


def test_cada_compra_ocurre_en_la_apertura_tras_la_senal(
    aleatorio: tuple[pd.DataFrame, ResultadoBacktest],
) -> None:
    velas, resultado = aleatorio
    senales = crear_estrategia(
        "cruce_medias_rsi", {"media_rapida": 5, "media_lenta": 20, "rsi_periodo": 5}
    ).calcular(velas)
    compras = [e for e in resultado.ejecuciones if e.lado == "compra"]
    assert compras
    for compra in compras:
        i = velas.index.get_loc(compra.fecha)
        assert senales.iloc[i - 1] == Senal.COMPRAR
        assert compra.precio == pytest.approx(velas["apertura"].iloc[i] * (1 + D))
    for venta in (
        e for e in resultado.ejecuciones if e.motivo in ("señal de venta", "take-profit")
    ):
        i = velas.index.get_loc(venta.fecha)
        assert venta.precio == pytest.approx(velas["apertura"].iloc[i] * (1 - D))


def test_temporalidad_marca_el_cierre() -> None:
    velas = velas_aleatorias(50, tf_ms=4 * HORA_MS)
    estrategia = crear_estrategia("senales_fijas_test", {})
    r = ejecutar_backtest(velas, estrategia, gestor(), "4h", 1000.0)
    assert (r.curva.index - velas.index == pd.Timedelta(hours=4)).all()
