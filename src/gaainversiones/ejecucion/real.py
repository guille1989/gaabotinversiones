"""RealBroker: órdenes reales en el exchange vía ccxt.

NO se usa salvo con ``modo: real``, ``GAA_PERMITIR_REAL=si`` y confirmación manual.

Ejecución de entradas y salidas (``real.tipo_orden: limite``, por defecto):

1. Orden **límite** al mejor precio contrario más un margen (compra hasta la mejor venta
   + ``limite_margen_pb``): se ejecuta al momento casi siempre, pero nunca a un precio
   peor que ese tope aunque el libro esté vacío.
2. Si en ``espera_limite_s`` no se ha llenado del todo, se cancela lo pendiente y se
   repite con lo que falta, hasta ``intentos_limite`` veces.
3. Lo que siga faltando va a mercado si ``mercado_si_no_se_llena``; si no, la ejecución
   queda parcial (y se registra tal cual).

Idempotencia: cada orden lleva un ``clientOrderId`` (``cl_ord_id`` en Kraken) derivado de
forma determinista del id de cliente del bot. Antes de crear una orden se busca si ya
existe; y si la creación falla por la red, se busca antes de reintentar. Así un timeout
nunca duplica una compra.

El stop-loss es una orden ``stop-loss`` nativa del exchange: protege la posición aunque
el bot esté caído. Los stops colocados se vigilan (lista guardada en el almacén) para
detectar cuándo se ejecutan.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal, TypeVar

import ccxt

from gaainversiones.config import ConfigReal
from gaainversiones.cuenta import Ejecucion
from gaainversiones.ejecucion.base import Broker, Cotizacion, ErrorBroker, OrdenStop, Saldos
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.exchange import con_reintentos, limites_desde_mercado
from gaainversiones.riesgo.gestor import LimitesMercado
from gaainversiones.tiempo import ahora_ms, ms_a_fecha

log = logging.getLogger(__name__)

T = TypeVar("T")
_ESPACIO_IDS = uuid.UUID("6f1c2d6e-4b1a-5c8e-9a77-3e0d5b8f2a10")
_CERRADAS = {"closed", "canceled", "cancelled", "expired", "rejected"}
_META_STOPS = "stops_vigilados"


def id_exchange(id_cliente: str) -> str:
    """``clientOrderId`` determinista (formato UUID, aceptado por Kraken)."""
    return str(uuid.uuid5(_ESPACIO_IDS, id_cliente))


class RealBroker(Broker):
    """Broker contra el exchange real."""

    modo = "real"

    def __init__(
        self,
        exchange: Any,
        simbolo: str,
        config: ConfigReal,
        almacen: AlmacenEstado,
        *,
        limites_por_defecto: LimitesMercado,
        reloj: Callable[[], int] = ahora_ms,
        dormir: Callable[[float], None] = time.sleep,
        intentos: int = 5,
    ) -> None:
        self.exchange = exchange
        self.simbolo = simbolo
        self.base, self.cotizada = simbolo.split("/")
        self.config = config
        self.almacen = almacen
        self._limites_por_defecto = limites_por_defecto
        self._limites: LimitesMercado | None = None
        self._reloj = reloj
        self._dormir = dormir
        self._intentos = intentos
        self._perdidos: list[str] = []

    def stops_perdidos(self) -> list[str]:
        perdidos, self._perdidos = self._perdidos, []
        return perdidos

    # --- Utilidades -----------------------------------------------------------------------

    def _llamar(self, descripcion: str, funcion: Callable[[], T]) -> T:
        """Llamadas de solo lectura o idempotentes: se reintentan ante errores de red."""
        return con_reintentos(
            funcion,
            descripcion=f"{self.exchange.id} {descripcion}",
            intentos=self._intentos,
            dormir=self._dormir,
        )

    def _buscar(self, id_cli: str) -> dict[str, Any] | None:
        """Orden con ese ``clientOrderId`` entre las abiertas y las cerradas recientes."""
        for metodo in (self.exchange.fetch_open_orders, self.exchange.fetch_closed_orders):
            for orden in self._llamar("buscar orden", lambda m=metodo: m(self.simbolo)):  # type: ignore[misc]
                if orden.get("clientOrderId") == id_cli:
                    return dict(orden)
        return None

    def _crear(self, id_cliente: str, crear: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        """Crea una orden sin duplicarla nunca (ver la nota de idempotencia del módulo)."""
        id_cli = id_exchange(id_cliente)
        existente = self._buscar(id_cli)
        if existente is not None:
            log.info("La orden %s ya existía en el exchange: se reutiliza", id_cliente)
            return existente
        for intento in range(1, self._intentos + 1):
            try:
                return dict(crear(id_cli))
            except ccxt.NetworkError as e:
                log.warning(
                    "Error de red al crear %s (%s): compruebo si llegó al exchange",
                    id_cliente,
                    type(e).__name__,
                )
                self._dormir(min(2**intento, 30))
                existente = self._buscar(id_cli)
                if existente is not None:
                    return existente
        raise ErrorBroker(f"No se pudo crear la orden {id_cliente} tras {self._intentos} intentos")

    def _consultar(self, orden: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self._llamar(
                "consultar orden", lambda: self.exchange.fetch_order(orden["id"], self.simbolo)
            )
        )

    def _esperar(self, orden: dict[str, Any], segundos: float) -> dict[str, Any]:
        """Espera a que la orden termine; si no termina a tiempo, cancela lo pendiente."""
        limite = self._reloj() + segundos * 1000
        while orden.get("status") not in _CERRADAS and self._reloj() < limite:
            self._dormir(1)
            orden = self._consultar(orden)
        if orden.get("status") not in _CERRADAS:
            with contextlib.suppress(ccxt.OrderNotFound):  # se llenó justo antes de cancelar
                self.exchange.cancel_order(orden["id"], self.simbolo)
            orden = self._consultar(orden)
        return orden

    def _comision_en_cotizada(self, orden: dict[str, Any]) -> float:
        comisiones = orden.get("fees") or ([orden["fee"]] if orden.get("fee") else [])
        total = 0.0
        for comision in comisiones:
            coste = float(comision.get("cost") or 0)
            if comision.get("currency") == self.base:
                coste *= float(orden.get("average") or orden.get("price") or 0)
            total += coste
        return total

    def _vigilados(self) -> list[str]:
        valor = self.almacen.meta(_META_STOPS) or ""
        return [i for i in valor.split(",") if i]

    def _guardar_vigilados(self, ids: list[str]) -> None:
        self.almacen.guardar_meta(_META_STOPS, ",".join(ids))

    # --- Interfaz Broker ------------------------------------------------------------------

    def limites(self) -> LimitesMercado:
        if self._limites is None:
            self._llamar("mercados", self.exchange.load_markets)
            self._limites = limites_desde_mercado(
                self.exchange.market(self.simbolo),
                self.exchange.precisionMode,
                self._limites_por_defecto,
            )
        return self._limites

    def cotizacion(self) -> Cotizacion:
        libro = self._llamar("libro", lambda: self.exchange.fetch_order_book(self.simbolo, 10))
        if not libro.get("bids") or not libro.get("asks"):
            raise ErrorBroker(f"Libro de órdenes vacío para {self.simbolo}")
        return Cotizacion(
            compra=float(libro["bids"][0][0]),
            venta=float(libro["asks"][0][0]),
            fecha=ms_a_fecha(self._reloj()),
        )

    def saldos(self) -> Saldos:
        balance = self._llamar("saldos", self.exchange.fetch_balance)
        total = balance.get("total") or {}
        return Saldos(
            base=float(total.get(self.base) or 0), cotizada=float(total.get(self.cotizada) or 0)
        )

    def capital_para_cuenta_nueva(self) -> float:
        return min(self.saldos().cotizada, self.config.capital_maximo)

    def comprar_mercado(self, cantidad: float, *, motivo: str, id_cliente: str) -> Ejecucion:
        return self._ejecutar("compra", cantidad, motivo, id_cliente)

    def vender_mercado(self, cantidad: float, *, motivo: str, id_cliente: str) -> Ejecucion:
        return self._ejecutar("venta", cantidad, motivo, id_cliente)

    def _ejecutar(
        self, lado: Literal["compra", "venta"], cantidad: float, motivo: str, id_cliente: str
    ) -> Ejecucion:
        lado_ccxt = "buy" if lado == "compra" else "sell"
        limites = self.limites()
        referencia = self.cotizacion().medio
        llenado = coste = comision = 0.0
        ordenes: list[dict[str, Any]] = []

        def restante() -> float:
            return limites.redondear(cantidad - llenado)

        intentos_limite = self.config.intentos_limite if self.config.tipo_orden == "limite" else 0
        margen = self.config.limite_margen_pb / 10_000
        for intento in range(intentos_limite):
            if not limites.admite(restante(), referencia):
                break
            cotizacion = self.cotizacion()
            precio = (
                cotizacion.venta * (1 + margen)
                if lado == "compra"
                else cotizacion.compra * (1 - margen)
            )
            pendiente = restante()
            orden = self._crear(
                f"{id_cliente}-{intento}",
                lambda cid, q=pendiente, p=precio: self.exchange.create_order(  # type: ignore[misc]
                    self.simbolo, "limit", lado_ccxt, q, p, {"clientOrderId": cid}
                ),
            )
            ordenes.append(self._esperar(orden, self.config.espera_limite_s))
            llenado, coste, comision = self._sumar(ordenes)

        if limites.admite(restante(), referencia) and (
            self.config.tipo_orden == "mercado" or self.config.mercado_si_no_se_llena
        ):
            pendiente = restante()
            orden = self._crear(
                f"{id_cliente}-m",
                lambda cid: self.exchange.create_order(
                    self.simbolo, "market", lado_ccxt, pendiente, None, {"clientOrderId": cid}
                ),
            )
            ordenes.append(self._esperar(orden, 30))
            llenado, coste, comision = self._sumar(ordenes)

        if llenado <= 0:
            raise ErrorBroker(f"La orden {id_cliente} no se ejecutó")
        if limites.redondear(cantidad - llenado) > 0:
            log.warning("Orden %s ejecutada en parte: %.8f de %.8f", id_cliente, llenado, cantidad)
        return Ejecucion(
            ms_a_fecha(self._reloj()),
            lado,
            coste / llenado,
            llenado,
            comision,
            motivo,
            referencia,
            id_cliente,
        )

    def _sumar(self, ordenes: list[dict[str, Any]]) -> tuple[float, float, float]:
        llenado = sum(float(o.get("filled") or 0) for o in ordenes)
        coste = sum(
            float(o.get("cost") or float(o.get("filled") or 0) * float(o.get("average") or 0))
            for o in ordenes
        )
        return llenado, coste, sum(self._comision_en_cotizada(o) for o in ordenes)

    def colocar_stop(self, cantidad: float, precio_stop: float, *, id_cliente: str) -> OrdenStop:
        orden = self._crear(
            id_cliente,
            lambda cid: self.exchange.create_order(
                self.simbolo,
                "market",
                "sell",
                cantidad,
                None,
                {"stopLossPrice": precio_stop, "clientOrderId": cid},
            ),
        )
        vigilados = self._vigilados()
        if orden["id"] not in vigilados:
            self._guardar_vigilados([*vigilados, str(orden["id"])])
        return OrdenStop(str(orden["id"]), cantidad, precio_stop, ms_a_fecha(self._reloj()))

    def cancelar_stop(self, id_orden: str) -> None:
        try:
            self._llamar(
                "cancelar stop", lambda: self.exchange.cancel_order(id_orden, self.simbolo)
            )
        except ccxt.OrderNotFound:
            log.info("El stop %s ya no existía", id_orden)
        self._guardar_vigilados([i for i in self._vigilados() if i != id_orden])

    def ordenes_stop(self) -> list[OrdenStop]:
        abiertas = self._llamar(
            "órdenes abiertas", lambda: self.exchange.fetch_open_orders(self.simbolo)
        )
        stops = []
        for orden in abiertas:
            precio = orden.get("stopLossPrice") or orden.get("triggerPrice")
            if orden.get("side") == "sell" and precio:
                creada = orden.get("timestamp") or self._reloj()
                stops.append(
                    OrdenStop(
                        str(orden["id"]), float(orden["amount"]), float(precio), ms_a_fecha(creada)
                    )
                )
        return stops

    def revisar_stops(self, hasta_ms: int | None = None) -> list[tuple[OrdenStop, Ejecucion]]:
        """Stops vigilados que el exchange ya ha ejecutado (``hasta_ms`` no aplica en real)."""
        ejecutados: list[tuple[OrdenStop, Ejecucion]] = []
        quedan: list[str] = []
        for id_orden in self._vigilados():
            orden = self._consultar({"id": id_orden})
            estado = orden.get("status")
            precio_stop = float(orden.get("stopLossPrice") or orden.get("triggerPrice") or 0)
            if estado == "closed" and float(orden.get("filled") or 0) > 0:
                llenado, coste, comision = self._sumar([orden])
                momento = orden.get("lastTradeTimestamp") or orden.get("timestamp") or self._reloj()
                stop = OrdenStop(
                    id_orden,
                    float(orden.get("amount") or llenado),
                    precio_stop,
                    ms_a_fecha(momento),
                )
                ejecucion = Ejecucion(
                    ms_a_fecha(momento),
                    "venta",
                    coste / llenado,
                    llenado,
                    comision,
                    "stop-loss",
                    precio_stop,
                    id_orden,
                )
                ejecutados.append((stop, ejecucion))
            elif estado in _CERRADAS:
                log.warning("El stop %s terminó sin ejecutarse (%s)", id_orden, estado)
                self._perdidos.append(id_orden)
            else:
                quedan.append(id_orden)
        if quedan != self._vigilados():
            self._guardar_vigilados(quedan)
        return ejecutados
