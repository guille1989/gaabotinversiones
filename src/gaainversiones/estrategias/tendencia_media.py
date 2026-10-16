"""Seguimiento de tendencia con una media móvil.

Idea: estar comprado solo mientras el precio está por encima de su media de largo plazo,
para participar en las tendencias alcistas y evitar lo peor de los mercados bajistas.

- COMPRAR: el cierre pasa a estar por encima de ``SMA(periodo) * (1 + banda)``.
- VENDER: el cierre pasa a estar por debajo de ``SMA(periodo) * (1 - banda)``.

La banda evita entrar y salir muchas veces cuando el precio ronda la media. Las señales
son eventos (el cruce): tras un stop-loss no se vuelve a entrar hasta un nuevo cruce.
Pensada para velas diarias y sin take-profit (``riesgo.tp_atr = 0``).
"""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd
from pydantic import Field

from gaainversiones.estrategias.base import Estrategia, ParametrosEstrategia, Senal, registrar
from gaainversiones.indicadores import sma


class ParametrosTendenciaMedia(ParametrosEstrategia):
    periodo: int = Field(100, ge=5, le=700)
    banda: float = Field(0.0, ge=0, le=0.2)


@registrar
class TendenciaMedia(Estrategia):
    nombre = "tendencia_media"
    Parametros = ParametrosTendenciaMedia

    @property
    def p(self) -> ParametrosTendenciaMedia:
        return cast(ParametrosTendenciaMedia, self.parametros)

    @property
    def velas_necesarias(self) -> int:
        return self.p.periodo + 1

    def calcular(self, velas: pd.DataFrame) -> pd.Series:
        cierre = velas["cierre"]
        media = sma(cierre, self.p.periodo)
        con_datos = (media.notna() & media.shift(1).notna()).to_numpy()
        encima = (cierre > media * (1 + self.p.banda)).to_numpy()
        debajo = (cierre < media * (1 - self.p.banda)).to_numpy()
        encima_antes = np.concatenate([[False], encima[:-1]])
        debajo_antes = np.concatenate([[False], debajo[:-1]])
        senales = np.where(
            con_datos & encima & ~encima_antes,
            Senal.COMPRAR,
            np.where(con_datos & debajo & ~debajo_antes, Senal.VENDER, Senal.MANTENER),
        )
        return self.serie_de_senales(senales, velas.index)
