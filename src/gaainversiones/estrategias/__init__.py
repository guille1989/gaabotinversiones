"""Estrategias de trading. Importar aquí cada módulo para que quede registrado."""

# Importar cada módulo registra su estrategia.
from gaainversiones.estrategias import (  # noqa: F401
    cruce_medias_rsi,
    reversion_rsi,
    ruptura_donchian,
    tendencia_media,
)
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
