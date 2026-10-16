"""PaperBroker: opera con precios reales del exchange y dinero simulado.

- **Órdenes a mercado:** se ejecutan contra el libro de órdenes real en ese instante,
  consumiendo niveles hasta completar la cantidad (precio medio ponderado), y pagan la
  comisión *taker*. Así el paper mide el deslizamiento real, no uno supuesto.
- **Stop-loss:** vive en el "exchange simulado" (SQLite) y bloquea la cantidad, como en
  Kraken. Se comprueba con velas de 1 minuto reales: salta en el primer minuto cuyo
  mínimo toca el stop y se ejecuta al precio del stop (o a la apertura de ese minuto si
  abrió por debajo) menos el deslizamiento configurado. Si el bot estuvo parado más de
  lo que cubren las velas de 1 minuto (12 h en Kraken), se usan velas de 1 hora.
- Todo se guarda en el mismo ``AlmacenEstado`` que la cuenta del bot, de modo que una
  operación y su registro se confirman juntos.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

import ccxt

from gaainversiones.cuenta import Ejecucion
from gaainversiones.ejecucion.base import (
    Broker,
    Cotizacion,
    ErrorBroker,
    OrdenStop,
    SaldoInsuficiente,
    Saldos,
)
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.exchange import con_reintentos, limites_desde_mercado
from gaainversiones.riesgo.gestor import LimitesMercado
from gaainversiones.tiempo import ahora_ms, inicio_vela, ms_a_fecha, timeframe_a_ms

log = logging.getLogger(__name__)

T = TypeVar("T")
_Vela = tuple[int, int, float, float, float]  # inicio, duración, apertura, mínimo, cierre
_MINUTO = 60_000
_EPSILON = 1e-12


class PaperBroker(Broker):
    """Broker simulado sobre datos reales de mercado."""

    modo = "paper"

    def __init__(
        self,
        exchange: Any,
        simbolo: str,
        costes: ModeloCostes,
        almacen: AlmacenEstado,
        *,
        capital_inicial: float,
        limites_por_defecto: LimitesMercado,
        profundidad: int = 50,
        reloj: Callable[[], int] = ahora_ms,
        intentos: int = 5,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self.exchange = exchange
        self.simbolo = simbolo
        self.base, self.cotizada = simbolo.split("/")
        self.costes = costes
        self.almacen = almacen
        self.profundidad = profundidad
        self._limites_por_defecto = limites_por_defecto
        self._limites: LimitesMercado | None = None
        self._reloj = reloj
        self._intentos = intentos
        self._dormir = dormir
        if almacen.sim_saldos() is None:
            almacen.guardar_sim_saldos({self.base: 0.0, self.cotizada: capital_inicial})
            log.info("Exchange simulado creado con %.2f %s", capital_inicial, self.cotizada)

    # --- Utilidades -----------------------------------------------------------------------

    def _llamar(self, descripcion: str, funcion: Callable[[], T]) -> T:
        return con_reintentos(
            funcion,
            descripcion=f"{self.exchange.id} {descripcion}",
            intentos=self._intentos,
            dormir=self._dormir,
        )

    def _saldos(self) -> dict[str, float]:
        saldos = self.almacen.sim_saldos()
        assert saldos is not None
        return saldos

    def _reservado(self) -> float:
        return sum(cantidad for _, cantidad, _, _, _ in self.almacen.sim_ordenes_abiertas())

    def _libro(self) -> dict[str, Any]:
        libro: dict[str, Any] = self._llamar(
            "libro de órdenes",
            lambda: self.exchange.fetch_order_book(self.simbolo, self.profundidad),
        )
        if not libro.get("bids") or not libro.get("asks"):
            raise ErrorBroker(f"Libro de órdenes vacío para {self.simbolo}")
        return libro

    def _precio_medio(
        self, niveles: Sequence[Sequence[float]], cantidad: float, compra: bool
    ) -> float:
        """Precio medio de ejecutar ``cantidad`` consumiendo los niveles del libro."""
        restante, coste = cantidad, 0.0
        for nivel in niveles:
            precio, volumen = float(nivel[0]), float(nivel[1])
            tomado = min(restante, volumen)
            coste += tomado * precio
            restante -= tomado
            if restante <= _EPSILON:
                return coste / cantidad
        # El libro descargado no cubre la cantidad: el resto, al peor nivel con deslizamiento.
        peor = float(niveles[-1][0])
        ajustado = (
            self.costes.precio_compra_mercado(peor)
            if compra
            else self.costes.precio_venta_mercado(peor)
        )
        log.warning(
            "El libro (%d niveles) no cubre %.8f %s; el resto se valora a %.2f",
            len(niveles),
            cantidad,
            self.base,
            ajustado,
        )
        return (coste + restante * ajustado) / cantidad

    # --- Interfaz Broker ------------------------------------------------------------------

    def limites(self) -> LimitesMercado:
        if self._limites is None:
            try:
                self._llamar("mercados", self.exchange.load_markets)
                self._limites = limites_desde_mercado(
                    self.exchange.market(self.simbolo),
                    self.exchange.precisionMode,
                    self._limites_por_defecto,
                )
            except ccxt.BaseError as e:
                log.warning(
                    "No se pudieron leer los límites del mercado (%s); uso los de config", e
                )
                self._limites = self._limites_por_defecto
        return self._limites

    def cotizacion(self) -> Cotizacion:
        libro = self._libro()
        return Cotizacion(
            compra=float(libro["bids"][0][0]),
            venta=float(libro["asks"][0][0]),
            fecha=ms_a_fecha(self._reloj()),
        )

    def saldos(self) -> Saldos:
        saldos = self._saldos()
        return Saldos(base=saldos[self.base], cotizada=saldos[self.cotizada])

    def comprar_mercado(self, cantidad: float, *, motivo: str, id_cliente: str) -> Ejecucion:
        previa = self.almacen.sim_ejecucion(id_cliente)
        if previa is not None:
            return previa  # ya ejecutada: no se repite
        libro = self._libro()
        precio = self._precio_medio(libro["asks"], cantidad, compra=True)
        referencia = (float(libro["bids"][0][0]) + float(libro["asks"][0][0])) / 2
        comision = self.costes.comision(precio * cantidad)
        saldos = self._saldos()
        coste = precio * cantidad + comision
        if coste > saldos[self.cotizada] + 1e-9:
            raise SaldoInsuficiente(
                f"Compra de {cantidad:.8f} {self.base}: cuesta {coste:.2f} {self.cotizada} "
                f"y hay {saldos[self.cotizada]:.2f}"
            )
        saldos[self.cotizada] -= coste
        saldos[self.base] += cantidad
        ejecucion = Ejecucion(
            ms_a_fecha(self._reloj()),
            "compra",
            precio,
            cantidad,
            comision,
            motivo,
            referencia,
            id_cliente,
        )
        with self.almacen.transaccion():
            self.almacen.guardar_sim_saldos(saldos)
            self.almacen.guardar_sim_ejecucion(ejecucion)
        return ejecucion

    def vender_mercado(self, cantidad: float, *, motivo: str, id_cliente: str) -> Ejecucion:
        previa = self.almacen.sim_ejecucion(id_cliente)
        if previa is not None:
            return previa
        saldos = self._saldos()
        disponible = saldos[self.base] - self._reservado()
        if cantidad > disponible + _EPSILON:
            raise SaldoInsuficiente(
                f"Venta de {cantidad:.8f} {self.base}: disponible {disponible:.8f} "
                "(¿hay un stop abierto que bloquea el saldo?)"
            )
        libro = self._libro()
        precio = self._precio_medio(libro["bids"], cantidad, compra=False)
        referencia = (float(libro["bids"][0][0]) + float(libro["asks"][0][0])) / 2
        comision = self.costes.comision(precio * cantidad)
        saldos[self.base] -= cantidad
        saldos[self.cotizada] += precio * cantidad - comision
        ejecucion = Ejecucion(
            ms_a_fecha(self._reloj()),
            "venta",
            precio,
            cantidad,
            comision,
            motivo,
            referencia,
            id_cliente,
        )
        with self.almacen.transaccion():
            self.almacen.guardar_sim_saldos(saldos)
            self.almacen.guardar_sim_ejecucion(ejecucion)
        return ejecucion

    def colocar_stop(self, cantidad: float, precio_stop: float, *, id_cliente: str) -> OrdenStop:
        abiertas = {orden[0]: orden for orden in self.almacen.sim_ordenes_abiertas()}
        if id_cliente in abiertas:
            _, c, p, creada, _ = abiertas[id_cliente]
            return OrdenStop(id_cliente, c, p, ms_a_fecha(creada))
        disponible = self._saldos()[self.base] - self._reservado()
        if cantidad > disponible + _EPSILON:
            raise SaldoInsuficiente(
                f"Stop de {cantidad:.8f} {self.base}: disponible {disponible:.8f}"
            )
        ahora = self._reloj()
        self.almacen.guardar_sim_orden(
            id_cliente,
            cantidad=cantidad,
            precio_stop=precio_stop,
            estado="abierta",
            creada=ahora,
            revisado_hasta=ahora,
        )
        return OrdenStop(id_cliente, cantidad, precio_stop, ms_a_fecha(ahora))

    def cancelar_stop(self, id_orden: str) -> None:
        for (
            orden_id,
            cantidad,
            precio_stop,
            creada,
            revisado,
        ) in self.almacen.sim_ordenes_abiertas():
            if orden_id == id_orden:
                self.almacen.guardar_sim_orden(
                    orden_id,
                    cantidad=cantidad,
                    precio_stop=precio_stop,
                    estado="cancelada",
                    creada=creada,
                    revisado_hasta=revisado,
                )

    def ordenes_stop(self) -> list[OrdenStop]:
        return [
            OrdenStop(i, c, p, ms_a_fecha(creada))
            for i, c, p, creada, _ in self.almacen.sim_ordenes_abiertas()
        ]

    def revisar_stops(self, hasta_ms: int | None = None) -> list[tuple[OrdenStop, Ejecucion]]:
        ahora = self._reloj()
        limite = ahora if hasta_ms is None else min(hasta_ms, ahora)
        ejecutados: list[tuple[OrdenStop, Ejecucion]] = []
        for (
            orden_id,
            cantidad,
            precio_stop,
            creada,
            revisado,
        ) in self.almacen.sim_ordenes_abiertas():
            desde = max(creada, revisado)
            if limite <= desde:
                continue
            disparo = self._primer_cruce(precio_stop, creada, desde, limite)
            if disparo is None:
                self.almacen.guardar_sim_orden(
                    orden_id,
                    cantidad=cantidad,
                    precio_stop=precio_stop,
                    estado="abierta",
                    creada=creada,
                    revisado_hasta=limite,
                )
                continue
            momento, precio_base = disparo
            precio = self.costes.precio_venta_mercado(precio_base)
            comision = self.costes.comision(precio * cantidad)
            saldos = self._saldos()
            saldos[self.base] -= cantidad
            saldos[self.cotizada] += precio * cantidad - comision
            ejecucion = Ejecucion(
                ms_a_fecha(momento),
                "venta",
                precio,
                cantidad,
                comision,
                "stop-loss",
                precio_stop,
                orden_id,
            )
            with self.almacen.transaccion():
                self.almacen.guardar_sim_orden(
                    orden_id,
                    cantidad=cantidad,
                    precio_stop=precio_stop,
                    estado="ejecutada",
                    creada=creada,
                    revisado_hasta=momento,
                )
                self.almacen.guardar_sim_saldos(saldos)
                self.almacen.guardar_sim_ejecucion(ejecucion)
            orden = OrdenStop(orden_id, cantidad, precio_stop, ms_a_fecha(creada))
            ejecutados.append((orden, ejecucion))
        return ejecutados

    # --- Detección del stop con velas reales ----------------------------------------------

    def _velas(self, temporalidad: str, desde: int, hasta: int) -> list[_Vela]:
        """(inicio, duración, apertura, mínimo, cierre) de las velas que empiezan en [desde, hasta).

        Incluye la vela en curso: su mínimo hasta ahora ya es información válida.
        """
        tf = timeframe_a_ms(temporalidad)
        velas: list[_Vela] = []
        cursor = inicio_vela(desde, tf)
        while cursor < hasta:
            inicio = cursor
            pagina = self._llamar(
                f"velas {temporalidad}",
                lambda inicio=inicio: self.exchange.fetch_ohlcv(  # type: ignore[misc]
                    self.simbolo, temporalidad, since=inicio, limit=720
                ),
            )
            nuevas = sorted(
                (int(f[0]), tf, float(f[1]), float(f[3]), float(f[4]))
                for f in pagina
                if inicio <= f[0] < hasta
            )
            if not nuevas:
                break
            velas.extend(nuevas)
            cursor = nuevas[-1][0] + tf
        return velas

    def _velas_para_revisar(self, desde: int, hasta: int) -> list[_Vela]:
        minutos = self._velas("1m", desde, hasta)
        primer_minuto = inicio_vela(desde, _MINUTO)
        if minutos and minutos[0][0] <= primer_minuto:
            return minutos
        # Las velas de 1 minuto no llegan tan atrás (Kraken: 12 h): el hueco, con velas de 1 hora.
        cubierto = minutos[0][0] if minutos else hasta
        log.warning(
            "Sin velas de 1 minuto desde %s: reviso el stop con velas de 1 hora hasta %s",
            ms_a_fecha(primer_minuto),
            ms_a_fecha(cubierto),
        )
        return self._velas("1h", desde, cubierto) + minutos

    def _primer_cruce(
        self, precio_stop: float, creada: int, desde: int, hasta: int
    ) -> tuple[int, float] | None:
        """Primer momento en que el precio toca el stop: (instante, precio base de ejecución).

        - Vela posterior a la colocación: salta al precio del stop o, si la vela abrió ya
          por debajo (hueco), a su apertura.
        - Vela en la que se colocó la orden: no se sabe si su mínimo fue antes o después
          de colocarla, así que solo cuenta si además cerró por debajo del stop.
        """
        for ts, duracion, apertura, minimo, cierre in self._velas_para_revisar(desde, hasta):
            if minimo > precio_stop:
                continue
            if ts < creada < ts + duracion:
                if cierre > precio_stop:
                    continue
                return creada, precio_stop
            return ts, min(apertura, precio_stop)
        return None
