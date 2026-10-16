"""Informes HTML y comandos `gaa backtest`, `gaa optimizar` y `gaa walkforward`."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from gaainversiones.backtest.informe import (
    formato_eur,
    formato_numero,
    formato_pct,
    informe_backtest,
    tabla_texto,
)
from gaainversiones.backtest.metricas import metricas_resultado
from gaainversiones.backtest.motor import ejecutar_backtest
from gaainversiones.cli import main
from gaainversiones.config import cargar_config
from gaainversiones.datos import CacheVelas
from gaainversiones.estrategias import crear_estrategia
from tests.utiles import HORA_MS, gestor, velas_aleatorias

RAIZ = Path(__file__).resolve().parents[1]
INICIO_2023 = int(pd.Timestamp("2023-01-01", tz="UTC").value // 1_000_000)


def test_formatos() -> None:
    assert formato_eur(1234567.891) == "1.234.567,89 €"
    assert formato_pct(0.1234) == "12,3 %"
    assert formato_pct(-0.5, 0) == "-50 %"
    assert formato_numero(float("nan")) == "—"
    assert formato_numero(float("inf")) == "∞"


@pytest.fixture
def proyecto(tmp_path: Path) -> Path:
    """Proyecto temporal con dos años de velas de 4h en la caché y una rejilla pequeña."""
    contenido = yaml.safe_load((RAIZ / "config" / "config.yaml").read_text(encoding="utf-8"))
    contenido["estrategia"]["parametros"].update(media_rapida=5, media_lenta=20, rsi_periodo=5)
    contenido["backtest"].update(
        inicio_oos="2024-01-01",
        operaciones_minimas=3,
        rejilla={"media_rapida": [5, 10], "media_lenta": [20, 40]},
        walkforward={"entrenamiento_meses": 6, "prueba_meses": 6},
    )
    (tmp_path / "config").mkdir()
    ruta = tmp_path / "config" / "config.yaml"
    ruta.write_text(yaml.safe_dump(contenido, allow_unicode=True), encoding="utf-8")
    cfg = cargar_config(ruta, entorno={})
    velas = velas_aleatorias(4380, tf_ms=4 * HORA_MS, semilla=4, inicio=INICIO_2023)
    CacheVelas(cfg.ruta_db_velas).guardar("bitstamp", "BTC/EUR", "4h", velas)
    return ruta


def test_informe_backtest(proyecto: Path) -> None:
    cfg = cargar_config(proyecto, entorno={})
    velas = velas_aleatorias(4380, tf_ms=4 * HORA_MS, semilla=4, inicio=INICIO_2023)
    estrategia = crear_estrategia("cruce_medias_rsi", {"media_rapida": 5, "media_lenta": 20})
    resultado = ejecutar_backtest(velas, estrategia, gestor(atr_periodo=14), "4h", 1000.0)
    ruta = informe_backtest(
        resultado, cfg, cfg.dir_informes / "prueba.html", pd.Timestamp("2024-01-01", tz="UTC")
    )
    html = ruta.read_text(encoding="utf-8")
    for texto in (
        "Curva de capital",
        "Caídas desde el máximo",
        "Precio y operaciones",
        "Estrategia OOS",
        "Estrategia didáctica",
        "apertura de la vela siguiente",
        f"Operaciones ({len(resultado.operaciones)})",
    ):
        assert texto in html
    assert (ruta.parent / "plotly.min.js").stat().st_size > 1_000_000
    assert ">nan<" not in html.lower()  # ninguna celda con NaN sin formatear

    total, referencia = metricas_resultado(resultado)
    texto = tabla_texto({"Estrategia": total, "Comprar y mantener": referencia})
    assert "Comisiones pagadas" in texto and formato_eur(total.comisiones) in texto


def test_cli_backtest(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(proyecto), "backtest", "--param", "media_lenta=40"]) == 0
    salida = capsys.readouterr().out
    assert "media_lenta=40" in salida and "Estrategia OOS" in salida
    informes = list((proyecto.parent.parent / "informes").glob("backtest_*.html"))
    assert len(informes) == 1
    assert informes[0].with_suffix(".csv").exists()


def test_cli_backtest_parametro_mal_escrito(proyecto: Path) -> None:
    assert main(["--config", str(proyecto), "backtest", "--param", "media_lenta"]) == 1
    assert main(["--config", str(proyecto), "backtest", "--param", "media_rapida=80"]) == 1


def test_cli_optimizar(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(proyecto), "optimizar"]) == 0
    salida = capsys.readouterr().out
    assert "4 combinaciones válidas de 4" in salida
    assert "sharpe IS" in salida and "CAGR OOS" in salida


def test_cli_walkforward(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(proyecto), "walkforward"]) == 0
    salida = capsys.readouterr().out
    assert "3 ventanas" in salida and "Walk-forward" in salida
    informes = list((proyecto.parent.parent / "informes").glob("walkforward_*.html"))
    assert len(informes) == 1
    assert "Ventanas" in informes[0].read_text(encoding="utf-8")


def test_cli_backtest_sin_datos(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "config").mkdir()
    ruta = tmp_path / "config" / "config.yaml"
    ruta.write_text((RAIZ / "config" / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["--config", str(ruta), "backtest"]) == 1
    assert "gaa datos descargar" in capsys.readouterr().err
