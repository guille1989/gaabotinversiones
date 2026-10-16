"""Cruce de medias con filtro RSI.

ESTRATEGIA DIDÁCTICA. Sirve para probar la infraestructura de punta a punta y como
plantilla para escribir estrategias nuevas. No hay ningún motivo para esperar que
gane dinero después de comisiones: no la uses con dinero real por haberla visto aquí.
"""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from gaainversiones.estrategias.base import Estrategia, ParametrosEstrategia, Senal, registrar
from gaainversiones.indicadores import rsi, sma


class ParametrosCruceMediasRsi(ParametrosEstrategia):
    media_rapida: int = Field(20, ge=2)
    media_lenta: int = Field(50, ge=3)
    rsi_periodo: int = Field(14, ge=2)
    rsi_max_entrada: float = Field(70.0, gt=0, le=100)

    @model_validator(mode="after")
    def _rapida_menor_que_lenta(self) -> ParametrosCruceMediasRsi:
        if self.media_rapida >= self.media_lenta:
            raise ValueError("media_rapida debe ser menor que media_lenta")
        return self


@registrar
class CruceMediasRsi(Estrategia):
    """Compra cuando la media rápida cruza al alza la lenta y el RSI no está en sobrecompra.

    - COMPRAR: en esta vela la SMA rápida pasa a estar por encima de la lenta (en la
      anterior no lo estaba) y el RSI es menor que ``rsi_max_entrada``.
    - VENDER: en esta vela la SMA rápida pasa a estar por debajo de la lenta (o igual).
    - MANTENER: en cualquier otro caso.

    Las señales son eventos (el cruce), no estados: si un cruce alcista se descarta
    por RSI alto, no se compra hasta el siguiente cruce.
    """

    nombre = "cruce_medias_rsi"
    Parametros = ParametrosCruceMediasRsi
    didactica = True

    @property
    def p(self) -> ParametrosCruceMediasRsi:
        return cast(ParametrosCruceMediasRsi, self.parametros)

    @property
    def velas_necesarias(self) -> int:
        # La media lenta completa en la vela anterior (para detectar el cruce) y el RSI.
        return max(self.p.media_lenta + 1, self.p.rsi_periodo + 1)

    def calcular(self, velas: pd.DataFrame) -> pd.Series:
        cierre = velas["cierre"]
        rapida = sma(cierre, self.p.media_rapida)
        lenta = sma(cierre, self.p.media_lenta)
        indice_rsi = rsi(cierre, self.p.rsi_periodo)

        encima = (rapida > lenta).to_numpy()
        encima_antes = np.concatenate([[False], encima[:-1]])
        con_datos = (lenta.notna() & lenta.shift(1).notna()).to_numpy()

        cruce_alcista = con_datos & encima & ~encima_antes
        cruce_bajista = con_datos & ~encima & encima_antes
        rsi_permite = (indice_rsi < self.p.rsi_max_entrada).to_numpy()  # NaN -> False

        senales = np.where(
            cruce_alcista & rsi_permite,
            Senal.COMPRAR,
            np.where(cruce_bajista, Senal.VENDER, Senal.MANTENER),
        )
        return self.serie_de_senales(senales, velas.index)
