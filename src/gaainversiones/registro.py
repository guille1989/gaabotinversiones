"""Logging a consola y a fichero rotativo, con enmascarado de secretos.

Los manejadores se instalan en el logger raíz, así que también pasan por el
enmascarado los mensajes de librerías de terceros (urllib3 puede incluir en sus
avisos la URL de Telegram, que contiene el token). Las marcas de tiempo van en UTC.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from gaainversiones.config import ConfigRegistro

NOMBRE_LOGGER = "gaainversiones"
MASCARA = "***"
FORMATO = "%(asctime)sZ %(levelname)-7s %(name)s: %(message)s"
FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"

# Por si un token de Telegram aparece sin estar registrado como secreto.
# Sin \b al principio: en las URL va pegado a "bot" (/bot123456:ABC...).
_PATRON_TOKEN_TELEGRAM = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")
# Longitud mínima para enmascarar un secreto: evita destrozar los logs con valores triviales.
_LONGITUD_MINIMA_SECRETO = 6


def enmascarar(texto: str, secretos: Iterable[str]) -> str:
    """Sustituye cada secreto (y cualquier cosa con forma de token de Telegram) por ``***``."""
    for secreto in sorted(set(secretos), key=len, reverse=True):
        if len(secreto) >= _LONGITUD_MINIMA_SECRETO:
            texto = texto.replace(secreto, MASCARA)
    return _PATRON_TOKEN_TELEGRAM.sub(MASCARA, texto)


class FormateadorSeguro(logging.Formatter):
    """Formateador que enmascara los secretos en el texto final, trazas de excepción incluidas."""

    def __init__(self, secretos: Iterable[str] = ()) -> None:
        super().__init__(FORMATO, FORMATO_FECHA)
        self.converter = time.gmtime
        self._secretos = tuple(secretos)

    def format(self, record: logging.LogRecord) -> str:
        return enmascarar(super().format(record), self._secretos)


def configurar_registro(
    config: ConfigRegistro,
    dir_logs: Path,
    secretos: Iterable[str] = (),
    *,
    consola: bool = True,
) -> logging.Logger:
    """Configura el logging del proyecto. Es idempotente: se puede llamar varias veces.

    Args:
        config: sección ``registro`` de la configuración.
        dir_logs: carpeta del fichero de log (se crea si no existe).
        secretos: valores que nunca deben aparecer en los logs.
        consola: si es False solo se escribe en fichero.

    Returns:
        El logger raíz del proyecto (``gaainversiones``).
    """
    formateador = FormateadorSeguro(secretos)
    dir_logs.mkdir(parents=True, exist_ok=True)

    raiz = logging.getLogger()
    for manejador in list(raiz.handlers):
        if getattr(manejador, "_gaa", False):
            raiz.removeHandler(manejador)
            manejador.close()

    manejadores: list[logging.Handler] = [
        RotatingFileHandler(
            dir_logs / config.fichero,
            maxBytes=config.max_bytes,
            backupCount=config.copias,
            encoding="utf-8",
        )
    ]
    if consola:
        manejadores.append(logging.StreamHandler(sys.stderr))
    for manejador in manejadores:
        manejador.setFormatter(formateador)
        manejador._gaa = True  # type: ignore[attr-defined]
        raiz.addHandler(manejador)

    # Terceros solo a partir de WARNING; el proyecto con el nivel configurado.
    raiz.setLevel(logging.WARNING)
    logging.captureWarnings(True)
    proyecto = logging.getLogger(NOMBRE_LOGGER)
    proyecto.setLevel(config.nivel)
    return proyecto
