"""Cliente de Telegram, notificadores, comandos desde el móvil y fichero STOP (sin red)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from gaainversiones.bot.killswitch import FicheroParada, OrdenManual, escribir_parada
from gaainversiones.bot.telegram import (
    AYUDA,
    ClienteTelegram,
    ComandosTelegram,
    NotificadorCompuesto,
    NotificadorTelegram,
)
from gaainversiones.config import ConfigRegistro
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.registro import configurar_registro

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"


class Respuesta:
    def __init__(self, estado: int, cuerpo: dict[str, Any]) -> None:
        self.status_code = estado
        self._cuerpo = cuerpo

    def json(self) -> dict[str, Any]:
        return self._cuerpo


class SesionFalsa:
    """Imita ``requests.Session.post``: devuelve las respuestas programadas en orden."""

    def __init__(self, *respuestas: Respuesta | Exception) -> None:
        self.respuestas = list(respuestas)
        self.peticiones: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, json: dict[str, Any], timeout: float) -> Respuesta:
        self.peticiones.append((url, json))
        siguiente = (
            self.respuestas.pop(0)
            if self.respuestas
            else Respuesta(200, {"ok": True, "result": []})
        )
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente


OK = Respuesta(200, {"ok": True, "result": {}})


def _cliente(
    *respuestas: Respuesta | Exception,
) -> tuple[ClienteTelegram, SesionFalsa, list[float]]:
    sesion = SesionFalsa(*respuestas)
    esperas: list[float] = []
    return ClienteTelegram(TOKEN, "42", sesion=sesion, dormir=esperas.append), sesion, esperas


def test_enviar_mensaje() -> None:
    cliente, sesion, _ = _cliente(OK)
    assert cliente.enviar("hola")
    url, datos = sesion.peticiones[0]
    assert url.endswith("/sendMessage") and datos["chat_id"] == "42" and datos["text"] == "hola"


def test_mensaje_largo_se_recorta() -> None:
    cliente, sesion, _ = _cliente(OK)
    cliente.enviar("x" * 10_000)
    assert len(sesion.peticiones[0][1]["text"]) <= 4096


def test_reintenta_errores_de_servidor_y_limite() -> None:
    limite = Respuesta(429, {"ok": False, "parameters": {"retry_after": 7}})
    cliente, sesion, esperas = _cliente(Respuesta(502, {}), limite, OK)
    assert cliente.enviar("hola")
    assert len(sesion.peticiones) == 3 and esperas == [2, 7]


def test_fallo_de_red_no_lanza_ni_filtra_el_token(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    configurar_registro(ConfigRegistro(), tmp_path, [TOKEN], consola=False)
    error = requests.ConnectionError(f"fallo conectando a https://api.telegram.org/bot{TOKEN}/x")
    cliente, _, _ = _cliente(error, error, error)
    with caplog.at_level(logging.WARNING):
        assert cliente.enviar("hola") is False
    assert "ConnectionError" in caplog.text
    contenido = (tmp_path / "gaainversiones.log").read_text(encoding="utf-8")
    assert TOKEN not in caplog.text and TOKEN not in contenido


def test_peticion_rechazada() -> None:
    cliente, sesion, _ = _cliente(Respuesta(400, {"ok": False, "description": "chat not found"}))
    assert cliente.enviar("hola") is False
    assert len(sesion.peticiones) == 1  # un error del cliente no se reintenta


class ClienteGrabador:
    def __init__(self) -> None:
        self.enviados: list[str] = []
        self.chat_id = "42"
        self.pendientes: list[dict[str, Any]] = []
        self.pedido_desde: list[int | None] = []

    def enviar(self, texto: str, chat_id: str | None = None) -> bool:
        self.enviados.append(texto)
        return True

    def actualizaciones(self, desde: int | None = None) -> list[dict[str, Any]]:
        self.pedido_desde.append(desde)
        pendientes, self.pendientes = self.pendientes, []
        return pendientes


def test_notificador_nivel_minimo_y_formato() -> None:
    cliente = ClienteGrabador()
    notificador = NotificadorTelegram(
        cliente, prefijo="GaaInversiones · paper", nivel_minimo="aviso"
    )  # type: ignore[arg-type]
    notificador.enviar("detalle", nivel="info")
    notificador.enviar("cuidado", nivel="aviso")
    assert cliente.enviados == ["⚠️ GaaInversiones · paper\ncuidado"]


def test_notificador_agrupa_errores_repetidos() -> None:
    cliente = ClienteGrabador()
    reloj = {"ms": 0}
    notificador = NotificadorTelegram(
        cliente,
        prefijo="p",
        agrupar_errores_s=600,
        reloj=lambda: reloj["ms"],  # type: ignore[arg-type]
    )
    for minuto in (0, 1, 2, 3):
        reloj["ms"] = minuto * 60_000
        notificador.enviar("Error de red", nivel="error")
    assert len(cliente.enviados) == 1
    reloj["ms"] = 11 * 60_000
    notificador.enviar("Error de red", nivel="error")
    assert "se repitió 3 veces más" in cliente.enviados[-1]
    notificador.enviar("Otro error distinto", nivel="error")
    assert len(cliente.enviados) == 3


def test_compuesto_aisla_fallos() -> None:
    class Roto:
        def enviar(self, texto: str, *, nivel: str = "info") -> None:
            raise RuntimeError("caído")

    cliente = ClienteGrabador()
    NotificadorCompuesto(Roto(), NotificadorTelegram(cliente, prefijo="p")).enviar("hola")  # type: ignore[arg-type]
    assert len(cliente.enviados) == 1


# --- Comandos desde el móvil ------------------------------------------------------------------


@pytest.fixture
def almacen(tmp_path: Path) -> Iterator[AlmacenEstado]:
    with AlmacenEstado(tmp_path / "e.sqlite", modo="paper", exchange="x", simbolo="BTC/EUR") as a:
        yield a


def _mensaje(update_id: int, texto: str, chat: int = 42) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": texto}}


def test_comandos_autorizados(almacen: AlmacenEstado) -> None:
    cliente = ClienteGrabador()
    comandos = ComandosTelegram(cliente, "42", almacen)  # type: ignore[arg-type]
    cliente.pendientes = [
        _mensaje(10, "/estado"),
        _mensaje(11, "/parar@MiBot"),
        _mensaje(12, "hola"),
    ]
    assert comandos.revisar() == [
        OrdenManual("estado", "Telegram"),
        OrdenManual("parar", "Telegram"),
    ]
    assert cliente.enviados == [AYUDA]
    assert almacen.meta_entero("telegram_offset") == 13
    comandos.revisar()
    assert cliente.pedido_desde[-1] == 13  # no se repiten mensajes ya leídos


def test_chat_no_autorizado_se_ignora(almacen: AlmacenEstado) -> None:
    cliente = ClienteGrabador()
    comandos = ComandosTelegram(cliente, "42", almacen)  # type: ignore[arg-type]
    cliente.pendientes = [_mensaje(1, "/liquidar", chat=666), _mensaje(2, "/confirmar", chat=666)]
    assert comandos.revisar() == []
    assert cliente.enviados == []
    assert "no autorizado (666)" in almacen.eventos()["texto"].iloc[0]


def test_liquidar_exige_confirmacion_a_tiempo(almacen: AlmacenEstado) -> None:
    cliente = ClienteGrabador()
    reloj = {"ms": 0}
    comandos = ComandosTelegram(cliente, "42", almacen, reloj=lambda: reloj["ms"])  # type: ignore[arg-type]
    cliente.pendientes = [_mensaje(1, "/liquidar")]
    assert comandos.revisar() == []
    assert "/confirmar" in cliente.enviados[-1]
    reloj["ms"] = 60_000
    cliente.pendientes = [_mensaje(2, "/confirmar")]
    assert comandos.revisar() == [OrdenManual("liquidar", "Telegram")]
    # Una confirmación tardía no vale.
    cliente.pendientes = [_mensaje(3, "/liquidar")]
    comandos.revisar()
    reloj["ms"] = 300_000
    cliente.pendientes = [_mensaje(4, "/confirmar")]
    assert comandos.revisar() == []
    assert "nada que confirmar" in cliente.enviados[-1]


def test_fichero_stop(tmp_path: Path) -> None:
    ruta = tmp_path / "STOP"
    fuente = FicheroParada(ruta)
    assert fuente.revisar() == []
    escribir_parada(ruta, "liquidar")
    assert fuente.revisar() == [OrdenManual("liquidar", "fichero STOP")]
    assert not ruta.exists()  # se consume
    ruta.write_text("", encoding="utf-8")  # vacío: solo parar
    assert fuente.revisar() == [OrdenManual("parar", "fichero STOP")]
