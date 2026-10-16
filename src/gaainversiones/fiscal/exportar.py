"""Exportación del registro de ejecuciones a CSV.

En España cada venta y cada intercambio cripto-cripto es una ganancia o pérdida
patrimonial, así que se exporta cada ejecución con fecha UTC, precio, cantidad, importe y
comisión. El fichero sirve para calcular el resultado por FIFO (el criterio que aplica
Hacienda) con la herramienta o asesor que prefieras.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd

from gaainversiones.estado.almacen import AlmacenEstado

COLUMNAS_FISCALES = [
    "fecha_utc",
    "exchange",
    "par",
    "tipo",
    "precio",
    "cantidad",
    "importe",
    "comision",
    "moneda_comision",
    "motivo",
    "modo",
    "id_orden",
]

Formato = Literal["estandar", "es"]


def tabla_fiscal(
    almacen: AlmacenEstado, desde: pd.Timestamp | None = None, hasta: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Ejecuciones con las columnas del registro fiscal, en orden cronológico."""
    return formato_fiscal(almacen.ejecuciones(desde, hasta))


def formato_fiscal(ejecuciones: pd.DataFrame) -> pd.DataFrame:
    """Convierte la tabla ``ejecuciones`` del almacén a las columnas del registro fiscal."""
    if ejecuciones.empty:
        return pd.DataFrame(columns=COLUMNAS_FISCALES)
    return pd.DataFrame(
        {
            "fecha_utc": ejecuciones["fecha"].dt.strftime("%Y-%m-%d %H:%M:%S"),
            "exchange": ejecuciones["exchange"],
            "par": ejecuciones["simbolo"],
            "tipo": ejecuciones["lado"],
            "precio": ejecuciones["precio"],
            "cantidad": ejecuciones["cantidad"],
            "importe": ejecuciones["importe"],
            "comision": ejecuciones["comision"],
            "moneda_comision": ejecuciones["moneda_comision"],
            "motivo": ejecuciones["motivo"],
            "modo": ejecuciones["modo"],
            "id_orden": ejecuciones["id_orden"],
        }
    )


def exportar_ejecuciones(
    almacen: AlmacenEstado,
    ruta: Path,
    *,
    desde: pd.Timestamp | None = None,
    hasta: pd.Timestamp | None = None,
    formato: Formato = "estandar",
) -> int:
    """Escribe el CSV y devuelve el número de filas.

    ``formato="es"`` usa ``;`` como separador y coma decimal (lo que espera Excel en
    español); ``"estandar"`` usa ``,`` y punto decimal.
    """
    tabla = tabla_fiscal(almacen, desde, hasta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if formato == "es":
        tabla.to_csv(ruta, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        tabla.to_csv(ruta, index=False, encoding="utf-8")
    return len(tabla)
