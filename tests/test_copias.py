"""Copias de seguridad de las bases de datos."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gaainversiones.copias import copiar_bases


def test_copia_en_caliente_y_rotacion(tmp_path: Path) -> None:
    datos = tmp_path / "data"
    datos.mkdir()
    con = sqlite3.connect(datos / "estado_paper.sqlite")
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()  # la conexion sigue abierta: copia "en caliente"
    destino = tmp_path / "copias"
    for i in range(3):
        (destino / f"2020010{i}-000000").mkdir(parents=True)
    carpeta = copiar_bases(datos, destino, conservar=2)
    con.close()
    copia = sqlite3.connect(carpeta / "estado_paper.sqlite")
    assert copia.execute("SELECT x FROM t").fetchone() == (42,)
    copia.close()
    assert sorted(p.name for p in destino.iterdir()) == ["20200102-000000", carpeta.name]


def test_sin_bases(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No hay bases"):
        copiar_bases(tmp_path, tmp_path / "copias")
