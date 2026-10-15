"""Configuración de pytest. Ningún test accede a internet salvo los marcados ``red``."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--red",
        action="store_true",
        default=False,
        help="ejecuta también los tests que acceden a internet",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--red"):
        return
    saltar = pytest.mark.skip(reason="accede a internet: usa --red")
    for item in items:
        if "red" in item.keywords:
            item.add_marker(saltar)


@pytest.fixture(autouse=True)
def _limpiar_logging() -> Iterator[None]:
    """Cierra los manejadores de log tras cada test (Windows bloquea los ficheros abiertos)."""
    yield
    raiz = logging.getLogger()
    for manejador in list(raiz.handlers):
        if getattr(manejador, "_gaa", False):
            raiz.removeHandler(manejador)
            manejador.close()
