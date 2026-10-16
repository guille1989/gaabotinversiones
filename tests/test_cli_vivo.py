"""Comandos `gaa estado`, `reanudar`, `reiniciar-paper` y `exportar`."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from gaainversiones.cli import main
from gaainversiones.config import cargar_config
from gaainversiones.cuenta import Ejecucion, EstadoCuenta
from gaainversiones.estado.almacen import AlmacenEstado

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture
def proyecto(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    ruta = tmp_path / "config" / "config.yaml"
    ruta.write_text((RAIZ / "config" / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return ruta


def _crear_estado(proyecto: Path, estado: str = "detenido") -> None:
    cfg = cargar_config(proyecto, entorno={})
    with AlmacenEstado(
        cfg.ruta_db_estado("paper"), modo="paper", exchange="kraken", simbolo="BTC/EUR"
    ) as a:
        cuenta = EstadoCuenta.nueva(1000)
        cuenta.registrar_cierre_vela(pd.Timestamp("2026-01-02", tz="UTC"), 100)
        cuenta.ultimo_equity, cuenta.equity_maximo = 840.0, 1000.0
        cuenta.estado, cuenta.motivo_estado = estado, "caída de 16,0 % desde el máximo"  # type: ignore[assignment]
        a.guardar_cuenta(cuenta)
        a.guardar_meta(
            "ultima_vela", int(pd.Timestamp("2026-01-01 20:00", tz="UTC").value // 1_000_000)
        )
        fecha = pd.Timestamp("2026-01-01 12:00", tz="UTC")
        a.registrar_ejecucion(
            Ejecucion(fecha, "compra", 50_000.0, 0.01, 2.0, "señal de compra", 49_990.0, "p-1")
        )
        a.registrar_evento("Bot detenido: caída", nivel="aviso")


def test_estado_sin_datos(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(proyecto), "estado"]) == 1
    assert "gaa paper" in capsys.readouterr().out


def test_estado(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _crear_estado(proyecto)
    assert main(["--config", str(proyecto), "estado"]) == 0
    salida = capsys.readouterr().out
    assert "Estado: detenido (caída de 16,0 % desde el máximo)" in salida
    assert "la que cerró el 2026-01-02 00:00 UTC" in salida
    assert "840,00 EUR" in salida and "caída actual -16,0 %" in salida
    assert "Bot detenido: caída" in salida


def test_reanudar(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _crear_estado(proyecto)
    assert main(["--config", str(proyecto), "reanudar", "--si"]) == 0
    cfg = cargar_config(proyecto, entorno={})
    with AlmacenEstado(
        cfg.ruta_db_estado("paper"), modo="paper", exchange="kraken", simbolo="BTC/EUR"
    ) as a:
        cuenta = a.cargar_cuenta()
        assert cuenta is not None and cuenta.estado == "activo"
        assert cuenta.equity_maximo == 840.0  # la caída se mide desde ahora
        assert "Reanudado a mano" in a.eventos()["texto"].iloc[-1]
    assert main(["--config", str(proyecto), "reanudar", "--si"]) == 0
    assert "ya está activo" in capsys.readouterr().out


def test_reanudar_pide_confirmacion(proyecto: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _crear_estado(proyecto)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert main(["--config", str(proyecto), "reanudar"]) == 1


def test_exportar(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _crear_estado(proyecto)
    salida = proyecto.parent.parent / "fiscal.csv"
    assert (
        main(["--config", str(proyecto), "exportar", "--salida", str(salida), "--formato", "es"])
        == 0
    )
    assert "1 ejecuciones exportadas" in capsys.readouterr().out
    texto = salida.read_text(encoding="utf-8-sig")
    assert texto.splitlines()[0].startswith("fecha_utc;exchange;par;tipo")
    assert "2026-01-01 12:00:00;kraken;BTC/EUR;compra;50000,0" in texto


def test_reiniciar_paper(proyecto: Path) -> None:
    _crear_estado(proyecto)
    cfg = cargar_config(proyecto, entorno={})
    assert cfg.ruta_db_estado("paper").exists()
    assert main(["--config", str(proyecto), "reiniciar-paper", "--si"]) == 0
    assert not cfg.ruta_db_estado("paper").exists()


def test_parar_con_el_bot_en_marcha_deja_la_orden(
    proyecto: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from gaainversiones.tiempo import ahora_ms

    _crear_estado(proyecto, estado="activo")
    cfg = cargar_config(proyecto, entorno={})
    with AlmacenEstado(
        cfg.ruta_db_estado("paper"), modo="paper", exchange="kraken", simbolo="BTC/EUR"
    ) as a:
        a.guardar_meta("latido", ahora_ms())
    assert main(["--config", str(proyecto), "parar", "--liquidar", "--si"]) == 0
    assert cfg.ruta_stop.read_text(encoding="utf-8").strip() == "liquidar"
    assert "Orden 'liquidar'" in capsys.readouterr().out


def test_parar_sin_estado(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(proyecto), "parar"]) == 0
    assert "nada que parar" in capsys.readouterr().out


def test_telegram_sin_configurar(proyecto: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(proyecto), "telegram-prueba"]) == 1
    assert main(["--config", str(proyecto), "telegram-chatid"]) == 1
    assert "GAA_TELEGRAM_TOKEN" in capsys.readouterr().err
