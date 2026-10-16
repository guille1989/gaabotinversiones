"""Rejilla, ventanas, optimización in-sample / out-of-sample y walk-forward."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from gaainversiones.backtest import (
    Laboratorio,
    generar_combinaciones,
    generar_ventanas,
    optimizar_is_oos,
    walk_forward,
)
from gaainversiones.backtest.metricas import calcular_metricas
from gaainversiones.backtest.walkforward import ResultadoCombinacion, elegir_mejor
from tests.utiles import HORA_MS, gestor, velas_aleatorias

INICIO_2023 = int(pd.Timestamp("2023-01-01", tz="UTC").value // 1_000_000)
REJILLA = {"media_rapida": [5, 10], "media_lenta": [20, 40]}


def _laboratorio(semilla: int = 4, riesgo_cambios: dict[str, float] | None = None) -> Laboratorio:
    # Dos años de velas de 4h.
    velas = velas_aleatorias(4380, tf_ms=4 * HORA_MS, semilla=semilla, inicio=INICIO_2023)
    return Laboratorio(
        velas=velas,
        temporalidad="4h",
        riesgo=gestor(atr_periodo=14, **(riesgo_cambios or {})),
        capital_inicial=1000.0,
        nombre_estrategia="cruce_medias_rsi",
        parametros_base={"rsi_periodo": 5},
    )


def test_generar_combinaciones() -> None:
    combinaciones = generar_combinaciones({"a": [1, 2], "b": [3, 4, 5]})
    assert len(combinaciones) == 6
    assert {"a": 2, "b": 4} in combinaciones
    assert generar_combinaciones({}) == [{}]


def test_generar_ventanas() -> None:
    ventanas = generar_ventanas(
        pd.Timestamp("2017-01-01", tz="UTC"), pd.Timestamp("2020-01-01", tz="UTC"), 12, 6
    )
    assert [
        (v.entreno_desde.strftime("%Y-%m"), v.prueba_desde.strftime("%Y-%m")) for v in ventanas
    ] == [
        ("2017-01", "2018-01"),
        ("2017-07", "2018-07"),
        ("2018-01", "2019-01"),
        ("2018-07", "2019-07"),
    ]
    for anterior, siguiente in zip(ventanas, ventanas[1:], strict=False):
        assert anterior.prueba_hasta == siguiente.prueba_desde  # pruebas contiguas
    for v in ventanas:
        assert v.entreno_hasta == v.prueba_desde  # la prueba empieza donde acaba el entreno
    assert ventanas[-1].prueba_hasta == pd.Timestamp("2020-01-01", tz="UTC")


def test_elegir_mejor_respeta_minimo_de_operaciones() -> None:
    def resultado(sharpe: float, operaciones: int) -> ResultadoCombinacion:
        base = calcular_metricas(
            pd.Series([1000.0, 1010.0], index=pd.date_range("2024-01-02", periods=2, tz="UTC")),
            1000,
            pd.Timestamp("2024-01-01", tz="UTC"),
        )
        from dataclasses import replace

        return ResultadoCombinacion(
            {"s": sharpe}, replace(base, sharpe=sharpe, operaciones=operaciones)
        )

    candidatos = [
        resultado(3.0, 2),
        resultado(1.0, 10),
        resultado(float("nan"), 50),
        resultado(2.0, 5),
    ]
    assert elegir_mejor(candidatos, "sharpe", 5) == candidatos[3]
    assert elegir_mejor(candidatos, "sharpe", 100) is None


def test_optimizar_is_oos() -> None:
    lab = _laboratorio()
    combinaciones = [*generar_combinaciones(REJILLA), {"media_rapida": 50, "media_lenta": 20}]
    resultado = optimizar_is_oos(
        lab,
        combinaciones,
        desde=None,
        inicio_oos=pd.Timestamp("2024-01-01", tz="UTC"),
        hasta=None,
        metrica="sharpe",
        operaciones_minimas=3,
    )
    assert resultado.combinaciones_probadas == 5
    assert len(resultado.filas) == 4  # la combinación imposible (rápida > lenta) se omite
    elegibles = [f for f in resultado.filas if f.elegible]
    valores = [f.dentro.sharpe for f in elegibles]
    assert valores == sorted(valores, reverse=True)
    assert resultado.mejor is elegibles[0]
    assert all(f.fuera is not None for f in resultado.filas)


def _wf(lab: Laboratorio) -> object:
    ventanas = generar_ventanas(
        lab.velas.index[0], lab.velas.index[-1] + pd.Timedelta(hours=4), 6, 3
    )
    return walk_forward(
        lab, generar_combinaciones(REJILLA), ventanas, metrica="sharpe", operaciones_minimas=3
    )


def test_walk_forward_encadena_capital_y_curva() -> None:
    lab = _laboratorio()
    r = _wf(lab)
    assert len(r.ventanas) == 6  # 24 meses: 6 de entreno y 6 pruebas de 3 meses
    for anterior, siguiente in zip(r.ventanas, r.ventanas[1:], strict=False):
        assert siguiente.capital_inicial == pytest.approx(anterior.capital_final)
    assert r.curva.index.is_monotonic_increasing and not r.curva.index.has_duplicates
    assert r.curva["equity"].iloc[-1] == pytest.approx(r.ventanas[-1].capital_final)
    assert len(r.referencia) == len(r.curva)
    assert r.curva.index[0] == r.ventanas[0].ventana.prueba_desde + pd.Timedelta(hours=4)
    assert r.metricas.operaciones == len(r.operaciones)
    assert r.combinaciones_probadas == 4


def test_walk_forward_elige_sin_mirar_la_prueba() -> None:
    """Cambiar los datos de la última prueba no cambia ninguna elección."""
    lab = _laboratorio()
    original = _wf(lab)
    ultima = original.ventanas[-1].ventana
    velas = lab.velas.copy()
    tramo = velas.index >= ultima.prueba_desde
    velas.loc[tramo] = velas.loc[tramo].to_numpy() * np.linspace(1, 2, tramo.sum())[:, None]
    alterado = _wf(
        Laboratorio(velas, "4h", lab.riesgo, 1000.0, "cruce_medias_rsi", {"rsi_periodo": 5})
    )
    elecciones = [v.elegida.parametros if v.elegida else None for v in original.ventanas]
    assert [v.elegida.parametros if v.elegida else None for v in alterado.ventanas] == elecciones
    assert original.ventanas[-2].capital_final == pytest.approx(alterado.ventanas[-2].capital_final)


def test_walk_forward_respeta_la_caida_maxima_acumulada() -> None:
    lab = _laboratorio(
        semilla=2,
        riesgo_cambios={
            "riesgo_por_operacion": 0.005,
            "perdida_diaria_max": 0.01,
            "caida_max": 0.02,
        },
    )
    r = _wf(lab)
    notas = [v.nota for v in r.ventanas]
    assert "se detiene por caída máxima" in notas
    parada = notas.index("se detiene por caída máxima")
    assert all(n == "detenido por caída máxima" for n in notas[parada + 1 :])
    for v in r.ventanas[parada + 1 :]:
        assert v.capital_final == v.capital_inicial and v.prueba is None
    assert not math.isnan(r.metricas.caida_maxima)
