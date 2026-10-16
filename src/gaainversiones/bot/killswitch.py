"""Kill switch: órdenes manuales para detener el bot.

Dos niveles:

- ``parar``: el bot se detiene y la posición (si la hay) conserva su stop-loss en el
  exchange. Es lo prudente casi siempre.
- ``liquidar``: cancela el stop, vende toda la posición a mercado y se detiene.

Se pueden dar con ``gaa parar [--liquidar]``, creando el fichero ``STOP`` en la raíz del
proyecto (contenido ``parar`` o ``liquidar``) o por Telegram (/parar, /liquidar). Tras
una parada manual el bot no vuelve a operar hasta ``gaa reanudar``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

AccionManual = Literal["parar", "liquidar", "estado"]


@dataclass(frozen=True)
class OrdenManual:
    accion: AccionManual
    origen: str


class FuenteOrdenes(Protocol):
    """Origen de órdenes manuales que el bot revisa en cada pasada."""

    def revisar(self) -> list[OrdenManual]: ...


class FicheroParada:
    """Si existe el fichero, el bot se detiene. El fichero se consume al leerlo."""

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta

    def revisar(self) -> list[OrdenManual]:
        if not self.ruta.exists():
            return []
        try:
            contenido = self.ruta.read_text(encoding="utf-8").strip().lower()
        except OSError:
            contenido = ""
        self.ruta.unlink(missing_ok=True)
        accion: AccionManual = "liquidar" if contenido.startswith("liquidar") else "parar"
        return [OrdenManual(accion, f"fichero {self.ruta.name}")]


def escribir_parada(ruta: Path, accion: Literal["parar", "liquidar"]) -> None:
    """Deja la orden para que la recoja el bot en marcha (en menos de ``intervalo_s``)."""
    ruta.write_text(accion + "\n", encoding="utf-8")
