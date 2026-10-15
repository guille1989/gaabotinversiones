"""Operaciones de alto nivel sobre la caché: actualizar, importar, inspeccionar y cargar."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

from gaainversiones.datos.cache import CacheVelas
from gaainversiones.datos.calidad import Hueco, detectar_huecos, rellenar_huecos, validar_velas
from gaainversiones.datos.fuentes import FuenteVelas, leer_csv_kraken
from gaainversiones.tiempo import ms_a_fecha, timeframe_a_ms

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResumenSerie:
    """Estado de una serie de la caché tras actualizarla o inspeccionarla."""

    exchange: str
    simbolo: str
    temporalidad: str
    velas: int = 0
    nuevas: int = 0
    primera: pd.Timestamp | None = None
    ultima: pd.Timestamp | None = None
    huecos: list[Hueco] = field(default_factory=list)
    problemas: list[str] = field(default_factory=list)

    @property
    def velas_ausentes(self) -> int:
        return sum(h.velas for h in self.huecos)

    def texto(self, max_huecos: int = 10) -> str:
        """Resumen legible para la consola."""
        cabecera = f"{self.exchange} {self.simbolo} {self.temporalidad}"
        if not self.velas:
            return f"{cabecera}: sin velas en la caché"
        lineas = [
            f"{cabecera}: {self.velas} velas ({self.nuevas} nuevas)",
            f"  desde {self.primera} hasta {self.ultima}",
            f"  huecos: {len(self.huecos)} ({self.velas_ausentes} velas ausentes)",
        ]
        mayores = sorted(self.huecos, key=lambda h: h.velas, reverse=True)[:max_huecos]
        lineas += [f"    {h.desde} -> {h.hasta}: {h.velas} velas" for h in mayores]
        if len(self.huecos) > max_huecos:
            lineas.append(f"    ... y {len(self.huecos) - max_huecos} huecos menores")
        lineas.append(
            "  problemas: " + ("; ".join(self.problemas) if self.problemas else "ninguno")
        )
        return "\n".join(lineas)


def inspeccionar(cache: CacheVelas, exchange: str, simbolo: str, temporalidad: str) -> ResumenSerie:
    """Analiza una serie guardada: extensión, huecos y problemas de calidad."""
    velas = cache.leer(exchange, simbolo, temporalidad)
    if velas.empty:
        return ResumenSerie(exchange, simbolo, temporalidad)
    return ResumenSerie(
        exchange=exchange,
        simbolo=simbolo,
        temporalidad=temporalidad,
        velas=len(velas),
        primera=velas.index[0],
        ultima=velas.index[-1],
        huecos=detectar_huecos(velas, temporalidad),
        problemas=validar_velas(velas, temporalidad),
    )


def actualizar_cache(
    cache: CacheVelas, fuente: FuenteVelas, simbolo: str, temporalidad: str, desde_ms: int
) -> ResumenSerie:
    """Completa la caché con lo que falte desde ``desde_ms`` hasta la última vela cerrada.

    Solo descarga los tramos que faltan: el anterior a la primera vela guardada (si se
    pide una fecha más antigua) y el posterior a la última.
    """
    tf_ms = timeframe_a_ms(temporalidad)
    rango = cache.rango(fuente.nombre, simbolo, temporalidad)
    tramos: list[tuple[int, int | None]] = []
    if rango is None:
        tramos.append((desde_ms, None))
    else:
        if desde_ms < rango.primero:
            tramos.append((desde_ms, rango.primero))
        tramos.append((rango.ultimo + tf_ms, None))

    nuevas = 0
    for inicio, fin in tramos:
        log.info(
            "Descargando %s %s %s desde %s%s",
            fuente.nombre,
            simbolo,
            temporalidad,
            ms_a_fecha(inicio),
            f" hasta {ms_a_fecha(fin)}" if fin is not None else "",
        )
        velas = fuente.descargar(simbolo, temporalidad, inicio, fin)
        nuevas += cache.guardar(fuente.nombre, simbolo, temporalidad, velas)
    return replace(inspeccionar(cache, fuente.nombre, simbolo, temporalidad), nuevas=nuevas)


def importar_csv_kraken(
    cache: CacheVelas, ruta: Path, simbolo: str, temporalidad: str | None = None
) -> ResumenSerie:
    """Importa un CSV del histórico oficial de Kraken a la serie ``kraken`` de la caché."""
    velas, temporalidad = leer_csv_kraken(ruta, temporalidad)
    nuevas = cache.guardar("kraken", simbolo, temporalidad, velas)
    log.info("Importadas %d velas de %s (%d nuevas)", len(velas), ruta.name, nuevas)
    return replace(inspeccionar(cache, "kraken", simbolo, temporalidad), nuevas=nuevas)


def cargar_velas(
    cache: CacheVelas,
    exchange: str,
    simbolo: str,
    temporalidad: str,
    *,
    desde_ms: int | None = None,
    hasta_ms: int | None = None,
    rellenar: bool = True,
) -> pd.DataFrame:
    """Lee velas de la caché listas para usar (con huecos rellenados si se pide).

    Raises:
        ValueError: si la serie no existe o tiene problemas de calidad.
    """
    velas = cache.leer(exchange, simbolo, temporalidad, desde_ms, hasta_ms)
    if velas.empty:
        raise ValueError(
            f"No hay velas de {exchange} {simbolo} {temporalidad} en la caché: "
            "ejecuta antes `gaa datos descargar`"
        )
    problemas = validar_velas(velas, temporalidad)
    if problemas:
        raise ValueError(f"Datos con problemas de calidad: {'; '.join(problemas)}")
    huecos = detectar_huecos(velas, temporalidad)
    if huecos:
        log.info(
            "%s %s %s: %d huecos (%d velas ausentes)%s",
            exchange,
            simbolo,
            temporalidad,
            len(huecos),
            sum(h.velas for h in huecos),
            ", rellenados con velas sintéticas" if rellenar else "",
        )
    if rellenar:
        return rellenar_huecos(velas, temporalidad)
    resultado = velas.copy()
    resultado["sintetica"] = False
    return resultado
