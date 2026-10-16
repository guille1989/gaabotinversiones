"""Punto de entrada del backtest: reúne datos, estrategia, riesgo y costes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from gaainversiones.backtest.motor import ResultadoBacktest, ejecutar_backtest
from gaainversiones.config import Config
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.estrategias import Estrategia, crear_estrategia
from gaainversiones.riesgo.gestor import GestorRiesgo, LimitesMercado


@dataclass
class Laboratorio:
    """Ejecuta backtests de una estrategia sobre unas velas, cacheando las señales.

    Las señales de cada combinación de parámetros se calculan una sola vez sobre todo el
    histórico (son causales, así que es equivalente a calcularlas vela a vela) y se
    reutilizan en cada periodo: así la optimización y el walk-forward son rápidos.
    """

    velas: pd.DataFrame
    temporalidad: str
    riesgo: GestorRiesgo
    capital_inicial: float
    nombre_estrategia: str
    parametros_base: dict[str, Any] = field(default_factory=dict)
    detener_en_caida_max: bool = True
    _senales: dict[str, pd.Series] = field(default_factory=dict, repr=False)

    @classmethod
    def desde_config(cls, config: Config, velas: pd.DataFrame) -> Laboratorio:
        riesgo = GestorRiesgo(
            config.riesgo,
            ModeloCostes.desde_config(config.costes),
            LimitesMercado.desde_config(config.mercado),
        )
        return cls(
            velas=velas,
            temporalidad=config.mercado.temporalidad,
            riesgo=riesgo,
            capital_inicial=config.paper.capital_inicial,
            nombre_estrategia=config.estrategia.nombre,
            parametros_base=dict(config.estrategia.parametros),
            detener_en_caida_max=config.backtest.detener_en_caida_max,
        )

    def estrategia(self, cambios: Mapping[str, Any] | None = None) -> Estrategia:
        """Estrategia con los parámetros base y ``cambios`` aplicados encima."""
        return crear_estrategia(self.nombre_estrategia, {**self.parametros_base, **(cambios or {})})

    def senales(self, estrategia: Estrategia) -> pd.Series:
        clave = estrategia.describir()
        if clave not in self._senales:
            self._senales[clave] = estrategia.calcular(self.velas)
        return self._senales[clave]

    def backtest(
        self,
        cambios: Mapping[str, Any] | None = None,
        *,
        desde: pd.Timestamp | None = None,
        hasta: pd.Timestamp | None = None,
        capital: float | None = None,
        equity_maximo: float | None = None,
    ) -> ResultadoBacktest:
        """Backtest con los parámetros base modificados por ``cambios``.

        Raises:
            ValueError: si los parámetros no son válidos o el periodo está vacío.
        """
        estrategia = self.estrategia(cambios)
        return ejecutar_backtest(
            self.velas,
            estrategia,
            self.riesgo,
            self.temporalidad,
            self.capital_inicial if capital is None else capital,
            desde=desde,
            hasta=hasta,
            senales=self.senales(estrategia),
            detener_en_caida_max=self.detener_en_caida_max,
            equity_maximo=equity_maximo,
        )
