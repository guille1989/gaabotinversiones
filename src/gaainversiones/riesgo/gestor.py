"""Capa de riesgo: decide cuánto comprar, dónde va el stop y cuándo hay que parar.

Manda siempre sobre la estrategia: puede recortar el tamaño de una entrada, vetarla
o forzar el cierre de la posición.

- Tamaño: tal que, si salta el stop, la pérdida (comisiones y deslizamiento incluidos)
  sea ``riesgo_por_operacion`` del capital.
- Stop y objetivo: a ``sl_atr`` y ``tp_atr`` veces el ATR del precio de entrada.
- Exposición: el importe invertido no supera ``exposicion_max`` del capital ni el
  efectivo disponible (spot, sin apalancamiento).
- Paradas: pérdida en el día UTC >= ``perdida_diaria_max`` o caída desde el máximo de
  capital >= ``caida_max``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from gaainversiones.config import ConfigMercado, ConfigRiesgo
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.formato import formato_pct


@dataclass(frozen=True)
class LimitesMercado:
    """Restricciones de tamaño de orden del exchange."""

    cantidad_minima: float = 0.0
    importe_minimo: float = 0.0
    decimales_cantidad: int = 8

    @classmethod
    def desde_config(cls, config: ConfigMercado) -> LimitesMercado:
        return cls(config.cantidad_minima, config.importe_minimo, config.decimales_cantidad)

    def redondear(self, cantidad: float) -> float:
        """Redondea hacia abajo a la precisión del exchange (nunca se compra de más)."""
        factor = 10**self.decimales_cantidad
        return math.floor(round(cantidad * factor, 6)) / factor

    def admite(self, cantidad: float, precio: float) -> bool:
        return (
            cantidad > 0
            and cantidad >= self.cantidad_minima
            and (cantidad * precio >= self.importe_minimo)
        )


@dataclass(frozen=True)
class DecisionEntrada:
    """Resultado de evaluar una entrada propuesta por la estrategia."""

    aprobada: bool
    cantidad: float = 0.0
    distancia_stop: float = 0.0
    distancia_objetivo: float = 0.0
    motivo: str = ""


@dataclass(frozen=True)
class ParadaRiesgo:
    """Límite de riesgo superado."""

    tipo: Literal["perdida_diaria", "caida_maxima"]
    descripcion: str


class GestorRiesgo:
    """Aplica las reglas de riesgo de la configuración."""

    def __init__(self, config: ConfigRiesgo, costes: ModeloCostes, limites: LimitesMercado) -> None:
        self.config = config
        self.costes = costes
        self.limites = limites

    def perdida_por_unidad(self, precio: float, distancia_stop: float) -> float:
        """Pérdida por unidad comprada a ``precio`` si se cierra en el stop, con costes."""
        entrada = self.costes.precio_compra_mercado(precio)
        coste_entrada = entrada * (1 + self.costes.comision_taker)
        salida = self.costes.precio_venta_mercado(entrada - distancia_stop)
        return coste_entrada - salida * (1 - self.costes.comision_taker)

    def evaluar_entrada(
        self, *, capital: float, efectivo: float, precio: float, atr: float
    ) -> DecisionEntrada:
        """Decide si se entra y con qué cantidad, a partir del precio y ATR actuales.

        Args:
            capital: valor total de la cuenta (efectivo + posiciones a precio actual).
            efectivo: dinero disponible para comprar.
            precio: precio de entrada estimado (el cierre de la vela de la señal).
            atr: ATR en la vela de la señal.
        """
        if not math.isfinite(atr) or atr <= 0:
            return DecisionEntrada(False, motivo="ATR no disponible")
        if not math.isfinite(precio) or precio <= 0 or capital <= 0:
            return DecisionEntrada(False, motivo="precio o capital no válidos")
        distancia_stop = self.config.sl_atr * atr
        if distancia_stop >= precio:
            return DecisionEntrada(False, motivo="el stop quedaría por debajo de cero")

        cantidad_por_riesgo = (
            capital * self.config.riesgo_por_operacion
        ) / self.perdida_por_unidad(precio, distancia_stop)
        coste_unidad = self.costes.precio_compra_mercado(precio) * (1 + self.costes.comision_taker)
        cantidad_por_exposicion = capital * self.config.exposicion_max / coste_unidad
        cantidad_por_efectivo = efectivo / coste_unidad

        cantidad = min(cantidad_por_riesgo, cantidad_por_exposicion, cantidad_por_efectivo)
        motivo = "riesgo por operación"
        if cantidad < cantidad_por_riesgo:
            motivo = "recortada por exposición máxima o efectivo disponible"
        cantidad = self.limites.redondear(cantidad)
        if not self.limites.admite(cantidad, precio):
            return DecisionEntrada(False, motivo="tamaño por debajo del mínimo del exchange")
        return DecisionEntrada(
            aprobada=True,
            cantidad=cantidad,
            distancia_stop=distancia_stop,
            distancia_objetivo=self.config.tp_atr * atr,
            motivo=motivo,
        )

    def comprobar_limites(
        self, *, equity: float, equity_inicio_dia: float, equity_maximo: float
    ) -> list[ParadaRiesgo]:
        """Límites superados con el capital actual (lista vacía si ninguno)."""
        paradas: list[ParadaRiesgo] = []
        caida = 1 - equity / equity_maximo if equity_maximo > 0 else 0.0
        if caida >= self.config.caida_max:
            paradas.append(
                ParadaRiesgo(
                    "caida_maxima",
                    f"caída de {formato_pct(caida)} desde el máximo "
                    f"(límite {formato_pct(self.config.caida_max, 0)})",
                )
            )
        perdida = 1 - equity / equity_inicio_dia if equity_inicio_dia > 0 else 0.0
        if perdida >= self.config.perdida_diaria_max:
            paradas.append(
                ParadaRiesgo(
                    "perdida_diaria",
                    f"pérdida de {formato_pct(perdida)} en el día "
                    f"(límite {formato_pct(self.config.perdida_diaria_max, 0)})",
                )
            )
        return paradas
