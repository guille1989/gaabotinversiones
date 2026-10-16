"""Utilidades de tiempo y comandos de la CLI que no acceden a internet."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from gaainversiones.cli import main
from gaainversiones.datos import CacheVelas
from gaainversiones.datos.fuentes import velas_a_dataframe
from gaainversiones.tiempo import (
    fecha_a_ms,
    indice_a_ms,
    inicio_vela,
    ms_a_fecha,
    ms_a_indice,
    timeframe_a_ms,
    vela_cerrada,
)
from tests.utiles import CONFIG_BASE, HORA_MS, generar_filas


def test_timeframe_a_ms() -> None:
    assert timeframe_a_ms("1m") == 60_000
    assert timeframe_a_ms("4h") == 4 * HORA_MS
    assert timeframe_a_ms("1d") == 24 * HORA_MS
    for malo in ("", "4", "h", "0h", "1M", "4 h"):
        with pytest.raises(ValueError):
            timeframe_a_ms(malo)


def test_fechas_en_utc() -> None:
    assert fecha_a_ms("1970-01-02") == 24 * HORA_MS
    assert fecha_a_ms("2024-01-01T02:00:00+02:00") == fecha_a_ms("2024-01-01T00:00:00")
    assert ms_a_fecha(0) == pd.Timestamp("1970-01-01", tz="UTC")
    with pytest.raises(ValueError):
        fecha_a_ms("ayer")


def test_velas_cerradas_e_inicio() -> None:
    assert inicio_vela(5 * HORA_MS + 123, HORA_MS) == 5 * HORA_MS
    assert vela_cerrada(0, HORA_MS, HORA_MS)
    assert not vela_cerrada(0, HORA_MS, HORA_MS - 1)


def test_indice_ida_y_vuelta() -> None:
    valores = [0, HORA_MS, 1_700_000_000_000]
    indice = ms_a_indice(valores)
    assert str(indice.tz) == "UTC" and indice.name == "fecha"
    assert list(indice_a_ms(indice)) == valores


# --- CLI -----------------------------------------------------------------------------------


@pytest.fixture
def proyecto(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    ruta = tmp_path / "config" / "config.yaml"
    ruta.write_text(CONFIG_BASE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text("GAA_API_KEY=clave-que-no-debe-salir\n", encoding="utf-8")
    return ruta


def test_cli_config_sin_secretos(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(proyecto), "config"]) == 0
    salida = capsys.readouterr().out
    datos = json.loads(salida)
    assert datos["mercado"]["simbolo"] == "BTC/EUR"
    assert datos["secretos"]["GAA_API_KEY"] == "definida"
    assert "clave-que-no-debe-salir" not in salida


def test_cli_config_no_valida(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "config").mkdir()
    ruta = tmp_path / "config" / "config.yaml"
    ruta.write_text("riesgo:\n  caida_max: 3\n", encoding="utf-8")
    assert main(["--config", str(ruta), "config"]) == 2
    assert "caida_max" in capsys.readouterr().err


def test_cli_verificar(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(proyecto), "datos", "verificar"]) == 1
    assert "vacía" in capsys.readouterr().out

    velas = velas_a_dataframe(generar_filas(50))
    CacheVelas(proyecto.parent.parent / "data" / "velas.sqlite").guardar(
        "bitstamp", "BTC/EUR", "1h", velas.drop(velas.index[5:7])
    )
    assert main(["--config", str(proyecto), "datos", "verificar"]) == 0
    salida = capsys.readouterr().out
    assert "48 velas" in salida and "huecos: 1 (2 velas ausentes)" in salida
