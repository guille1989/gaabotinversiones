"""Utilidades de tiempo.

Todo el proyecto trabaja en UTC. Internamente las marcas de tiempo son enteros en
milisegundos desde epoch (el mismo convenio que ccxt); en los DataFrame se usa un
``DatetimeIndex`` con zona UTC llamado ``fecha``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from datetime import datetime

import numpy as np
import pandas as pd

_MS_POR_UNIDAD = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
_PATRON_TEMPORALIDAD = re.compile(r"^(\d+)([mhdw])$")


def timeframe_a_ms(temporalidad: str) -> int:
    """Convierte una temporalidad estilo ccxt ('15m', '4h', '1d', '1w') a milisegundos.

    Raises:
        ValueError: si el formato no es válido.
    """
    coincidencia = _PATRON_TEMPORALIDAD.match(temporalidad)
    if not coincidencia or int(coincidencia.group(1)) <= 0:
        raise ValueError(
            f"Temporalidad no válida: {temporalidad!r} (ejemplos: '15m', '1h', '4h', '1d')"
        )
    return int(coincidencia.group(1)) * _MS_POR_UNIDAD[coincidencia.group(2)]


def minutos_a_timeframe(minutos: int) -> str:
    """Convierte minutos a la temporalidad más compacta (240 -> '4h', 1440 -> '1d')."""
    if minutos <= 0:
        raise ValueError(f"Minutos no válidos: {minutos}")
    ms = minutos * 60_000
    for unidad in ("w", "d", "h"):
        if ms % _MS_POR_UNIDAD[unidad] == 0:
            return f"{ms // _MS_POR_UNIDAD[unidad]}{unidad}"
    return f"{minutos}m"


def ahora_ms() -> int:
    """Instante actual en milisegundos UTC."""
    return int(time.time() * 1000)


def fecha_a_ms(fecha: str | datetime | pd.Timestamp) -> int:
    """Convierte una fecha a milisegundos UTC. Las fechas sin zona se toman como UTC."""
    marca = pd.Timestamp(fecha)
    marca = marca.tz_localize("UTC") if marca.tzinfo is None else marca.tz_convert("UTC")
    return int(marca.value // 1_000_000)


def ms_a_fecha(ms: int) -> pd.Timestamp:
    """Convierte milisegundos UTC a ``pd.Timestamp`` con zona UTC."""
    return pd.Timestamp(int(ms), unit="ms", tz="UTC")


def inicio_vela(ts_ms: int, tf_ms: int) -> int:
    """Inicio de la vela que contiene ``ts_ms`` (alineada a epoch; no válido para '1w')."""
    return ts_ms - ts_ms % tf_ms


def vela_cerrada(ts_vela_ms: int, tf_ms: int, ahora: int) -> bool:
    """True si la vela que empieza en ``ts_vela_ms`` ya ha cerrado en el instante ``ahora``."""
    return ts_vela_ms + tf_ms <= ahora


def indice_a_ms(indice: pd.Index) -> np.ndarray:
    """Convierte un ``DatetimeIndex`` a un array de enteros en milisegundos UTC."""
    fechas = pd.DatetimeIndex(indice).as_unit("ms")
    return np.asarray(fechas.asi8, dtype="int64")  # type: ignore[attr-defined]


def ms_a_indice(valores_ms: Iterable[int]) -> pd.DatetimeIndex:
    """Construye el ``DatetimeIndex`` UTC estándar del proyecto a partir de milisegundos."""
    if isinstance(valores_ms, np.ndarray):
        valores = valores_ms.astype("int64", copy=False)
    else:
        valores = np.fromiter(valores_ms, dtype="int64")
    return pd.DatetimeIndex(pd.to_datetime(valores, unit="ms", utc=True), name="fecha")
