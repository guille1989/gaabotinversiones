"""Logging: fichero rotativo, UTC y enmascarado de secretos."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from gaainversiones.config import ConfigRegistro
from gaainversiones.registro import configurar_registro, enmascarar

SECRETO = "mi-clave-api-ULTRASECRETA"
TOKEN_TELEGRAM = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"


def _configurar(tmp_path: Path, secretos: tuple[str, ...] = (SECRETO,)) -> Path:
    configurar_registro(ConfigRegistro(nivel="DEBUG"), tmp_path, secretos)
    return tmp_path / "gaainversiones.log"


def _leer(ruta: Path) -> str:
    for manejador in logging.getLogger().handlers:
        manejador.flush()
    return ruta.read_text(encoding="utf-8")


def test_enmascara_en_fichero_y_consola(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fichero = _configurar(tmp_path)
    logging.getLogger("gaainversiones.prueba").info("conectando con clave %s", SECRETO)
    contenido = _leer(fichero)
    assert SECRETO not in contenido and "conectando con clave ***" in contenido
    assert SECRETO not in capsys.readouterr().err


def test_enmascara_trazas_de_excepcion(tmp_path: Path) -> None:
    fichero = _configurar(tmp_path)
    try:
        raise RuntimeError(f"fallo autenticando con {SECRETO}")
    except RuntimeError:
        logging.getLogger("gaainversiones.prueba").exception("error")
    contenido = _leer(fichero)
    assert "RuntimeError" in contenido and SECRETO not in contenido


def test_enmascara_tokens_de_telegram_no_registrados(tmp_path: Path) -> None:
    fichero = _configurar(tmp_path, secretos=())
    # Un aviso de urllib3 con la URL de Telegram (librería de terceros).
    logging.getLogger("urllib3.connectionpool").warning(
        "Retrying: /bot%s/sendMessage", TOKEN_TELEGRAM
    )
    contenido = _leer(fichero)
    assert TOKEN_TELEGRAM not in contenido and "/bot***/sendMessage" in contenido


def test_terceros_solo_desde_warning(tmp_path: Path) -> None:
    fichero = _configurar(tmp_path)
    logging.getLogger("urllib3.connectionpool").debug("detalle ruidoso")
    logging.getLogger("gaainversiones.prueba").debug("detalle del proyecto")
    contenido = _leer(fichero)
    assert "detalle ruidoso" not in contenido and "detalle del proyecto" in contenido


def test_idempotente(tmp_path: Path) -> None:
    fichero = _configurar(tmp_path)
    _configurar(tmp_path)
    logging.getLogger("gaainversiones.prueba").info("una sola vez")
    assert _leer(fichero).count("una sola vez") == 1


def test_marca_de_tiempo_en_utc(tmp_path: Path) -> None:
    fichero = _configurar(tmp_path)
    logging.getLogger("gaainversiones.prueba").info("hora")
    primera_linea = _leer(fichero).splitlines()[0]
    assert primera_linea[19] == "Z"  # "2026-10-15 12:00:00Z INFO ..."


def test_enmascarar_no_toca_valores_cortos() -> None:
    assert enmascarar("modo si", ["si"]) == "modo si"
    assert enmascarar(f"a {SECRETO} b {SECRETO}", [SECRETO]) == "a *** b ***"
