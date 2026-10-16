"""Panel web: lectura en solo lectura y prueba de humo de la aplicación (sin red)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from gaainversiones.config import Config, cargar_config
from gaainversiones.cuenta import Ejecucion, EstadoCuenta, Posicion
from gaainversiones.datos import CacheVelas
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.panel import datos as panel
from tests.utiles import CONFIG_BASE, HORA_MS, velas_aleatorias

APP = Path(__file__).resolve().parents[1] / "src" / "gaainversiones" / "panel" / "app.py"
FECHA = pd.Timestamp("2026-01-05 08:00", tz="UTC")


@pytest.fixture
def proyecto(tmp_path: Path) -> Config:
    (tmp_path / "config").mkdir()
    ruta = tmp_path / "config" / "config.yaml"
    ruta.write_text(CONFIG_BASE.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = cargar_config(ruta, entorno={})
    with AlmacenEstado(
        cfg.ruta_db_estado("paper"), modo="paper", exchange="kraken", simbolo="BTC/EUR"
    ) as a:
        cuenta = EstadoCuenta.nueva(1000)
        cuenta.registrar_cierre_vela(FECHA, 100)
        cuenta.posicion = Posicion(0.5, 100.0, 96.0, 106.0, FECHA, 0.2, "s1")
        cuenta.efectivo, cuenta.ultimo_equity, cuenta.equity_maximo = 949.8, 1004.8, 1010.0
        a.guardar_cuenta(cuenta)
        a.guardar_meta("ultima_vela", int(FECHA.value // 1_000_000))
        a.registrar_capital(FECHA, equity=1004.8, efectivo=949.8, precio=110.0, en_mercado=True)
        a.registrar_ejecucion(
            Ejecucion(FECHA, "compra", 100.0, 0.5, 0.2, "señal de compra", 99.9, "c1")
        )
        a.registrar_evento("Bot arrancado")
    inicio = int(pd.Timestamp("2025-01-01", tz="UTC").value // 1_000_000)  # alineado a 4 h
    velas = velas_aleatorias(300, tf_ms=4 * HORA_MS, inicio=inicio)
    CacheVelas(cfg.ruta_db_velas).guardar("bitstamp", "BTC/EUR", "4h", velas)
    return cfg


def test_leer_estado(proyecto: Config) -> None:
    estado = panel.leer_estado(proyecto, "paper", int(FECHA.value // 1_000_000))
    assert estado.existe and not estado.en_marcha
    assert estado.capital == pytest.approx(1004.8) and estado.caida == pytest.approx(
        1004.8 / 1010 - 1
    )
    assert estado.posicion is not None and estado.posicion["stop"] == 96.0
    assert estado.latente == pytest.approx(0.5 * 110 - (50 + 0.2))
    assert estado.ultima_vela_cierre == FECHA + pd.Timedelta(hours=4)
    assert not panel.leer_estado(proyecto, "real", 0).existe


def test_lectura_sin_escritura(proyecto: Config) -> None:
    ruta = proyecto.ruta_db_estado("paper")
    assert list(panel.leer_tabla(ruta, "ejecuciones")["lado"]) == ["compra"]
    con = panel.conexion_lectura(ruta)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("DELETE FROM ejecuciones")
    con.close()
    with pytest.raises(ValueError):
        panel.leer_tabla(ruta, "sqlite_master")


def test_niveles_estrategia() -> None:
    velas = velas_aleatorias(200)
    lineas = panel.niveles_estrategia(velas, "tendencia_media", {"periodo": 50, "banda": 0.02})
    assert list(lineas.columns) == [
        "Media 50",
        "Entrada (banda superior)",
        "Salida (banda inferior)",
    ]
    fila = lineas.iloc[-1]
    assert fila.iloc[1] == pytest.approx(fila.iloc[0] * 1.02)
    assert list(panel.niveles_estrategia(velas, "cruce_medias_rsi", {}).columns) == [
        "Media 20",
        "Media 50",
    ]
    assert panel.niveles_estrategia(velas, "reversion_rsi", {}).empty


def test_informes(tmp_path: Path) -> None:
    (tmp_path / "backtest_tendencia_media_20261016-141930.html").write_text(
        '<html><script src="plotly.min.js"></script>hola</html>', encoding="utf-8"
    )
    (tmp_path / "walkforward_cruce_medias_rsi_20261016-150000.html").write_text(
        "x", encoding="utf-8"
    )
    (tmp_path / "otro.html").write_text("x", encoding="utf-8")
    (tmp_path / "plotly.min.js").write_text("var plotly=1;", encoding="utf-8")
    informes = panel.listar_informes(tmp_path)
    assert [(i.tipo, i.estrategia) for i in informes] == [
        ("walkforward", "cruce_medias_rsi"),
        ("backtest", "tendencia_media"),
    ]
    html = panel.html_autonomo(informes[1].ruta)
    assert "<script>var plotly=1;</script>" in html and "src=" not in html


def test_la_app_arranca_y_muestra_el_estado(
    proyecto: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GAA_PANEL_CONFIG", str(proyecto.raiz / "config" / "config.yaml"))
    monkeypatch.setenv("GAA_PANEL_MODO", "paper")
    monkeypatch.setenv("GAA_PANEL_SIN_RED", "1")
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    assert not app.exception
    assert app.title[0].value == "GaaInversiones"
    metricas = {m.label: m.value for m in app.metric}
    assert metricas["Capital"] == "1.004,80 EUR"
    assert metricas["Posición"] == "0.500000 BTC"
    textos = " ".join(c.value for c in app.caption)
    assert "caché local" in textos and "solo lectura" in textos
