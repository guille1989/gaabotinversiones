"""Estado de la cuenta: efectivo, posición abierta, máximos de capital y estado del bot.

Lo comparten el backtest, el paper y el real. Cada compra o venta se registra como una
``Ejecucion`` (base del registro fiscal) y cada ida y vuelta completa como una
``Operacion`` (base de las estadísticas).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd

EstadoBot = Literal["activo", "pausado", "detenido"]


@dataclass
class Posicion:
    """Posición larga abierta."""

    cantidad: float
    precio_entrada: float
    stop: float
    objetivo: float
    fecha_entrada: pd.Timestamp
    comision_entrada: float
    id_orden_stop: str = ""  # orden de stop-loss en el exchange (en vivo)

    @property
    def coste_entrada(self) -> float:
        """Dinero que salió de la cuenta al abrirla (importe + comisión)."""
        return self.precio_entrada * self.cantidad + self.comision_entrada


@dataclass(frozen=True)
class Ejecucion:
    """Una orden ejecutada."""

    fecha: pd.Timestamp
    lado: Literal["compra", "venta"]
    precio: float
    cantidad: float
    comision: float
    motivo: str
    # En vivo: precio de referencia al decidir (para medir el deslizamiento real) e id de orden.
    precio_referencia: float | None = None
    id_orden: str = ""

    @property
    def importe(self) -> float:
        return self.precio * self.cantidad


@dataclass(frozen=True)
class Operacion:
    """Ida y vuelta completa: entrada y salida de una posición."""

    fecha_entrada: pd.Timestamp
    precio_entrada: float
    fecha_salida: pd.Timestamp
    precio_salida: float
    cantidad: float
    coste_entrada: float
    comisiones: float
    resultado: float  # EUR netos, comisiones incluidas
    motivo_salida: str

    @property
    def rentabilidad(self) -> float:
        """Resultado sobre el dinero invertido."""
        return self.resultado / self.coste_entrada if self.coste_entrada else 0.0

    @property
    def duracion(self) -> pd.Timedelta:
        return self.fecha_salida - self.fecha_entrada


def dia_de_cierre(fecha_cierre: pd.Timestamp) -> date:
    """Día UTC al que pertenece un cierre de vela (el cierre de las 00:00 es del día anterior)."""
    return (fecha_cierre - pd.Timedelta(milliseconds=1)).date()


@dataclass
class EstadoCuenta:
    """Efectivo, posición y contabilidad de riesgo de la cuenta.

    ``estado``:
        - ``activo``: opera con normalidad.
        - ``pausado``: límite de pérdida diaria alcanzado; no abre posiciones hasta el
          siguiente día UTC, en el que se reactiva sola.
        - ``detenido``: caída máxima alcanzada; no vuelve a operar hasta que una persona
          lo reanude.
    """

    efectivo: float
    equity_maximo: float
    equity_inicio_dia: float
    ultimo_equity: float
    posicion: Posicion | None = None
    dia: date | None = None
    estado: EstadoBot = "activo"
    motivo_estado: str = ""

    @classmethod
    def nueva(cls, capital: float) -> EstadoCuenta:
        return cls(
            efectivo=capital,
            equity_maximo=capital,
            equity_inicio_dia=capital,
            ultimo_equity=capital,
        )

    def equity(self, precio: float) -> float:
        """Valor de la cuenta con la posición valorada a ``precio``."""
        return self.efectivo + (self.posicion.cantidad * precio if self.posicion else 0.0)

    def registrar_cierre_vela(self, fecha_cierre: pd.Timestamp, precio: float) -> float:
        """Valora la cuenta al cierre de una vela y lleva la contabilidad diaria.

        Returns:
            El capital (equity) a ese cierre.
        """
        dia = dia_de_cierre(fecha_cierre)
        if self.dia is None:
            self.dia = dia
        elif dia != self.dia:
            # Nuevo día UTC: la referencia es el capital al cierre del día anterior.
            self.dia = dia
            self.equity_inicio_dia = self.ultimo_equity
            if self.estado == "pausado":
                self.estado, self.motivo_estado = "activo", ""
        equity = self.equity(precio)
        self.ultimo_equity = equity
        self.equity_maximo = max(self.equity_maximo, equity)
        return equity

    def aplicar_compra(
        self,
        *,
        fecha: pd.Timestamp,
        precio: float,
        cantidad: float,
        comision: float,
        stop: float,
        objetivo: float,
        motivo: str,
    ) -> Ejecucion:
        """Abre la posición. El llamante garantiza que hay efectivo suficiente."""
        if self.posicion is not None:
            raise RuntimeError("Ya hay una posición abierta")
        coste = precio * cantidad + comision
        if coste > self.efectivo * (1 + 1e-9):
            raise RuntimeError(f"Efectivo insuficiente: {coste:.2f} > {self.efectivo:.2f}")
        self.efectivo -= coste
        self.posicion = Posicion(cantidad, precio, stop, objetivo, fecha, comision)
        return Ejecucion(fecha, "compra", precio, cantidad, comision, motivo)

    def aplicar_venta(
        self, *, fecha: pd.Timestamp, precio: float, comision: float, motivo: str
    ) -> tuple[Ejecucion, Operacion]:
        """Cierra la posición completa."""
        posicion = self.posicion
        if posicion is None:
            raise RuntimeError("No hay posición que cerrar")
        ingreso = precio * posicion.cantidad - comision
        self.efectivo += ingreso
        self.posicion = None
        ejecucion = Ejecucion(fecha, "venta", precio, posicion.cantidad, comision, motivo)
        operacion = Operacion(
            fecha_entrada=posicion.fecha_entrada,
            precio_entrada=posicion.precio_entrada,
            fecha_salida=fecha,
            precio_salida=precio,
            cantidad=posicion.cantidad,
            coste_entrada=posicion.coste_entrada,
            comisiones=posicion.comision_entrada + comision,
            resultado=ingreso - posicion.coste_entrada,
            motivo_salida=motivo,
        )
        return ejecucion, operacion
