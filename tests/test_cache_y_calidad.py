"""Caché SQLite, detección y relleno de huecos, validación y servicio de actualización."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gaainversiones.datos import (
    CacheVelas,
    FuenteCCXT,
    actualizar_cache,
    cargar_velas,
    detectar_huecos,
    importar_csv_kraken,
    rellenar_huecos,
    validar_velas,
)
from gaainversiones.datos.fuentes import velas_a_dataframe
from gaainversiones.tiempo import indice_a_ms
from tests.utiles import HORA_MS, INICIO_MS, ExchangeFalso, generar_filas

CLAVE = ("falso", "BTC/EUR", "1h")


@pytest.fixture
def cache(tmp_path: Path) -> CacheVelas:
    return CacheVelas(tmp_path / "data" / "velas.sqlite")


@pytest.fixture
def velas() -> pd.DataFrame:
    return velas_a_dataframe(generar_filas(100))


# --- Caché ---------------------------------------------------------------------------------


def test_guardar_es_idempotente(cache: CacheVelas, velas: pd.DataFrame) -> None:
    assert cache.guardar(*CLAVE, velas) == 100
    assert cache.guardar(*CLAVE, velas) == 0
    assert cache.guardar(*CLAVE, velas.iloc[50:]) == 0
    rango = cache.rango(*CLAVE)
    assert rango is not None and rango.velas == 100


def test_guardar_solapado_solo_cuenta_nuevas(cache: CacheVelas) -> None:
    filas = generar_filas(150)
    cache.guardar(*CLAVE, velas_a_dataframe(filas[:100]))
    assert cache.guardar(*CLAVE, velas_a_dataframe(filas[80:])) == 50
    leidas = cache.leer(*CLAVE)
    pd.testing.assert_frame_equal(leidas, velas_a_dataframe(filas))


def test_guardar_actualiza_vela_existente(cache: CacheVelas, velas: pd.DataFrame) -> None:
    cache.guardar(*CLAVE, velas)
    corregida = velas.iloc[[10]].copy()
    corregida["cierre"] = 12345.0
    cache.guardar(*CLAVE, corregida)
    assert cache.leer(*CLAVE)["cierre"].iloc[10] == 12345.0


def test_leer_rango_y_series(cache: CacheVelas, velas: pd.DataFrame) -> None:
    cache.guardar(*CLAVE, velas)
    cache.guardar("otro", "ETH/EUR", "4h", velas_a_dataframe(generar_filas(5, tf_ms=4 * HORA_MS)))
    parte = cache.leer(*CLAVE, desde_ms=INICIO_MS + 10 * HORA_MS, hasta_ms=INICIO_MS + 20 * HORA_MS)
    assert len(parte) == 10 and indice_a_ms(parte.index)[0] == INICIO_MS + 10 * HORA_MS
    assert [s[:3] for s in cache.series()] == [CLAVE, ("otro", "ETH/EUR", "4h")]
    assert cache.leer("nada", "BTC/EUR", "1h").empty
    assert cache.rango("nada", "BTC/EUR", "1h") is None


def test_no_guarda_nan(cache: CacheVelas, velas: pd.DataFrame) -> None:
    velas.iloc[3, 0] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        cache.guardar(*CLAVE, velas)


# --- Huecos y validación -------------------------------------------------------------------


def test_detectar_y_rellenar_huecos(velas: pd.DataFrame) -> None:
    con_hueco = velas.drop(velas.index[10:13])
    huecos = detectar_huecos(con_hueco, "1h")
    assert len(huecos) == 1
    assert huecos[0].velas == 3
    assert huecos[0].desde == velas.index[10] and huecos[0].hasta == velas.index[12]

    relleno = rellenar_huecos(con_hueco, "1h")
    assert relleno.index.equals(velas.index)
    assert relleno["sintetica"].sum() == 3
    previo = velas["cierre"].iloc[9]
    for columna in ("apertura", "maximo", "minimo", "cierre"):
        assert (relleno[columna].iloc[10:13] == previo).all()
    assert (relleno["volumen"].iloc[10:13] == 0).all()
    originales = ~relleno["sintetica"]
    pd.testing.assert_frame_equal(relleno.loc[originales, velas.columns], con_hueco)


def test_sin_huecos_no_cambia(velas: pd.DataFrame) -> None:
    assert detectar_huecos(velas, "1h") == []
    relleno = rellenar_huecos(velas, "1h")
    assert not relleno["sintetica"].any() and len(relleno) == len(velas)


def test_rellenar_rechaza_velas_desalineadas(velas: pd.DataFrame) -> None:
    desplazada = velas.copy()
    desplazada.index = desplazada.index.where(
        desplazada.index != desplazada.index[50], desplazada.index[50] + pd.Timedelta(minutes=30)
    )
    with pytest.raises(ValueError, match="desalineadas"):
        rellenar_huecos(desplazada.drop(desplazada.index[20]), "1h")


def test_validar_velas(velas: pd.DataFrame) -> None:
    assert validar_velas(velas, "1h") == []
    mala = velas.copy()
    mala.iloc[1, mala.columns.get_loc("maximo")] = mala["minimo"].iloc[1] - 1
    mala.iloc[2, mala.columns.get_loc("cierre")] = -5
    mala.iloc[3, mala.columns.get_loc("volumen")] = -1
    problemas = " | ".join(validar_velas(mala, "1h"))
    assert "incoherentes" in problemas and "<= 0" in problemas and "volumen negativo" in problemas
    assert "no alineadas" in " ".join(validar_velas(velas, "4h"))


# --- Servicio ------------------------------------------------------------------------------


def test_actualizar_cache_incremental(cache: CacheVelas) -> None:
    filas = generar_filas(1200)
    ahora = {"ms": INICIO_MS + 1000 * HORA_MS}  # velas 0..999 cerradas
    exchange = ExchangeFalso(filas)
    fuente = FuenteCCXT(exchange, reloj=lambda: ahora["ms"], dormir=lambda s: None)

    primera = actualizar_cache(cache, fuente, "BTC/EUR", "1h", INICIO_MS)
    assert (primera.velas, primera.nuevas, primera.huecos) == (1000, 1000, [])

    exchange.llamadas.clear()
    segunda = actualizar_cache(cache, fuente, "BTC/EUR", "1h", INICIO_MS)
    assert segunda.nuevas == 0
    assert exchange.llamadas == []  # no ha cerrado ninguna vela nueva: ni siquiera pregunta

    ahora["ms"] += 150 * HORA_MS
    exchange.llamadas.clear()
    tercera = actualizar_cache(cache, fuente, "BTC/EUR", "1h", INICIO_MS)
    assert (tercera.velas, tercera.nuevas) == (1150, 150)
    assert exchange.llamadas == [INICIO_MS + 1000 * HORA_MS]  # solo pide lo posterior
    pd.testing.assert_frame_equal(
        cache.leer("falso", "BTC/EUR", "1h"), velas_a_dataframe(filas[:1150])
    )


def test_actualizar_cache_completa_hacia_atras(cache: CacheVelas) -> None:
    filas = generar_filas(500)
    cache.guardar("falso", "BTC/EUR", "1h", velas_a_dataframe(filas[200:400]))
    fuente = FuenteCCXT(ExchangeFalso(filas), reloj=lambda: INICIO_MS + 400 * HORA_MS)
    resumen = actualizar_cache(cache, fuente, "BTC/EUR", "1h", INICIO_MS)
    assert (resumen.velas, resumen.nuevas, resumen.huecos) == (400, 200, [])


def test_cargar_velas(cache: CacheVelas, velas: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="gaa datos descargar"):
        cargar_velas(cache, *CLAVE)
    cache.guardar(*CLAVE, velas.drop(velas.index[40:45]))
    rellenas = cargar_velas(cache, *CLAVE)
    assert len(rellenas) == 100 and rellenas["sintetica"].sum() == 5
    sin_rellenar = cargar_velas(cache, *CLAVE, rellenar=False)
    assert len(sin_rellenar) == 95 and not sin_rellenar["sintetica"].any()


def test_importar_csv_kraken(cache: CacheVelas, tmp_path: Path) -> None:
    filas = generar_filas(30, tf_ms=4 * HORA_MS, inicio=INICIO_MS - INICIO_MS % (4 * HORA_MS))
    ruta = tmp_path / "XBTEUR_240.csv"
    ruta.write_text(
        "\n".join(f"{int(f[0] // 1000)},{f[1]},{f[2]},{f[3]},{f[4]},{f[5]},3" for f in filas),
        encoding="utf-8",
    )
    resumen = importar_csv_kraken(cache, ruta, "BTC/EUR")
    assert (resumen.exchange, resumen.temporalidad, resumen.velas, resumen.nuevas) == (
        "kraken",
        "4h",
        30,
        30,
    )
    assert importar_csv_kraken(cache, ruta, "BTC/EUR").nuevas == 0
    assert "30 velas" in resumen.texto()
