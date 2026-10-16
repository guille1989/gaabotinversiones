"""Bucle en vivo del bot (paper y, más adelante, real).

Cada ``intervalo_s`` segundos:

1. Si ha cerrado una vela nueva (hace al menos ``retraso_cierre_s``):
   a. revisa los stops ejecutados durante esa vela;
   b. descarga las últimas ``velas_historia`` velas cerradas y calcula la señal;
   c. pasa por el mismo ``Nucleo`` que el backtest y ejecuta sus órdenes a mercado
      (equivale a "la apertura de la vela siguiente");
   d. guarda todo en una sola transacción, incluida la marca de vela procesada.
2. Revisa si ha saltado el stop-loss desde la última comprobación.

Solo actúa sobre velas cerradas. Tras un reinicio no repite velas ya procesadas y, si
estuvo parado varias velas, actúa solo con la última (nunca con señales caducadas).
Los ids de cliente de las órdenes son deterministas (modo-vela-lado): si una ejecución
se interrumpe y se repite, el broker no duplica la orden.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

import ccxt
import pandas as pd

from gaainversiones.bot.killswitch import AccionManual, FuenteOrdenes
from gaainversiones.bot.notificador import Nivel, Notificador, NotificadorLog
from gaainversiones.config import Config
from gaainversiones.cuenta import Ejecucion, EstadoCuenta, Operacion, Posicion
from gaainversiones.datos.calidad import rellenar_huecos
from gaainversiones.datos.fuentes import FuenteVelas
from gaainversiones.ejecucion.base import Broker, ErrorBroker
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.estado.reconciliacion import ErrorReconciliacion, reconciliar
from gaainversiones.estrategias import Senal, crear_estrategia
from gaainversiones.formato import formato_dinero, formato_pct
from gaainversiones.indicadores import atr as calcular_atr
from gaainversiones.nucleo import Nucleo, Orden
from gaainversiones.riesgo.gestor import GestorRiesgo
from gaainversiones.tiempo import ahora_ms, indice_a_ms, inicio_vela, ms_a_fecha, timeframe_a_ms

log = logging.getLogger(__name__)

Mensajes = list[tuple[str, Nivel]]

# Margen al comprar con todo el efectivo: el precio medio del libro puede superar la mejor venta.
_MARGEN_EFECTIVO = 0.005


class VelaNoDisponible(Exception):
    """El exchange aún no ha publicado la vela que acaba de cerrar."""


class BotYaEnMarcha(Exception):
    """Otro proceso está usando este mismo estado."""


class BotDetenido(Exception):
    """El bot está detenido (caída máxima o parada manual) y necesita ``gaa reanudar``."""


class Bot:
    """Bot de trading sobre un broker (paper o real)."""

    def __init__(
        self,
        config: Config,
        broker: Broker,
        almacen: AlmacenEstado,
        fuente: FuenteVelas,
        *,
        notificador: Notificador | None = None,
        ordenes: Sequence[FuenteOrdenes] = (),
        reloj: Callable[[], int] = ahora_ms,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        """``ordenes``: orígenes del kill switch (fichero STOP, Telegram...)."""
        self.config = config
        self.broker = broker
        self.almacen = almacen
        self.fuente = fuente
        self.notificador = notificador or NotificadorLog()
        self.ordenes = list(ordenes)
        self.reloj = reloj
        self.dormir = dormir
        self.simbolo = config.mercado.simbolo
        self.base, self.cotizada = config.mercado.base, config.mercado.cotizada
        self.temporalidad = config.mercado.temporalidad
        self.tf_ms = timeframe_a_ms(self.temporalidad)
        self.estrategia = crear_estrategia(config.estrategia.nombre, config.estrategia.parametros)
        if config.bot.velas_historia <= self.estrategia.velas_necesarias:
            raise ValueError(
                f"bot.velas_historia ({config.bot.velas_historia}) es menor que la historia que "
                f"necesita la estrategia ({self.estrategia.velas_necesarias} velas)"
            )
        self.costes = ModeloCostes.desde_config(config.costes)
        self.riesgo = GestorRiesgo(config.riesgo, self.costes, broker.limites())
        self.nucleo = Nucleo(self.riesgo, detener_en_caida_max=True)
        self.cuenta: EstadoCuenta | None = None
        self._parar = False

    # --- Utilidades ---------------------------------------------------------------------

    @property
    def _cuenta(self) -> EstadoCuenta:
        if self.cuenta is None:
            raise RuntimeError("El bot no ha arrancado")
        return self.cuenta

    def parar(self) -> None:
        """Pide al bucle que termine tras el ciclo en curso."""
        self._parar = True

    def _avisar(self, mensajes: Mensajes) -> None:
        for texto, nivel in mensajes:
            self.notificador.enviar(texto, nivel=nivel)

    def _evento(self, texto: str, nivel: Nivel = "info") -> tuple[str, Nivel]:
        self.almacen.registrar_evento(texto, nivel=nivel)
        return texto, nivel

    def latido_reciente(self) -> bool:
        latido = self.almacen.meta_entero("latido")
        return (
            bool(latido) and self.reloj() - (latido or 0) < 3 * self.config.bot.intervalo_s * 1000
        )

    def _ultima_vela_cerrada(self, ahora: int) -> int:
        """Inicio de la última vela que cerró hace al menos ``retraso_cierre_s``."""
        return inicio_vela(ahora - self.config.bot.retraso_cierre_s * 1000, self.tf_ms) - self.tf_ms

    def _dinero(self, valor: float, *, signo: bool = False) -> str:
        return formato_dinero(valor, self.cotizada, signo=signo)

    def _texto_venta(self, ejecucion: Ejecucion, operacion: Operacion) -> str:
        return (
            f"VENTA {ejecucion.cantidad:.8f} {self.base} a {self._dinero(ejecucion.precio)} "
            f"({ejecucion.motivo}). Resultado {self._dinero(operacion.resultado, signo=True)} "
            f"({formato_pct(operacion.rentabilidad, 2, signo=True)})."
        )

    # --- Arranque -----------------------------------------------------------------------

    def arrancar(self, *, forzar: bool = False) -> None:
        """Carga el estado, recoge los stops ejecutados mientras estaba parado y reconcilia.

        Raises:
            BotYaEnMarcha: si otro proceso ha dado señales de vida hace poco.
            ErrorReconciliacion: si el estado no cuadra con el exchange.
        """
        if not forzar and self.latido_reciente():
            raise BotYaEnMarcha(
                "Parece que ya hay un bot en marcha con este estado (latido reciente). "
                "Si no es así, espera un par de minutos y vuelve a intentarlo."
            )
        mensajes: Mensajes = []
        cuenta = self.almacen.cargar_cuenta()
        if cuenta is None:
            capital = self.broker.capital_para_cuenta_nueva()
            cuenta = EstadoCuenta.nueva(capital)
            mensajes.append(self._evento(f"Cuenta nueva con {self._dinero(capital)}"))
        if cuenta.estado == "detenido":
            raise BotDetenido(
                f"El bot está detenido ({cuenta.motivo_estado}). Revisa la situación y, si "
                "quieres que vuelva a operar, ejecuta `gaa reanudar`."
            )
        self.cuenta = cuenta
        mensajes += self._revisar_stops(None)

        resultado = reconciliar(cuenta, self.broker, cuenta_exclusiva=self.broker.modo == "paper")
        if not resultado.ok:
            texto = (
                "El estado guardado no cuadra con el exchange; el bot NO opera:\n- "
                + "\n- ".join(resultado.discrepancias)
            )
            self._avisar([self._evento(texto, "error")])
            raise ErrorReconciliacion(texto)
        if resultado.falta_stop:
            mensajes += self._proteger_posicion()

        self.almacen.guardar_cuenta(cuenta)
        self.almacen.guardar_meta("latido", self.reloj())
        posicion = (
            f"posición de {cuenta.posicion.cantidad:.8f} {self.base} con stop en "
            f"{self._dinero(cuenta.posicion.stop)}"
            if cuenta.posicion
            else "sin posición"
        )
        mensajes.append(
            self._evento(
                f"Bot arrancado en modo {self.broker.modo}: {self.simbolo} {self.temporalidad}, "
                f"{self.estrategia.describir()}. Capital {self._dinero(cuenta.ultimo_equity)}, "
                f"{posicion}, estado {cuenta.estado}."
            )
        )
        self._avisar(mensajes)

    def _proteger_posicion(self) -> Mensajes:
        """Posición sin stop en el exchange: se coloca o, si el precio ya está debajo, se vende."""
        cuenta = self._cuenta
        posicion = cuenta.posicion
        assert posicion is not None
        sufijo = f"{self.broker.modo}-proteccion-{self.reloj()}"
        with self.almacen.transaccion():
            if self.broker.cotizacion().compra <= posicion.stop:
                return self._vender(posicion, "stop-loss (sin stop al arrancar)", f"{sufijo}-venta")
            orden = self.broker.colocar_stop(
                posicion.cantidad, posicion.stop, id_cliente=f"{sufijo}-stop"
            )
            posicion.id_orden_stop = orden.id
            self.almacen.guardar_cuenta(cuenta)
            return [
                self._evento(
                    "La posición no tenía stop en el exchange: colocado en "
                    f"{self._dinero(posicion.stop)}",
                    "aviso",
                )
            ]

    # --- Ciclo --------------------------------------------------------------------------

    def ciclo(self) -> None:
        """Una pasada del bucle: órdenes manuales, vela nueva (si la hay) y stops."""
        if self._atender_ordenes_manuales():
            return
        ahora = self.reloj()
        ultima = self._ultima_vela_cerrada(ahora)
        procesada = self.almacen.meta_entero("ultima_vela")
        mensajes: Mensajes = []
        if procesada is None or ultima > procesada:
            mensajes += self._revisar_stops(ultima + self.tf_ms)
            mensajes += self._procesar_vela(ultima, procesada)
        mensajes += self._revisar_stops(None)
        self.almacen.guardar_meta("latido", ahora)
        self._avisar(mensajes)
        self._resumen_diario(ahora)

    # --- Kill switch y resúmenes --------------------------------------------------------

    def _atender_ordenes_manuales(self) -> bool:
        """Ejecuta las órdenes del kill switch. Devuelve True si el bot debe detenerse."""
        for fuente in self.ordenes:
            for orden in fuente.revisar():
                if orden.accion == "estado":
                    self.notificador.enviar(self.resumen())
                    continue
                self.aplicar_parada(orden.accion, orden.origen)
                return True
        return False

    def aplicar_parada(self, accion: AccionManual, origen: str) -> None:
        """Kill switch: ``parar`` conserva la posición con su stop; ``liquidar`` la vende."""
        cuenta = self._cuenta
        mensajes: Mensajes = []
        try:
            with self.almacen.transaccion():
                if accion == "liquidar" and cuenta.posicion is not None:
                    mensajes += self._vender(
                        cuenta.posicion,
                        f"kill switch ({origen})",
                        f"{self.broker.modo}-liquidar-{self.reloj()}",
                    )
                cuenta.estado, cuenta.motivo_estado = "detenido", f"parada manual ({origen})"
                self.almacen.guardar_cuenta(cuenta)
        except Exception:
            self.cuenta = self.almacen.cargar_cuenta()
            raise
        if cuenta.posicion is not None:
            detalle = (
                f"La posición de {cuenta.posicion.cantidad:.8f} {self.base} conserva su stop en "
                f"{self._dinero(cuenta.posicion.stop)}."
            )
        else:
            detalle = "Sin posición abierta."
        mensajes.append(
            self._evento(
                f"KILL SWITCH ({accion}, {origen}): bot detenido. {detalle} "
                "Para volver a operar: `gaa reanudar`.",
                "aviso",
            )
        )
        self._avisar(mensajes)
        self._parar = True

    def parada_directa(self, accion: AccionManual, origen: str) -> None:
        """Kill switch con el bot apagado: actúa directamente sobre el broker y el estado."""
        cuenta = self.almacen.cargar_cuenta()
        if cuenta is None:
            raise ValueError("No hay estado: el bot nunca ha arrancado en este modo")
        self.cuenta = cuenta
        self._avisar(self._revisar_stops(None))
        self.aplicar_parada(accion, origen)

    def resumen(self) -> str:
        """Estado actual en pocas líneas (para /estado y el resumen diario)."""
        cuenta = self._cuenta
        curva = self.almacen.curva()
        hace_un_dia = pd.Timestamp(self.reloj(), unit="ms", tz="UTC") - pd.Timedelta(days=1)
        anteriores = curva[curva.index <= hace_un_dia]
        variacion = (
            f" ({formato_pct(cuenta.ultimo_equity / anteriores['equity'].iloc[-1] - 1, signo=True)}"
            " en 24 h)"
            if not anteriores.empty
            else ""
        )
        lineas = [
            f"Modo {self.broker.modo} · {self.simbolo} {self.temporalidad}",
            f"Estado: {cuenta.estado}"
            + (f" ({cuenta.motivo_estado})" if cuenta.motivo_estado else ""),
            f"Capital: {self._dinero(cuenta.ultimo_equity)}{variacion}",
        ]
        posicion = cuenta.posicion
        if posicion is None:
            lineas.append("Posición: ninguna")
        else:
            texto = (
                f"Posición: {posicion.cantidad:.8f} {self.base} a "
                f"{self._dinero(posicion.precio_entrada)}, stop {self._dinero(posicion.stop)}"
            )
            if not curva.empty:
                precio = float(curva["precio"].iloc[-1])
                latente = posicion.cantidad * precio - posicion.coste_entrada
                texto += f", latente {self._dinero(latente, signo=True)}"
            lineas.append(texto)
        operaciones = self.almacen.operaciones()
        recientes = (
            operaciones[operaciones["fecha_salida"] > hace_un_dia]
            if not operaciones.empty
            else operaciones
        )
        if not recientes.empty:
            lineas.append(
                f"Últimas 24 h: {len(recientes)} operaciones, "
                f"{self._dinero(float(recientes['resultado'].sum()), signo=True)}"
            )
        return "\n".join(lineas)

    def _resumen_diario(self, ahora: int) -> None:
        """Latido diario: un resumen al día a partir de ``alertas.hora_resumen_utc``."""
        momento = pd.Timestamp(ahora, unit="ms", tz="UTC")
        hoy = momento.date().isoformat()
        if (
            momento.hour < self.config.alertas.hora_resumen_utc
            or self.almacen.meta("resumen") == hoy
        ):
            return
        self.almacen.guardar_meta("resumen", hoy)
        self.notificador.enviar("Resumen diario\n" + self.resumen())

    def _revisar_stops(self, hasta_ms: int | None) -> Mensajes:
        cuenta = self._cuenta
        mensajes: Mensajes = []
        try:
            with self.almacen.transaccion():
                for orden, ejecucion in self.broker.revisar_stops(hasta_ms):
                    posicion = cuenta.posicion
                    if posicion is None or posicion.id_orden_stop != orden.id:
                        mensajes.append(
                            self._evento(f"Se ejecutó un stop desconocido ({orden.id})", "error")
                        )
                        continue
                    _, operacion = cuenta.aplicar_venta(
                        fecha=ejecucion.fecha,
                        precio=ejecucion.precio,
                        comision=ejecucion.comision,
                        motivo="stop-loss",
                    )
                    self.almacen.registrar_ejecucion(ejecucion)
                    self.almacen.registrar_operacion(operacion)
                    self.almacen.guardar_cuenta(cuenta)
                    mensajes.append(self._evento(self._texto_venta(ejecucion, operacion)))
        except Exception:
            self.cuenta = self.almacen.cargar_cuenta()  # deshace los cambios en memoria
            raise
        posicion = cuenta.posicion
        perdidos = self.broker.stops_perdidos()
        if posicion is not None and posicion.id_orden_stop in perdidos:
            mensajes.append(
                self._evento("El stop de la posición desapareció del exchange", "error")
            )
            mensajes += self._proteger_posicion()
        return mensajes

    def _procesar_vela(self, ts: int, procesada: int | None) -> Mensajes:
        cuenta = self._cuenta
        mensajes: Mensajes = []
        if procesada is not None and ts - procesada > self.tf_ms:
            perdidas = (ts - procesada) // self.tf_ms - 1
            mensajes.append(
                self._evento(
                    f"El bot estuvo parado: {perdidas} velas sin procesar. "
                    "Actúa solo con la última.",
                    "aviso",
                )
            )
        velas = self.fuente.descargar(
            self.simbolo,
            self.temporalidad,
            ts - (self.config.bot.velas_historia - 1) * self.tf_ms,
            ts + self.tf_ms,
        )
        if velas.empty or int(indice_a_ms(velas.index)[-1]) != ts:
            raise VelaNoDisponible(f"La vela de {ms_a_fecha(ts)} aún no está disponible")
        velas = rellenar_huecos(velas, self.temporalidad)
        senal = Senal(self.estrategia.calcular(velas).iloc[-1])
        atr = float(
            calcular_atr(
                velas["maximo"], velas["minimo"], velas["cierre"], self.config.riesgo.atr_periodo
            ).iloc[-1]
        )
        cierre = float(velas["cierre"].iloc[-1])
        fecha_cierre = ms_a_fecha(ts + self.tf_ms)
        try:
            with self.almacen.transaccion():
                decision = self.nucleo.al_cerrar_vela(
                    cuenta, fecha_cierre=fecha_cierre, cierre=cierre, senal=senal, atr=atr
                )
                mensajes += [self._evento(e, "aviso") for e in decision.eventos]
                self.almacen.registrar_capital(
                    fecha_cierre,
                    equity=cuenta.ultimo_equity,
                    efectivo=cuenta.efectivo,
                    precio=cierre,
                    en_mercado=cuenta.posicion is not None,
                )
                for orden in decision.ordenes:
                    mensajes += self._ejecutar(orden, ts)
                self.almacen.guardar_meta("ultima_vela", ts)
                self.almacen.guardar_cuenta(cuenta)
        except Exception:
            self.cuenta = self.almacen.cargar_cuenta()
            raise
        log.info(
            "Vela %s cerrada: %.2f, señal %s, capital %.2f %s, %s, estado %s",
            fecha_cierre,
            cierre,
            senal.value,
            cuenta.ultimo_equity,
            self.cotizada,
            "en posición" if cuenta.posicion else "sin posición",
            cuenta.estado,
        )
        return mensajes

    def _ejecutar(self, orden: Orden, ts: int) -> Mensajes:
        cuenta = self._cuenta
        id_base = f"{self.broker.modo}-{ts}"
        if orden.lado == "venta":
            if cuenta.posicion is None:
                return []
            return self._vender(cuenta.posicion, orden.motivo, f"{id_base}-venta")
        if cuenta.posicion is not None:
            return []

        cotizacion = self.broker.cotizacion()
        limites = self.riesgo.limites
        asequible = limites.redondear(
            cuenta.efectivo
            / (cotizacion.venta * (1 + self.costes.comision_taker) * (1 + _MARGEN_EFECTIVO))
        )
        cantidad = min(orden.cantidad, asequible)
        if not limites.admite(cantidad, cotizacion.venta):
            return [
                self._evento("Compra cancelada: efectivo insuficiente al precio actual", "aviso")
            ]
        ejecucion = self.broker.comprar_mercado(
            cantidad, motivo=orden.motivo, id_cliente=f"{id_base}-compra"
        )
        stop = ejecucion.precio - orden.distancia_stop
        objetivo = ejecucion.precio + orden.distancia_objetivo
        cuenta.aplicar_compra(
            fecha=ejecucion.fecha,
            precio=ejecucion.precio,
            cantidad=ejecucion.cantidad,
            comision=ejecucion.comision,
            stop=stop,
            objetivo=objetivo,
            motivo=orden.motivo,
        )
        self.almacen.registrar_ejecucion(ejecucion)
        orden_stop = self.broker.colocar_stop(
            ejecucion.cantidad, stop, id_cliente=f"{id_base}-stop"
        )
        assert cuenta.posicion is not None
        cuenta.posicion.id_orden_stop = orden_stop.id
        return [
            self._evento(
                f"COMPRA {ejecucion.cantidad:.8f} {self.base} a {self._dinero(ejecucion.precio)} "
                f"(comisión {self._dinero(ejecucion.comision)}). Stop {self._dinero(stop)}, "
                f"objetivo {self._dinero(objetivo)}."
            )
        ]

    def _vender(self, posicion: Posicion, motivo: str, id_cliente: str) -> Mensajes:
        cuenta = self._cuenta
        if posicion.id_orden_stop:
            self.broker.cancelar_stop(posicion.id_orden_stop)  # libera el saldo bloqueado
        ejecucion = self.broker.vender_mercado(
            posicion.cantidad, motivo=motivo, id_cliente=id_cliente
        )
        _, operacion = cuenta.aplicar_venta(
            fecha=ejecucion.fecha,
            precio=ejecucion.precio,
            comision=ejecucion.comision,
            motivo=motivo,
        )
        self.almacen.registrar_ejecucion(ejecucion)
        self.almacen.registrar_operacion(operacion)
        self.almacen.guardar_cuenta(cuenta)
        return [self._evento(self._texto_venta(ejecucion, operacion))]

    # --- Bucle --------------------------------------------------------------------------

    def _espera(self) -> float:
        """Segundos hasta la próxima pasada: el intervalo o el próximo cierre, lo primero."""
        ahora = self.reloj()
        siguiente_cierre = (
            inicio_vela(ahora, self.tf_ms) + self.tf_ms + self.config.bot.retraso_cierre_s * 1000
        )
        return max(1.0, min(float(self.config.bot.intervalo_s), (siguiente_cierre - ahora) / 1000))

    def ejecutar(self, *, max_ciclos: int | None = None, forzar: bool = False) -> None:
        """Arranca y repite ciclos hasta que se pida parar o haya demasiados errores seguidos."""
        self.arrancar(forzar=forzar)
        errores = 0
        ciclos = 0
        motivo_fin = "a petición"
        try:
            while not self._parar:
                try:
                    self.ciclo()
                    errores = 0
                except VelaNoDisponible as e:
                    log.info("%s; se reintenta en la próxima pasada", e)
                except ccxt.NetworkError as e:
                    errores += 1
                    log.warning("Error de red (%d seguidos): %s", errores, e)
                except (ErrorBroker, ccxt.BaseError) as e:
                    errores += 1
                    self._avisar([self._evento(f"Error del exchange: {e}", "error")])
                except Exception as e:
                    errores += 1
                    log.exception("Error inesperado en el ciclo")
                    self._avisar(
                        [self._evento(f"Error inesperado: {type(e).__name__}: {e}", "error")]
                    )
                if errores >= self.config.bot.max_errores_seguidos:
                    motivo_fin = f"{errores} errores seguidos"
                    break
                ciclos += 1
                if max_ciclos is not None and ciclos >= max_ciclos:
                    break
                self.dormir(self._espera())
        except KeyboardInterrupt:
            motivo_fin = "interrumpido por el usuario"
        finally:
            self.almacen.guardar_meta("latido", 0)
            nivel: Nivel = "error" if motivo_fin.endswith("seguidos") else "info"
            self._avisar(
                [
                    self._evento(
                        f"Bot detenido ({motivo_fin}). "
                        "Si hay posición, su stop sigue en el exchange.",
                        nivel,
                    )
                ]
            )
