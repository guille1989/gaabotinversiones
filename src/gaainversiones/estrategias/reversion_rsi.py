"""Reversión a la media con RSI corto dentro de una tendencia alcista (al estilo Connors).

Idea: en un mercado alcista, las caídas bruscas de pocos días tienden a recuperarse.
Se compra el "pánico" corto solo si la tendencia de fondo es alcista.

- COMPRAR: ``RSI(rsi_periodo) < rsi_entrada`` y cierre por encima de ``SMA(tendencia)``.
- VENDER: el cierre supera ``SMA(salida_media)`` (el rebote ya se ha producido).

Opera más que las de tendencia; cuidado con las comisiones en temporalidades cortas.
Pensada para velas diarias y sin take-profit (``riesgo.tp_atr = 0``).
"""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd
from pydantic import Field

from gaainversiones.estrategias.base import Estrategia, ParametrosEstrategia, Senal, registrar
from gaainversiones.indicadores import rsi, sma


class ParametrosReversionRsi(ParametrosEstrategia):
    rsi_periodo: int = Field(3, ge=2, le=30)
    rsi_entrada: float = Field(20.0, gt=0, lt=50)
    tendencia: int = Field(200, ge=10, le=700)
    salida_media: int = Field(5, ge=2, le=50)


@registrar
class ReversionRsi(Estrategia):
    nombre = "reversion_rsi"
    Parametros = ParametrosReversionRsi

    @property
    def p(self) -> ParametrosReversionRsi:
        return cast(ParametrosReversionRsi, self.parametros)

    @property
    def velas_necesarias(self) -> int:
        return max(self.p.tendencia, self.p.rsi_periodo + 1, self.p.salida_media)

    def calcular(self, velas: pd.DataFrame) -> pd.Series:
        cierre = velas["cierre"]
        indice = rsi(cierre, self.p.rsi_periodo)
        tendencia = sma(cierre, self.p.tendencia)
        salida = sma(cierre, self.p.salida_media)
        comprar = ((indice < self.p.rsi_entrada) & (cierre > tendencia)).to_numpy()
        vender = (cierre > salida).to_numpy()
        senales = np.where(vender, Senal.VENDER, np.where(comprar, Senal.COMPRAR, Senal.MANTENER))
        return self.serie_de_senales(senales, velas.index)
