"""Avisos del bot (operaciones, paradas, errores). En la fase 4 se añade Telegram."""

from __future__ import annotations

import logging
from typing import Literal, Protocol

Nivel = Literal["info", "aviso", "error"]

log = logging.getLogger("gaainversiones.avisos")

_NIVELES = {"info": logging.INFO, "aviso": logging.WARNING, "error": logging.ERROR}


class Notificador(Protocol):
    def enviar(self, texto: str, *, nivel: Nivel = "info") -> None: ...


class NotificadorLog:
    """Escribe los avisos en el log."""

    def enviar(self, texto: str, *, nivel: Nivel = "info") -> None:
        log.log(_NIVELES[nivel], texto)
