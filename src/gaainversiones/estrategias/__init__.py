"""Estrategias de trading. Importar aquí cada módulo para que quede registrado."""

from gaainversiones.estrategias import cruce_medias_rsi  # noqa: F401  (registra la estrategia)
from gaainversiones.estrategias.base import (
    REGISTRO,
    ErrorLookahead,
    Estrategia,
    ParametrosEstrategia,
    Senal,
    crear_estrategia,
    registrar,
    verificar_sin_lookahead,
)

__all__ = [
    "REGISTRO",
    "ErrorLookahead",
    "Estrategia",
    "ParametrosEstrategia",
    "Senal",
    "crear_estrategia",
    "registrar",
    "verificar_sin_lookahead",
]
