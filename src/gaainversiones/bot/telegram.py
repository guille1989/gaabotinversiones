"""Telegram: avisos al móvil y órdenes desde el móvil (/parar, /liquidar, /estado).

- Si Telegram no responde, el bot sigue operando: se reintenta un par de veces y, si no,
  el aviso queda solo en el log. Nada de aquí lanza excepciones hacia el bot.
- El token va en la URL de la API; el logging lo enmascara y aquí nunca se registra el
  texto de las excepciones de red (podría incluir la URL).
- Solo se obedecen mensajes de ``GAA_TELEGRAM_CHAT_ID``; los demás se ignoran y quedan
  registrados como evento.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from gaainversiones.bot.killswitch import OrdenManual
from gaainversiones.bot.notificador import Nivel, Notificador
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.tiempo import ahora_ms

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
LIMITE_TEXTO = 4096
_ORDEN_NIVEL = {"info": 0, "aviso": 1, "error": 2}
_ICONO = {"info": "ℹ️", "aviso": "⚠️", "error": "🚨"}

AYUDA = (
    "Comandos:\n"
    "/estado - capital, posición y estado del bot\n"
    "/parar - detiene el bot y MANTIENE el stop-loss de la posición\n"
    "/liquidar - vende toda la posición a mercado y detiene el bot (pide confirmación)\n"
    "/ayuda - esta ayuda"
)


class ClienteTelegram:
    """Cliente mínimo de la Bot API de Telegram."""

    def __init__(
        self,
        token: str,
        chat_id: str | None = None,
        *,
        sesion: Any = None,
        timeout: float = 10,
        intentos: int = 3,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self._sesion = sesion or requests.Session()
        self._timeout = timeout
        self._intentos = intentos
        self._dormir = dormir

    def _llamar(self, metodo: str, datos: dict[str, Any]) -> dict[str, Any] | None:
        url = f"{API}/bot{self._token}/{metodo}"
        for intento in range(1, self._intentos + 1):
            espera: float = 2**intento
            try:
                respuesta = self._sesion.post(url, json=datos, timeout=self._timeout)
            except requests.RequestException as e:
                log.warning("Telegram %s: error de red (%s)", metodo, type(e).__name__)
            else:
                try:
                    cuerpo: dict[str, Any] = respuesta.json()
                except ValueError:
                    cuerpo = {}
                if respuesta.status_code == 429:
                    espera = float((cuerpo.get("parameters") or {}).get("retry_after", 5))
                    log.warning("Telegram %s: límite de mensajes, espero %.0f s", metodo, espera)
                elif respuesta.status_code >= 500:
                    log.warning("Telegram %s: error %s del servidor", metodo, respuesta.status_code)
                elif not cuerpo.get("ok"):
                    log.warning("Telegram %s rechazado: %s", metodo, cuerpo.get("description"))
                    return None
                else:
                    return cuerpo
            if intento < self._intentos:
                self._dormir(min(espera, 30))
        return None

    def enviar(self, texto: str, chat_id: str | None = None) -> bool:
        """Envía un mensaje. Devuelve False (sin lanzar) si no se pudo."""
        destino = chat_id or self.chat_id
        if not destino:
            log.warning("Telegram: falta el chat id")
            return False
        if len(texto) > LIMITE_TEXTO:
            texto = texto[: LIMITE_TEXTO - 20] + "\n… (recortado)"
        datos = {"chat_id": destino, "text": texto, "disable_web_page_preview": True}
        return self._llamar("sendMessage", datos) is not None

    def actualizaciones(self, desde: int | None = None) -> list[dict[str, Any]]:
        """Mensajes recibidos por el bot (sin esperar)."""
        datos: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message"]}
        if desde is not None:
            datos["offset"] = desde
        cuerpo = self._llamar("getUpdates", datos)
        return list(cuerpo.get("result", [])) if cuerpo else []


class NotificadorTelegram:
    """Envía los avisos del bot a Telegram, agrupando errores repetidos."""

    def __init__(
        self,
        cliente: ClienteTelegram,
        *,
        prefijo: str,
        nivel_minimo: Nivel = "info",
        agrupar_errores_s: int = 600,
        reloj: Callable[[], int] = ahora_ms,
    ) -> None:
        self.cliente = cliente
        self.prefijo = prefijo
        self.nivel_minimo = nivel_minimo
        self.agrupar_ms = agrupar_errores_s * 1000
        self.reloj = reloj
        self._errores: dict[str, tuple[int, int]] = {}  # texto -> (último envío, repeticiones)

    def enviar(self, texto: str, *, nivel: Nivel = "info") -> None:
        if _ORDEN_NIVEL[nivel] < _ORDEN_NIVEL[self.nivel_minimo]:
            return
        if nivel == "error":
            clave = texto[:200]
            ahora = self.reloj()
            ultimo, repetidos = self._errores.get(clave, (None, 0))
            if ultimo is not None and ahora - ultimo < self.agrupar_ms:
                self._errores[clave] = (ultimo, repetidos + 1)
                return
            if repetidos:
                texto += f"\n(este error se repitió {repetidos} veces más)"
            self._errores[clave] = (ahora, 0)
        self.cliente.enviar(f"{_ICONO[nivel]} {self.prefijo}\n{texto}")


class NotificadorCompuesto:
    """Reparte cada aviso entre varios notificadores; el fallo de uno no afecta a los demás."""

    def __init__(self, *notificadores: Notificador) -> None:
        self.notificadores = notificadores

    def enviar(self, texto: str, *, nivel: Nivel = "info") -> None:
        for notificador in self.notificadores:
            try:
                notificador.enviar(texto, nivel=nivel)
            except Exception:
                log.exception("Fallo al enviar un aviso con %s", type(notificador).__name__)


# --- Órdenes desde el móvil -----------------------------------------------------------------


class ComandosTelegram:
    """Lee los comandos enviados al bot de Telegram desde el chat autorizado.

    ``/liquidar`` necesita confirmarse con ``/confirmar`` en los dos minutos siguientes.
    La posición en la cola de mensajes se guarda en el almacén, así que los comandos
    enviados con el bot apagado se ejecutan al arrancar.
    """

    def __init__(
        self,
        cliente: ClienteTelegram,
        chat_id: str,
        almacen: AlmacenEstado,
        *,
        reloj: Callable[[], int] = ahora_ms,
        confirmacion_s: int = 120,
    ) -> None:
        self.cliente = cliente
        self.chat_id = str(chat_id)
        self.almacen = almacen
        self.reloj = reloj
        self.confirmacion_ms = confirmacion_s * 1000
        self._liquidar_pedido: int | None = None

    def revisar(self) -> list[OrdenManual]:
        desde = self.almacen.meta_entero("telegram_offset")
        try:
            mensajes = self.cliente.actualizaciones(desde)
        except Exception:
            log.exception("No se pudieron leer los mensajes de Telegram")
            return []
        ordenes: list[OrdenManual] = []
        siguiente = desde
        for actualizacion in mensajes:
            siguiente = int(actualizacion["update_id"]) + 1
            mensaje = actualizacion.get("message") or {}
            chat = str((mensaje.get("chat") or {}).get("id"))
            comando = (mensaje.get("text") or "").strip().split("@")[0].split(" ")[0].lower()
            if chat != self.chat_id:
                texto = f"Mensaje de Telegram de un chat no autorizado ({chat}) ignorado"
                log.warning(texto)
                self.almacen.registrar_evento(texto, nivel="aviso")
                continue
            orden = self._interpretar(comando)
            if orden is not None:
                ordenes.append(orden)
        if siguiente != desde:
            self.almacen.guardar_meta("telegram_offset", siguiente)
        return ordenes

    def _interpretar(self, comando: str) -> OrdenManual | None:
        if comando == "/parar":
            return OrdenManual("parar", "Telegram")
        if comando == "/estado":
            return OrdenManual("estado", "Telegram")
        if comando == "/liquidar":
            self._liquidar_pedido = self.reloj()
            self.cliente.enviar(
                "¿Seguro? Se venderá TODA la posición a mercado y el bot se detendrá. "
                "Envía /confirmar en los próximos 2 minutos."
            )
            return None
        if comando == "/confirmar":
            pedido, self._liquidar_pedido = self._liquidar_pedido, None
            if pedido is not None and self.reloj() - pedido <= self.confirmacion_ms:
                return OrdenManual("liquidar", "Telegram")
            self.cliente.enviar("No hay nada que confirmar (o pasaron más de 2 minutos).")
            return None
        self.cliente.enviar(AYUDA)
        return None
