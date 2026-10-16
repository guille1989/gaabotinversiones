"""Copias de seguridad de las bases de datos (estado del bot, registro fiscal y velas).

Usa la API de copia en caliente de SQLite: la copia es coherente aunque el bot esté
escribiendo en ese momento (copiar el fichero a mano podría dejarla corrupta).
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def copiar_bases(origen: Path, destino: Path, *, conservar: int = 14) -> Path:
    """Copia cada ``*.sqlite`` de ``origen`` a una carpeta con fecha dentro de ``destino``.

    Conserva las ``conservar`` copias más recientes y borra las anteriores.

    Returns:
        La carpeta de la copia creada.
    """
    bases = sorted(origen.glob("*.sqlite"))
    if not bases:
        raise ValueError(f"No hay bases de datos que copiar en {origen}")
    carpeta = destino / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    carpeta.mkdir(parents=True, exist_ok=False)
    for base in bases:
        fuente = sqlite3.connect(base)
        copia = sqlite3.connect(carpeta / base.name)
        try:
            fuente.backup(copia)
        finally:
            copia.close()
            fuente.close()
    anteriores = sorted(p for p in destino.iterdir() if p.is_dir())
    for vieja in anteriores[: max(0, len(anteriores) - conservar)]:
        shutil.rmtree(vieja)
    return carpeta
