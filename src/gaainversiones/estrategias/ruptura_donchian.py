"""Ruptura del canal de Donchian (al estilo de las "tortugas").

Idea: comprar fuerza. Un nuevo máximo de N días suele indicar el comienzo o la
continuación de una tendencia; se sale cuando el precio marca un mínimo de M días.

- COMPRAR: el cierre supera el máximo de las ``entrada`` velas anteriores (sin contar la
  actual) y en la vela anterior no lo superaba.
- VENDER: el cierre cae por debajo del mínimo de las ``salida`` velas anteriores.

Pensada para velas diarias y sin take-profit (``riesgo.tp_atr = 0``).
"""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from gaainversiones.estrategias.base import Estrategia, ParametrosEstrategia, Senal, registrar


class ParametrosDonchian(ParametrosEstrategia):
    entrada: int = Field(55, ge=2, le=500)
    salida: int = Field(20, ge=2, le=500)

    @model_validator(mode="after")
    def _salida_mas_corta(self) -> ParametrosDonchian:
        if self.salida >= self.entrada:
            raise ValueError("salida debe ser menor que entrada")
        return self


@registrar
class RupturaDonchian(Estrategia):
    nombre = "ruptura_donchian"
    Parametros = ParametrosDonchian

    @property
    def p(self) -> ParametrosDonchian:
        return cast(ParametrosDonchian, self.parametros)

    @property
    def velas_necesarias(self) -> int:
        return self.p.entrada + 2

    def calcular(self, velas: pd.DataFrame) -> pd.Series:
        cierre = velas["cierre"]
        techo = velas["maximo"].rolling(self.p.entrada, min_periods=self.p.entrada).max().shift(1)
        suelo = velas["minimo"].rolling(self.p.salida, min_periods=self.p.salida).min().shift(1)
        rompe = (cierre > techo).to_numpy()  # NaN -> False
        rompia = np.concatenate([[False], rompe[:-1]])
        pierde = (cierre < suelo).to_numpy()
        senales = np.where(
            pierde, Senal.VENDER, np.where(rompe & ~rompia, Senal.COMPRAR, Senal.MANTENER)
        )
        return self.serie_de_senales(senales, velas.index)
