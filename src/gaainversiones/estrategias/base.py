"""Interfaz común de las estrategias y registro por nombre.

Una estrategia solo decide *qué le gustaría hacer* (comprar, vender o nada) mirando
velas cerradas. No sabe de capital, tamaños ni stops: eso es cosa de la capa de riesgo,
que además puede vetarla.

Para añadir una estrategia nueva:

1. Crea un módulo en ``gaainversiones/estrategias/`` con una subclase de ``Estrategia``
   decorada con ``@registrar`` y con un ``nombre`` único.
2. Impórtalo en ``gaainversiones/estrategias/__init__.py``.
3. Pon ese nombre en ``estrategia.nombre`` de ``config.yaml``.

``calcular`` debe ser causal: la señal de la vela t solo puede depender de velas hasta
t. ``verificar_sin_lookahead`` lo comprueba y los tests lo exigen a cada estrategia.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError


class Senal(StrEnum):
    """Lo que la estrategia querría hacer al cierre de una vela."""

    COMPRAR = "comprar"
    VENDER = "vender"
    MANTENER = "mantener"


class ParametrosEstrategia(BaseModel):
    """Base de los parámetros de una estrategia: inmutables y sin campos desconocidos."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorLookahead(AssertionError):
    """La estrategia usa información de velas futuras."""


class Estrategia(ABC):
    """Estrategia de trading: velas cerradas -> una señal por vela."""

    nombre: ClassVar[str]
    Parametros: ClassVar[type[ParametrosEstrategia]] = ParametrosEstrategia
    didactica: ClassVar[bool] = False

    def __init__(self, **parametros: Any) -> None:
        try:
            self.parametros = self.Parametros(**parametros)
        except ValidationError as e:
            detalles = "; ".join(
                f"{'.'.join(str(p) for p in error['loc']) or 'parámetros'}: {error['msg']}"
                for error in e.errors()
            )
            raise ValueError(f"Parámetros no válidos para {self.nombre}: {detalles}") from None

    @property
    @abstractmethod
    def velas_necesarias(self) -> int:
        """Velas de historia mínimas para que la señal tenga sentido."""

    @abstractmethod
    def calcular(self, velas: pd.DataFrame) -> pd.Series:
        """Señal (``Senal``) para cada vela de ``velas``, usando solo el pasado de cada una.

        Donde no hay historia suficiente la señal debe ser ``Senal.MANTENER``.
        """

    def senal_actual(self, velas: pd.DataFrame) -> Senal:
        """Señal de la última vela cerrada (uso en vivo)."""
        if len(velas) < self.velas_necesarias:
            return Senal.MANTENER
        return Senal(self.calcular(velas).iloc[-1])

    def describir(self) -> str:
        """Nombre y parámetros, p. ej. ``cruce_medias_rsi(media_rapida=20, ...)``."""
        parametros = ", ".join(f"{k}={v}" for k, v in self.parametros.model_dump().items())
        return f"{self.nombre}({parametros})"

    @staticmethod
    def serie_de_senales(valores: np.ndarray, indice: pd.Index) -> pd.Series:
        """Construye la serie de señales con el tipo esperado por el resto del sistema."""
        return pd.Series(valores, index=indice, dtype=object, name="senal")


REGISTRO: dict[str, type[Estrategia]] = {}


def registrar(clase: type[Estrategia]) -> type[Estrategia]:
    """Decorador que da de alta una estrategia por su ``nombre``."""
    if clase.nombre in REGISTRO and REGISTRO[clase.nombre] is not clase:
        raise ValueError(f"Ya existe una estrategia llamada {clase.nombre!r}")
    REGISTRO[clase.nombre] = clase
    return clase


def crear_estrategia(nombre: str, parametros: Mapping[str, Any] | None = None) -> Estrategia:
    """Instancia una estrategia registrada.

    Raises:
        ValueError: si no existe o sus parámetros no son válidos.
    """
    if nombre not in REGISTRO:
        disponibles = ", ".join(sorted(REGISTRO)) or "ninguna"
        raise ValueError(f"Estrategia desconocida {nombre!r}. Disponibles: {disponibles}")
    return REGISTRO[nombre](**dict(parametros or {}))


def verificar_sin_lookahead(
    estrategia: Estrategia, velas: pd.DataFrame, *, puntos: int = 30, semilla: int = 0
) -> None:
    """Comprueba que la estrategia no mira al futuro.

    Compara la señal de varias velas calculada con todo el histórico con la calculada
    cortando el histórico justo en esa vela. Si alguna difiere, la estrategia estaba
    usando datos posteriores.

    Raises:
        ErrorLookahead: con la primera vela en la que difieren.
    """
    completas = estrategia.calcular(velas)
    inicio = min(estrategia.velas_necesarias, len(velas) - 1)
    rng = np.random.default_rng(semilla)
    candidatos = np.arange(inicio, len(velas))
    elegidos = rng.choice(candidatos, size=min(puntos, len(candidatos)), replace=False)
    for i in sorted({*elegidos.tolist(), len(velas) - 1}):
        parcial = estrategia.calcular(velas.iloc[: i + 1]).iloc[-1]
        if parcial != completas.iloc[i]:
            raise ErrorLookahead(
                f"{estrategia.describir()}: en {velas.index[i]} la señal con todo el histórico "
                f"es {completas.iloc[i]!r} pero con datos solo hasta esa vela es {parcial!r}"
            )
