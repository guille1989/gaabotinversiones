"""Capa de riesgo y modelo de costes."""

from __future__ import annotations

import math

import pytest

from gaainversiones.config import ConfigCostes
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.riesgo.gestor import LimitesMercado
from tests.utiles import COSTES, gestor

D, T = 0.0005, 0.004  # deslizamiento y comisión taker de COSTES


def test_modelo_costes() -> None:
    costes = ModeloCostes.desde_config(ConfigCostes(deslizamiento_pb=5))
    assert costes.deslizamiento == pytest.approx(0.0005)
    assert costes.precio_compra_mercado(100) == pytest.approx(100.05)
    assert costes.precio_venta_mercado(100) == pytest.approx(99.95)
    assert costes.comision(1000) == pytest.approx(4.0)
    assert costes.comision(1000, maker=True) == pytest.approx(2.5)
    assert costes.coste_ida_y_vuelta == pytest.approx(0.009)


def test_tamano_por_riesgo_incluye_costes() -> None:
    decision = gestor().evaluar_entrada(capital=1000, efectivo=1000, precio=100, atr=2)
    entrada = 100 * (1 + D)
    perdida_unidad = entrada * (1 + T) - (entrada - 4) * (1 - D) * (1 - T)
    assert decision.aprobada
    assert decision.cantidad == pytest.approx(math.floor(10 / perdida_unidad * 1e8) / 1e8)
    assert (decision.distancia_stop, decision.distancia_objetivo) == (4, 6)
    # Si salta el stop se pierde, como mucho, el 1 % del capital (10 EUR).
    assert decision.cantidad * perdida_unidad == pytest.approx(10, abs=1e-6)


def test_recorte_por_exposicion_y_efectivo() -> None:
    riesgo = gestor(riesgo_por_operacion=0.02, sl_atr=0.5, exposicion_max=0.5)
    decision = riesgo.evaluar_entrada(capital=1000, efectivo=1000, precio=100, atr=2)
    coste_unidad = 100 * (1 + D) * (1 + T)
    assert decision.cantidad == pytest.approx(math.floor(500 / coste_unidad * 1e8) / 1e8)
    assert "recortada" in decision.motivo
    poco_efectivo = riesgo.evaluar_entrada(capital=1000, efectivo=100, precio=100, atr=2)
    assert poco_efectivo.cantidad * coste_unidad <= 100


@pytest.mark.parametrize(
    ("capital", "precio", "atr", "motivo"),
    [
        (1000, 100, float("nan"), "ATR"),
        (1000, 100, 0, "ATR"),
        (1000, 100, 60, "por debajo de cero"),  # stop a 2 x 60 = 120 > precio
        (1000, 0, 2, "no válidos"),
        (0.01, 100, 2, "mínimo"),  # capital diminuto: orden por debajo del mínimo
    ],
)
def test_vetos(capital: float, precio: float, atr: float, motivo: str) -> None:
    decision = gestor().evaluar_entrada(capital=capital, efectivo=capital, precio=precio, atr=atr)
    assert not decision.aprobada
    assert motivo in decision.motivo


def test_limites_de_mercado() -> None:
    limites = LimitesMercado(cantidad_minima=0.0001, importe_minimo=5, decimales_cantidad=4)
    assert limites.redondear(0.123456) == 0.1234  # siempre hacia abajo
    assert limites.redondear(0.3) == 0.3  # sin error de coma flotante (0.29999...)
    assert not limites.admite(0.00009, 100_000)  # por debajo de la cantidad mínima
    assert not limites.admite(0.0001, 1_000)  # 0,10 EUR < importe mínimo
    assert limites.admite(0.0001, 100_000)


def test_comprobar_limites() -> None:
    riesgo = gestor(perdida_diaria_max=0.03, caida_max=0.15)
    assert riesgo.comprobar_limites(equity=980, equity_inicio_dia=1000, equity_maximo=1100) == []
    diaria = riesgo.comprobar_limites(equity=965, equity_inicio_dia=1000, equity_maximo=1000)
    assert [p.tipo for p in diaria] == ["perdida_diaria"]
    ambas = riesgo.comprobar_limites(equity=840, equity_inicio_dia=900, equity_maximo=1000)
    assert [p.tipo for p in ambas] == ["caida_maxima", "perdida_diaria"]
    assert "16.0%" in ambas[0].descripcion


def test_costes_compartidos_por_defecto() -> None:
    assert gestor().costes is COSTES
