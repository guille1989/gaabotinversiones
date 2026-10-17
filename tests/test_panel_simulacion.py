"""Textos y cálculos de la pestaña Simulaciones del panel."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from gaainversiones.backtest.informe import FILAS_METRICAS
from gaainversiones.backtest.metricas import Metricas
from gaainversiones.panel.simulacion import (
    GLOSARIO,
    Requisito,
    checklist_real,
    comparar_con_paper,
    nombre_estrategia,
    veredicto,
)

BASE = Metricas(
    capital_inicial=1000,
    capital_final=5000,
    rentabilidad_total=4.0,
    cagr=0.2,
    caida_maxima=-0.25,
    sharpe=1.2,
    sortino=1.5,
    calmar=0.8,
    operaciones=30,
    pct_ganadoras=0.4,
    profit_factor=1.8,
    ganancia_media=100.0,
    perdida_media=-30.0,
    exposicion=0.4,
    comisiones=50.0,
    dias=3000.0,
)


def _ref(**cambios: float) -> Metricas:
    return replace(BASE, referencia=True, **cambios)


def test_veredicto_gana_menos_con_menos_riesgo() -> None:
    titular, detalle, nivel = veredicto(BASE, _ref(capital_final=80_000, caida_maxima=-0.8))
    assert titular.startswith("Ganó menos que mantener") and nivel == "info"
    assert "-25,0 %" in detalle and "-80,0 %" in detalle and "3,2 veces" in detalle


@pytest.mark.parametrize(
    ("estrategia", "referencia", "nivel"),
    [
        ({"capital_final": 9000}, {"caida_maxima": -0.6}, "success"),
        ({"capital_final": 9000, "caida_maxima": -0.7}, {"caida_maxima": -0.3}, "warning"),
        ({"capital_final": 100}, {"caida_maxima": -0.1}, "warning"),
    ],
)
def test_veredicto_otros_casos(
    estrategia: dict[str, float], referencia: dict[str, float], nivel: str
) -> None:
    _, _, obtenido = veredicto(replace(BASE, **estrategia), _ref(**referencia))
    assert obtenido == nivel


def test_veredicto_avisa_si_hay_pocas_operaciones() -> None:
    _, detalle, _ = veredicto(replace(BASE, operaciones=5), _ref(capital_final=80_000))
    assert "solo hubo 5 operaciones" in detalle
    _, detalle, _ = veredicto(BASE, _ref(capital_final=80_000, caida_maxima=-0.8))
    assert "operaciones; con tan pocas" not in detalle


def test_veredicto_sin_caida_no_divide_por_cero() -> None:
    sin_caida = replace(BASE, caida_maxima=-0.0)
    titular, detalle, _ = veredicto(sin_caida, _ref(capital_final=80_000, caida_maxima=-0.5))
    assert titular and "veces" not in detalle


def test_el_glosario_cubre_todas_las_metricas() -> None:
    assert {etiqueta for _, etiqueta, _ in FILAS_METRICAS} <= GLOSARIO.keys()


def test_nombres_de_estrategia() -> None:
    assert nombre_estrategia("tendencia_media") == "Tendencia con media móvil"
    assert nombre_estrategia("otra_cosa_nueva") == "Otra cosa nueva"


def _requisitos(**cambios: object) -> list[Requisito]:
    base: dict[str, object] = {
        "dias": 45.0,
        "operaciones": 8,
        "caida_paper": -0.2,
        "caida_simulada": -0.35,
        "errores_recientes": 0,
        "en_marcha": True,
        "diferencia": 0.02,
    }
    return checklist_real(**{**base, **cambios})  # type: ignore[arg-type]


def test_checklist_completo() -> None:
    assert all(r.cumplido for r in _requisitos())


@pytest.mark.parametrize(
    ("cambio", "fallan"),
    [
        ({"dias": 10.0}, [0, 2]),  # con pocos días tampoco se puede comparar con la simulación
        ({"operaciones": 2}, [1]),
        ({"diferencia": 0.2}, [2]),
        ({"caida_paper": -0.5}, [3]),
        ({"errores_recientes": 1}, [4]),
        ({"en_marcha": False}, [5]),
    ],
)
def test_checklist_marca_el_requisito_que_falla(
    cambio: dict[str, object], fallan: list[int]
) -> None:
    requisitos = _requisitos(**cambio)
    assert [i for i, r in enumerate(requisitos) if not r.cumplido] == fallan


def test_checklist_sin_datos_no_compara() -> None:
    parecido = _requisitos(diferencia=None)[2]
    assert not parecido.cumplido and "30 días" in parecido.detalle


def test_comparar_con_pocos_datos_devuelve_none() -> None:
    capital = pd.DataFrame({"fecha": [pd.Timestamp("2026-01-01", tz="UTC")], "equity": [1000.0]})
    assert comparar_con_paper(None, capital) is None  # type: ignore[arg-type]
