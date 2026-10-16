"""Ciclo de decisión común al backtest, al paper y al real.

Se ejecuta una vez por vela cerrada y devuelve las órdenes que hay que lanzar en ese
momento (que en la práctica se ejecutan al precio de apertura de la vela siguiente).
Si el backtest y el bot en vivo se comportan distinto, es un bug: los dos pasan por aquí.

Orden de prioridades:

1. Límites de riesgo: si se superan, se cierra la posición y se pausa o detiene el bot.
2. Con posición abierta: se sale si el cierre alcanza el objetivo (take-profit) o si la
   estrategia da señal de venta. El stop-loss no se decide aquí: vive en el exchange y
   se ejecuta dentro de la vela.
3. Sin posición y con el bot activo: si la estrategia quiere comprar, la capa de riesgo
   decide el tamaño o la veta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from gaainversiones.cuenta import EstadoCuenta
from gaainversiones.estrategias.base import Senal
from gaainversiones.riesgo.gestor import GestorRiesgo


@dataclass(frozen=True)
class Orden:
    """Orden a mercado decidida al cierre de una vela."""

    lado: Literal["compra", "venta"]
    cantidad: float
    motivo: str
    # Solo compras: el stop y el objetivo se fijan respecto al precio real de ejecución.
    distancia_stop: float = 0.0
    distancia_objetivo: float = 0.0


@dataclass(frozen=True)
class Decision:
    """Órdenes a lanzar y hechos relevantes para el registro."""

    ordenes: list[Orden] = field(default_factory=list)
    eventos: list[str] = field(default_factory=list)


class Nucleo:
    """Decide qué hacer al cierre de cada vela."""

    def __init__(self, riesgo: GestorRiesgo, *, detener_en_caida_max: bool = True) -> None:
        self.riesgo = riesgo
        self.detener_en_caida_max = detener_en_caida_max

    def al_cerrar_vela(
        self,
        cuenta: EstadoCuenta,
        *,
        fecha_cierre: pd.Timestamp,
        cierre: float,
        senal: Senal | str,
        atr: float,
    ) -> Decision:
        equity = cuenta.registrar_cierre_vela(fecha_cierre, cierre)
        eventos: list[str] = []

        # 1. Límites de riesgo.
        if cuenta.estado != "detenido":
            for parada in self.riesgo.comprobar_limites(
                equity=equity,
                equity_inicio_dia=cuenta.equity_inicio_dia,
                equity_maximo=cuenta.equity_maximo,
            ):
                if parada.tipo == "caida_maxima" and self.detener_en_caida_max:
                    cuenta.estado, cuenta.motivo_estado = "detenido", parada.descripcion
                    eventos.append(f"Bot detenido: {parada.descripcion}")
                    break
                if parada.tipo == "perdida_diaria" and cuenta.estado == "activo":
                    cuenta.estado, cuenta.motivo_estado = "pausado", parada.descripcion
                    eventos.append(f"Bot pausado hasta mañana: {parada.descripcion}")
            if cuenta.estado != "activo" and cuenta.posicion is not None:
                motivo = "parada por riesgo"
                return Decision([Orden("venta", cuenta.posicion.cantidad, motivo)], eventos)
        elif cuenta.posicion is not None:
            # Detenido con posición (p. ej. si falló la venta): se insiste en cerrarla.
            return Decision([Orden("venta", cuenta.posicion.cantidad, "parada por riesgo")])

        # 2. Gestión de la posición abierta.
        posicion = cuenta.posicion
        if posicion is not None:
            if cierre >= posicion.objetivo:
                return Decision([Orden("venta", posicion.cantidad, "take-profit")], eventos)
            if senal == Senal.VENDER:
                return Decision([Orden("venta", posicion.cantidad, "señal de venta")], eventos)
            return Decision([], eventos)

        # 3. Entrada.
        if cuenta.estado == "activo" and senal == Senal.COMPRAR:
            decision = self.riesgo.evaluar_entrada(
                capital=equity, efectivo=cuenta.efectivo, precio=cierre, atr=atr
            )
            if decision.aprobada:
                orden = Orden(
                    "compra",
                    decision.cantidad,
                    "señal de compra",
                    decision.distancia_stop,
                    decision.distancia_objetivo,
                )
                return Decision([orden], eventos)
            eventos.append(f"Entrada vetada por riesgo: {decision.motivo}")
        return Decision([], eventos)
