"""Motor de backtest vela a vela.

Reglas (las mismas que seguirá el bot en vivo):

- Las decisiones se toman al cierre de cada vela con ``Nucleo`` y se ejecutan a mercado
  en la **apertura de la vela siguiente**: nunca al precio de la vela que generó la señal.
- Cada ejecución a mercado paga comisión *taker* y deslizamiento.
- El stop-loss está en el exchange: salta dentro de la vela si el mínimo lo toca. Se
  ejecuta al precio del stop o, si la vela abre ya por debajo (hueco), a la apertura.
  En la vela de entrada también puede saltar.
- El take-profit lo vigila el bot al cierre de cada vela; si se alcanza, vende en la
  apertura siguiente. Si en una vela se tocan el stop y el objetivo, gana el stop.
- En velas sintéticas (rellenos de huecos, sin operaciones reales) no se ejecuta nada:
  las órdenes pendientes esperan a la siguiente vela real.
- Una posición abierta al final del periodo se liquida al último cierre, con costes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from gaainversiones.cuenta import Ejecucion, EstadoCuenta, Operacion
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.estrategias.base import Estrategia
from gaainversiones.indicadores import atr as calcular_atr
from gaainversiones.nucleo import Nucleo, Orden
from gaainversiones.riesgo.gestor import GestorRiesgo
from gaainversiones.tiempo import timeframe_a_ms


@dataclass
class ResultadoBacktest:
    """Todo lo que produce un backtest."""

    estrategia: str
    nombre_estrategia: str
    parametros: dict[str, Any]
    didactica: bool
    temporalidad: str
    capital_inicial: float
    inicio: pd.Timestamp  # apertura de la primera vela del periodo
    curva: pd.DataFrame  # índice: cierre de cada vela; equity, efectivo, precio, en_mercado
    referencia: pd.Series  # comprar y mantener, mismo índice
    operaciones: list[Operacion] = field(default_factory=list)
    ejecuciones: list[Ejecucion] = field(default_factory=list)
    eventos: list[tuple[pd.Timestamp, str]] = field(default_factory=list)
    estado_final: str = "activo"

    @property
    def capital_final(self) -> float:
        return float(self.curva["equity"].iloc[-1])

    @property
    def fin(self) -> pd.Timestamp:
        return pd.Timestamp(self.curva.index[-1])

    def operaciones_df(self) -> pd.DataFrame:
        """Operaciones como tabla (una fila por ida y vuelta)."""
        return pd.DataFrame(
            [
                {
                    "fecha_entrada": op.fecha_entrada,
                    "precio_entrada": op.precio_entrada,
                    "fecha_salida": op.fecha_salida,
                    "precio_salida": op.precio_salida,
                    "cantidad": op.cantidad,
                    "comisiones": op.comisiones,
                    "resultado": op.resultado,
                    "rentabilidad": op.rentabilidad,
                    "motivo_salida": op.motivo_salida,
                }
                for op in self.operaciones
            ]
        )


def comprar_y_mantener(
    apertura_inicial: float, cierres: np.ndarray, capital: float, costes: ModeloCostes
) -> np.ndarray:
    """Valor de comprar todo en la primera apertura y vender en el último cierre, con costes."""
    precio = costes.precio_compra_mercado(apertura_inicial)
    unidades = capital / (precio * (1 + costes.comision_taker))
    valores = unidades * np.asarray(cierres, dtype=float)
    valores[-1] = (
        unidades * costes.precio_venta_mercado(float(cierres[-1])) * (1 - costes.comision_taker)
    )
    return valores


def _ejecutar_orden(
    cuenta: EstadoCuenta,
    orden: Orden,
    fecha: pd.Timestamp,
    apertura: float,
    riesgo: GestorRiesgo,
) -> tuple[Ejecucion | None, Operacion | None, str | None]:
    """Ejecuta una orden a mercado a la apertura. Devuelve (ejecución, operación, evento)."""
    costes = riesgo.costes
    if orden.lado == "compra":
        if cuenta.posicion is not None:
            return None, None, None
        precio = costes.precio_compra_mercado(apertura)
        asequible = riesgo.limites.redondear(
            cuenta.efectivo / (precio * (1 + costes.comision_taker))
        )
        cantidad = min(orden.cantidad, asequible)
        if not riesgo.limites.admite(cantidad, precio):
            return None, None, "Compra cancelada: efectivo insuficiente al precio de apertura"
        ejecucion = cuenta.aplicar_compra(
            fecha=fecha,
            precio=precio,
            cantidad=cantidad,
            comision=costes.comision(precio * cantidad),
            stop=precio - orden.distancia_stop,
            objetivo=precio + orden.distancia_objetivo,
            motivo=orden.motivo,
        )
        return ejecucion, None, None
    if cuenta.posicion is None:
        return None, None, None
    precio = costes.precio_venta_mercado(apertura)
    ejecucion, operacion = cuenta.aplicar_venta(
        fecha=fecha,
        precio=precio,
        comision=costes.comision(precio * cuenta.posicion.cantidad),
        motivo=orden.motivo,
    )
    return ejecucion, operacion, None


def ejecutar_backtest(
    velas: pd.DataFrame,
    estrategia: Estrategia,
    riesgo: GestorRiesgo,
    temporalidad: str,
    capital_inicial: float,
    *,
    desde: pd.Timestamp | None = None,
    hasta: pd.Timestamp | None = None,
    senales: pd.Series | None = None,
    detener_en_caida_max: bool = True,
    equity_maximo: float | None = None,
) -> ResultadoBacktest:
    """Simula la estrategia sobre ``velas``.

    Args:
        velas: histórico completo (las velas anteriores a ``desde`` sirven para calentar
            los indicadores, pero no se opera en ellas).
        estrategia: estrategia a simular.
        riesgo: capa de riesgo (incluye el modelo de costes y los límites del exchange).
        temporalidad: temporalidad de las velas.
        capital_inicial: capital al empezar, en moneda cotizada.
        desde: primera vela (por fecha de apertura) en la que se puede operar.
        hasta: fin exclusivo del periodo (por fecha de apertura).
        senales: señales ya calculadas para ``velas`` (evita recalcularlas).
        detener_en_caida_max: si False, la caída máxima no detiene el bot (solo estudio).
        equity_maximo: máximo de capital previo, para encadenar periodos (walk-forward)
            sin olvidar la caída acumulada.
    """
    costes = riesgo.costes
    duracion = pd.Timedelta(milliseconds=timeframe_a_ms(temporalidad))
    if senales is None:
        senales = estrategia.calcular(velas)
    atr = calcular_atr(
        velas["maximo"], velas["minimo"], velas["cierre"], riesgo.config.atr_periodo
    ).to_numpy()

    fechas = pd.DatetimeIndex(velas.index)
    mascara = np.ones(len(velas), dtype=bool)
    if desde is not None:
        mascara &= fechas >= desde
    if hasta is not None:
        mascara &= fechas < hasta
    seleccion = np.flatnonzero(mascara)
    if len(seleccion) == 0:
        raise ValueError("No hay velas en el periodo pedido")
    i0, i1 = int(seleccion[0]), int(seleccion[-1]) + 1

    apertura = velas["apertura"].to_numpy(dtype=float)
    minimo = velas["minimo"].to_numpy(dtype=float)
    cierre = velas["cierre"].to_numpy(dtype=float)
    sintetica = (
        velas["sintetica"].to_numpy(dtype=bool)
        if "sintetica" in velas
        else np.zeros(len(velas), dtype=bool)
    )
    valores_senal = senales.to_numpy(dtype=object)

    cuenta = EstadoCuenta.nueva(capital_inicial)
    if equity_maximo is not None:
        cuenta.equity_maximo = max(capital_inicial, equity_maximo)
    nucleo = Nucleo(riesgo, detener_en_caida_max=detener_en_caida_max)
    resultado = ResultadoBacktest(
        estrategia=estrategia.describir(),
        nombre_estrategia=estrategia.nombre,
        parametros=estrategia.parametros.model_dump(),
        didactica=estrategia.didactica,
        temporalidad=temporalidad,
        capital_inicial=capital_inicial,
        inicio=pd.Timestamp(fechas[i0]),
        curva=pd.DataFrame(),
        referencia=pd.Series(dtype=float),
    )

    n = i1 - i0
    equity = np.empty(n)
    efectivo = np.empty(n)
    en_mercado = np.zeros(n, dtype=bool)
    pendientes: list[Orden] = []

    for k, i in enumerate(range(i0, i1)):
        fecha = fechas[i]
        # 1. Órdenes decididas al cierre anterior: a la apertura de esta vela.
        if pendientes and not sintetica[i]:
            for orden in pendientes:
                ejecucion, operacion, evento = _ejecutar_orden(
                    cuenta, orden, fecha, apertura[i], riesgo
                )
                if ejecucion:
                    resultado.ejecuciones.append(ejecucion)
                if operacion:
                    resultado.operaciones.append(operacion)
                if evento:
                    resultado.eventos.append((fecha, evento))
            pendientes = []

        # 2. Stop-loss dentro de la vela.
        posicion = cuenta.posicion
        if posicion is not None and not sintetica[i] and minimo[i] <= posicion.stop:
            precio = costes.precio_venta_mercado(min(apertura[i], posicion.stop))
            ejecucion, operacion = cuenta.aplicar_venta(
                fecha=fecha,
                precio=precio,
                comision=costes.comision(precio * posicion.cantidad),
                motivo="stop-loss",
            )
            resultado.ejecuciones.append(ejecucion)
            resultado.operaciones.append(operacion)

        # 3. Decisión al cierre.
        fecha_cierre = fecha + duracion
        decision = nucleo.al_cerrar_vela(
            cuenta,
            fecha_cierre=fecha_cierre,
            cierre=cierre[i],
            senal=valores_senal[i],
            atr=atr[i],
        )
        # Se suman a las que no pudieron ejecutarse (vela sintética); una compra repetida
        # o una venta sin posición se ignoran al ejecutar.
        pendientes.extend(decision.ordenes)
        resultado.eventos.extend((fecha_cierre, evento) for evento in decision.eventos)
        equity[k] = cuenta.ultimo_equity
        efectivo[k] = cuenta.efectivo
        en_mercado[k] = cuenta.posicion is not None

    # Liquidación de la posición abierta al último cierre.
    if cuenta.posicion is not None:
        precio = costes.precio_venta_mercado(cierre[i1 - 1])
        ejecucion, operacion = cuenta.aplicar_venta(
            fecha=fechas[i1 - 1] + duracion,
            precio=precio,
            comision=costes.comision(precio * cuenta.posicion.cantidad),
            motivo="fin del backtest",
        )
        resultado.ejecuciones.append(ejecucion)
        resultado.operaciones.append(operacion)
        equity[-1] = efectivo[-1] = cuenta.efectivo

    indice = pd.DatetimeIndex(fechas[i0:i1] + duracion, name="fecha_cierre")
    resultado.curva = pd.DataFrame(
        {
            "equity": equity,
            "efectivo": efectivo,
            "precio": cierre[i0:i1],
            "en_mercado": en_mercado,
        },
        index=indice,
    )
    resultado.referencia = pd.Series(
        comprar_y_mantener(apertura[i0], cierre[i0:i1], capital_inicial, costes),
        index=indice,
        name="comprar_y_mantener",
    )
    resultado.estado_final = cuenta.estado
    return resultado
