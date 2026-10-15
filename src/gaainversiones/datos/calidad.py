"""Control de calidad de series de velas: huecos, alineación y coherencia OHLC."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from gaainversiones.datos.fuentes import COLUMNAS
from gaainversiones.tiempo import indice_a_ms, ms_a_fecha, ms_a_indice, timeframe_a_ms


@dataclass(frozen=True)
class Hueco:
    """Tramo de velas ausentes entre dos velas presentes."""

    desde: pd.Timestamp  # primera vela que falta
    hasta: pd.Timestamp  # última vela que falta
    velas: int


def detectar_huecos(velas: pd.DataFrame, temporalidad: str) -> list[Hueco]:
    """Huecos de la serie: saltos entre velas consecutivas mayores que la temporalidad."""
    if len(velas) < 2:
        return []
    tf_ms = timeframe_a_ms(temporalidad)
    ts = indice_a_ms(velas.index)
    saltos = np.diff(ts)
    return [
        Hueco(
            desde=ms_a_fecha(int(ts[i]) + tf_ms),
            hasta=ms_a_fecha(int(ts[i + 1]) - tf_ms),
            velas=int(saltos[i] // tf_ms) - 1,
        )
        for i in np.flatnonzero(saltos > tf_ms).tolist()
    ]


def rellenar_huecos(velas: pd.DataFrame, temporalidad: str) -> pd.DataFrame:
    """Rellena los huecos con velas sintéticas planas.

    Cada vela añadida tiene apertura = máximo = mínimo = cierre = último cierre conocido
    y volumen 0: representa "no hubo operaciones". La columna booleana ``sintetica``
    marca cuáles son, para que el backtest pueda ignorarlas o señalarlas.

    Raises:
        ValueError: si hay velas desalineadas con la temporalidad.
    """
    resultado = velas.copy()
    resultado["sintetica"] = False
    if len(velas) < 2:
        return resultado
    tf_ms = timeframe_a_ms(temporalidad)
    ts = indice_a_ms(velas.index)
    completo = np.arange(ts[0], ts[-1] + tf_ms, tf_ms, dtype="int64")
    if len(completo) == len(ts):
        return resultado
    if not np.isin(ts, completo).all():
        raise ValueError(f"Hay velas desalineadas con la temporalidad {temporalidad}")

    resultado = resultado.reindex(ms_a_indice(completo))
    sintetica = resultado["cierre"].isna()
    cierre = resultado["cierre"].ffill()
    for columna in ("apertura", "maximo", "minimo"):
        resultado[columna] = resultado[columna].fillna(cierre)
    resultado["cierre"] = cierre
    resultado["volumen"] = resultado["volumen"].fillna(0.0)
    resultado["sintetica"] = sintetica.to_numpy(dtype=bool)
    return resultado


def validar_velas(velas: pd.DataFrame, temporalidad: str) -> list[str]:
    """Lista de problemas encontrados (vacía si la serie es correcta)."""
    problemas: list[str] = []
    if velas.empty:
        return problemas
    if velas.index.has_duplicates:
        problemas.append(f"{int(velas.index.duplicated().sum())} fechas duplicadas")
    if not velas.index.is_monotonic_increasing:
        problemas.append("las velas no están ordenadas por fecha")

    tf_ms = timeframe_a_ms(temporalidad)
    if not temporalidad.endswith("w"):  # las velas semanales no se alinean con epoch
        desalineadas = int((indice_a_ms(velas.index) % tf_ms != 0).sum())
        if desalineadas:
            problemas.append(
                f"{desalineadas} velas no alineadas con la temporalidad {temporalidad}"
            )

    vacias = int(velas[list(COLUMNAS)].isna().any(axis=1).sum())
    if vacias:
        problemas.append(f"{vacias} velas con valores vacíos")

    apertura, maximo, minimo, cierre, volumen = (velas[c] for c in COLUMNAS)
    incoherentes = (maximo < np.maximum(apertura, cierre)) | (minimo > np.minimum(apertura, cierre))
    if int(incoherentes.sum()):
        problemas.append(f"{int(incoherentes.sum())} velas con máximo/mínimo incoherentes")
    no_positivas = (velas[["apertura", "maximo", "minimo", "cierre"]] <= 0).any(axis=1)
    if int(no_positivas.sum()):
        problemas.append(f"{int(no_positivas.sum())} velas con precios <= 0")
    if int((volumen < 0).sum()):
        problemas.append(f"{int((volumen < 0).sum())} velas con volumen negativo")
    return problemas
