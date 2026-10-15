"""Caché local de velas en SQLite.

Cada vela se identifica por (exchange, símbolo, temporalidad, ts). La clave primaria
impide duplicados: volver a guardar una vela existente la actualiza en lugar de repetirla.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from gaainversiones.datos.fuentes import COLUMNAS, dataframe_vacio, velas_a_dataframe
from gaainversiones.tiempo import indice_a_ms

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS velas (
    exchange     TEXT    NOT NULL,
    simbolo      TEXT    NOT NULL,
    temporalidad TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    apertura     REAL    NOT NULL,
    maximo       REAL    NOT NULL,
    minimo       REAL    NOT NULL,
    cierre       REAL    NOT NULL,
    volumen      REAL    NOT NULL,
    PRIMARY KEY (exchange, simbolo, temporalidad, ts)
) WITHOUT ROWID;
"""

_INSERTAR = """
INSERT INTO velas (exchange, simbolo, temporalidad, ts, apertura, maximo, minimo, cierre, volumen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (exchange, simbolo, temporalidad, ts) DO UPDATE SET
    apertura = excluded.apertura,
    maximo   = excluded.maximo,
    minimo   = excluded.minimo,
    cierre   = excluded.cierre,
    volumen  = excluded.volumen
"""

_FILTRO_SERIE = "exchange = ? AND simbolo = ? AND temporalidad = ?"


@dataclass(frozen=True)
class RangoSerie:
    """Extensión de una serie guardada (marcas en milisegundos UTC)."""

    primero: int
    ultimo: int
    velas: int


class CacheVelas:
    """Almacén de velas OHLCV en un fichero SQLite."""

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with self._conexion() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_ESQUEMA)

    @contextmanager
    def _conexion(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.ruta)
        try:
            with con:  # transacción: commit al salir, rollback si hay excepción
                yield con
        finally:
            con.close()

    @staticmethod
    def _contar(con: sqlite3.Connection, clave: tuple[str, str, str]) -> int:
        fila = con.execute(f"SELECT COUNT(*) FROM velas WHERE {_FILTRO_SERIE}", clave).fetchone()
        return int(fila[0])

    def guardar(self, exchange: str, simbolo: str, temporalidad: str, velas: pd.DataFrame) -> int:
        """Inserta o actualiza velas. Devuelve cuántas no estaban ya guardadas."""
        if velas.empty:
            return 0
        valores = velas[list(COLUMNAS)].to_numpy(dtype=float)
        if np.isnan(valores).any():
            raise ValueError("No se guardan velas con valores vacíos (NaN)")
        clave = (exchange, simbolo, temporalidad)
        filas = [
            (*clave, int(ts), *map(float, fila))
            for ts, fila in zip(indice_a_ms(velas.index), valores, strict=True)
        ]
        with self._conexion() as con:
            antes = self._contar(con, clave)
            con.executemany(_INSERTAR, filas)
            return self._contar(con, clave) - antes

    def leer(
        self,
        exchange: str,
        simbolo: str,
        temporalidad: str,
        desde_ms: int | None = None,
        hasta_ms: int | None = None,
    ) -> pd.DataFrame:
        """Velas guardadas con fecha en ``[desde_ms, hasta_ms)``, ordenadas."""
        consulta = f"SELECT ts, {', '.join(COLUMNAS)} FROM velas WHERE {_FILTRO_SERIE}"
        parametros: list[object] = [exchange, simbolo, temporalidad]
        if desde_ms is not None:
            consulta += " AND ts >= ?"
            parametros.append(desde_ms)
        if hasta_ms is not None:
            consulta += " AND ts < ?"
            parametros.append(hasta_ms)
        with self._conexion() as con:
            filas = con.execute(consulta + " ORDER BY ts", parametros).fetchall()
        if not filas:
            return dataframe_vacio()
        return velas_a_dataframe(np.array(filas, dtype=float))

    def rango(self, exchange: str, simbolo: str, temporalidad: str) -> RangoSerie | None:
        """Primera y última vela guardadas de una serie, o None si no hay ninguna."""
        with self._conexion() as con:
            fila = con.execute(
                f"SELECT MIN(ts), MAX(ts), COUNT(*) FROM velas WHERE {_FILTRO_SERIE}",
                (exchange, simbolo, temporalidad),
            ).fetchone()
        if not fila or fila[2] == 0:
            return None
        return RangoSerie(primero=int(fila[0]), ultimo=int(fila[1]), velas=int(fila[2]))

    def series(self) -> list[tuple[str, str, str, RangoSerie]]:
        """Todas las series guardadas con su extensión."""
        with self._conexion() as con:
            filas = con.execute(
                "SELECT exchange, simbolo, temporalidad, MIN(ts), MAX(ts), COUNT(*) FROM velas "
                "GROUP BY exchange, simbolo, temporalidad ORDER BY exchange, simbolo, temporalidad"
            ).fetchall()
        return [(f[0], f[1], f[2], RangoSerie(int(f[3]), int(f[4]), int(f[5]))) for f in filas]
