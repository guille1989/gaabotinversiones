"""Creación de clientes ccxt y reintentos ante errores de red."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import ccxt

log = logging.getLogger(__name__)

T = TypeVar("T")


def crear_exchange(
    nombre: str,
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    timeout_ms: int = 30_000,
) -> Any:
    """Crea un cliente ccxt con el limitador de peticiones activado.

    Sin claves, el cliente solo puede usar la API pública (precios, velas, libro de órdenes).
    """
    if nombre not in ccxt.exchanges:
        raise ValueError(f"exchange desconocido para ccxt: {nombre!r}")
    parametros: dict[str, Any] = {"enableRateLimit": True, "timeout": timeout_ms}
    if api_key and api_secret:
        parametros.update(apiKey=api_key, secret=api_secret)
    return getattr(ccxt, nombre)(parametros)


def con_reintentos(
    funcion: Callable[[], T],
    *,
    descripcion: str,
    intentos: int = 5,
    espera_base: float = 2.0,
    espera_max: float = 60.0,
    dormir: Callable[[float], None] = time.sleep,
) -> T:
    """Ejecuta ``funcion`` reintentando con espera exponencial ante errores de red.

    Solo se reintentan los ``ccxt.NetworkError`` (timeouts, exchange no disponible,
    límite de peticiones...). El resto de errores (símbolo inexistente, saldo
    insuficiente...) se propagan al instante: reintentarlos no los arregla.

    Ojo: no usar para crear órdenes sin un identificador de cliente; un timeout no
    garantiza que la orden no llegara al exchange.
    """
    for intento in range(1, intentos + 1):
        try:
            return funcion()
        except ccxt.NetworkError as e:
            if intento == intentos:
                log.error("%s: error de red tras %d intentos: %s", descripcion, intentos, e)
                raise
            espera = min(espera_base * 2 ** (intento - 1), espera_max)
            log.warning(
                "%s: %s (%s). Reintento %d de %d en %.0f s",
                descripcion,
                type(e).__name__,
                str(e)[:200],
                intento,
                intentos - 1,
                espera,
            )
            dormir(espera)
    raise AssertionError("inalcanzable")
