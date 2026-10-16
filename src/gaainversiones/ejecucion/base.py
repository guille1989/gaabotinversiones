"""Interfaz común de los brokers: el bot no sabe si opera en paper o en real.

Modelo de órdenes (el mismo para los dos):

- Entradas y salidas a mercado, cada una con un ``id_cliente`` único: si un fallo de red
  obliga a repetir la petición, el broker no ejecuta dos veces la misma orden.
- Un stop-loss por posición, colocado **en el exchange** (en paper, en el exchange
  simulado). Mientras está abierto bloquea la cantidad: para vender a mercado hay que
  cancelarlo antes, igual que en Kraken.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from gaainversiones.cuenta import Ejecucion
from gaainversiones.riesgo.gestor import LimitesMercado


class ErrorBroker(Exception):
    """Error al operar que no se arregla reintentando."""


class SaldoInsuficiente(ErrorBroker):
    """No hay saldo disponible para la orden."""


@dataclass(frozen=True)
class Saldos:
    base: float  # p. ej. BTC
    cotizada: float  # p. ej. EUR


@dataclass(frozen=True)
class Cotizacion:
    compra: float  # mejor precio al que alguien compra (bid)
    venta: float  # mejor precio al que alguien vende (ask)
    fecha: pd.Timestamp

    @property
    def medio(self) -> float:
        return (self.compra + self.venta) / 2


@dataclass(frozen=True)
class OrdenStop:
    """Stop-loss abierto en el exchange."""

    id: str
    cantidad: float
    precio_stop: float
    creada: pd.Timestamp


class Broker(ABC):
    """Operaciones que el bot necesita del exchange."""

    modo: ClassVar[str]

    @abstractmethod
    def limites(self) -> LimitesMercado:
        """Cantidad mínima, importe mínimo y precisión del par."""

    @abstractmethod
    def cotizacion(self) -> Cotizacion:
        """Mejor compra y venta actuales."""

    @abstractmethod
    def saldos(self) -> Saldos:
        """Saldos totales (incluido lo bloqueado por órdenes abiertas)."""

    @abstractmethod
    def comprar_mercado(self, cantidad: float, *, motivo: str, id_cliente: str) -> Ejecucion:
        """Compra a mercado ``cantidad`` de la moneda base."""

    @abstractmethod
    def vender_mercado(self, cantidad: float, *, motivo: str, id_cliente: str) -> Ejecucion:
        """Vende a mercado ``cantidad`` de la moneda base."""

    @abstractmethod
    def colocar_stop(self, cantidad: float, precio_stop: float, *, id_cliente: str) -> OrdenStop:
        """Coloca un stop-loss de venta en el exchange."""

    @abstractmethod
    def cancelar_stop(self, id_orden: str) -> None:
        """Cancela un stop-loss abierto (no falla si ya no existe)."""

    @abstractmethod
    def ordenes_stop(self) -> list[OrdenStop]:
        """Stops abiertos."""

    @abstractmethod
    def revisar_stops(self, hasta_ms: int | None = None) -> list[tuple[OrdenStop, Ejecucion]]:
        """Stops que se han ejecutado desde la última revisión.

        Args:
            hasta_ms: solo considera lo ocurrido antes de este instante (el resto se
                revisará en la siguiente llamada). ``None``: hasta ahora.
        """
