"""Modelo de costes de ejecución, compartido por el backtest y el PaperBroker.

Una orden a mercado paga la comisión *taker* y sufre deslizamiento: se compra un poco
más caro y se vende un poco más barato que el precio de referencia.
"""

from __future__ import annotations

from dataclasses import dataclass

from gaainversiones.config import ConfigCostes


@dataclass(frozen=True)
class ModeloCostes:
    """Comisiones (fracción del importe) y deslizamiento (fracción del precio)."""

    comision_maker: float
    comision_taker: float
    deslizamiento: float

    @classmethod
    def desde_config(cls, config: ConfigCostes) -> ModeloCostes:
        return cls(
            comision_maker=config.comision_maker,
            comision_taker=config.comision_taker,
            deslizamiento=config.deslizamiento_pb / 10_000,
        )

    def precio_compra_mercado(self, precio: float) -> float:
        """Precio efectivo de una compra a mercado."""
        return precio * (1 + self.deslizamiento)

    def precio_venta_mercado(self, precio: float) -> float:
        """Precio efectivo de una venta a mercado."""
        return precio * (1 - self.deslizamiento)

    def comision(self, importe: float, *, maker: bool = False) -> float:
        """Comisión en moneda cotizada para una ejecución de ``importe``."""
        return importe * (self.comision_maker if maker else self.comision_taker)

    @property
    def coste_ida_y_vuelta(self) -> float:
        """Coste aproximado (fracción de la posición) de comprar y vender a mercado."""
        return 2 * (self.comision_taker + self.deslizamiento)
