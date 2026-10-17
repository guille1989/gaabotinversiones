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


def test_cache_del_panel_solo_lee_las_ultimas_velas(proyecto: Config) -> None:
    antes = proyecto.ruta_db_velas.read_bytes()
    velas = panel.leer_velas_cache(proyecto, limite=50)
    assert len(velas) == 50 and velas.index.is_monotonic_increasing
    assert proyecto.ruta_db_velas.read_bytes() == antes
    with pytest.raises(ValueError):
        panel.leer_velas_cache(proyecto, limite=0)


def test_cache_ausente_no_se_crea(tmp_path: Path) -> None:
    cfg = Config(raiz=tmp_path)
    assert panel.leer_velas_cache(cfg).empty
    assert not cfg.ruta_db_velas.exists()


def test_latido_futuro_no_confirma_que_este_en_marcha(proyecto: Config) -> None:
    ahora = int(FECHA.value // 1_000_000)
    with AlmacenEstado(
        proyecto.ruta_db_estado("paper"), modo="paper", exchange="kraken", simbolo="BTC/EUR"
    ) as almacen:
        almacen.guardar_meta("latido", ahora + 10_000)
    assert not panel.leer_estado(proyecto, "paper", ahora).en_marcha


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
    assert app.title[0].value == "Tu robot, de un vistazo"
    metricas = {m.label: m.value for m in app.metric}
    assert metricas["Dinero de la cuenta"] == "1.004,80 €"
    assert metricas["Resultado de la compra abierta"] == "+4,80 €"
    textos = " ".join(c.value for c in app.caption)
    assert "solo lectura" in textos and "dinero simulado" in textos
    assert "No hay conexión reciente" in " ".join(m.value for m in app.markdown)
    assert len(app.get("plotly_chart")) == 0  # solo hay un punto de capital; Mercado no se ejecutó
    app.radio[0].set_value("Precio y estrategia").run()
    assert not app.exception
    assert "caché local" in " ".join(c.value for c in app.caption)
    assert len(app.get("plotly_chart")) == 1
    app.radio[0].set_value("Historial").run()
    assert not app.exception and app.download_button
    app.radio[0].set_value("Simulaciones").run()
    assert not app.exception


@pytest.mark.parametrize("estado", ["activo", "pausado", "detenido"])
def test_resumen_distingue_espera_de_parada(
    proyecto: Config, monkeypatch: pytest.MonkeyPatch, estado: str
) -> None:
    monkeypatch.setenv("GAA_PANEL_CONFIG", str(proyecto.raiz / "config/config.yaml"))
    monkeypatch.setenv("GAA_PANEL_MODO", "paper")
    monkeypatch.setenv("GAA_PANEL_SIN_RED", "1")
    with AlmacenEstado(
        proyecto.ruta_db_estado("paper"), modo="paper", exchange="kraken", simbolo="BTC/EUR"
    ) as almacen:
        cuenta = EstadoCuenta.nueva(1000)
        cuenta.estado = estado  # type: ignore[assignment]
        cuenta.motivo_estado = "Límite de pérdida alcanzado" if estado != "activo" else ""
        almacen.guardar_cuenta(cuenta)
        almacen.guardar_meta("latido", int(pd.Timestamp.now(tz="UTC").value // 1_000_000))
    app = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not app.exception
    textos = " ".join(m.value for m in app.markdown)
    assert "No hay ninguna compra abierta" in " ".join(s.value for s in app.subheader)
    if estado == "activo":
        assert "Tu robot está funcionando" in textos
        assert "esperando una oportunidad" in textos
    else:
        assert "no puede abrir compras" in textos
        assert "Límite de pérdida alcanzado" in textos
        assert "esperando una oportunidad" not in textos
