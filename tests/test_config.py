"""Carga y validación de la configuración y de los secretos."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gaainversiones.config import ErrorConfig, cargar_config

RAIZ = Path(__file__).resolve().parents[1]
CONFIG_PROYECTO = RAIZ / "config" / "config.yaml"
SECRETO = "clave-super-secreta-123"


def _proyecto(tmp_path: Path, cambios: dict[str, object] | None = None, env: str = "") -> Path:
    """Crea un proyecto temporal con el config.yaml del repositorio modificado."""
    contenido = yaml.safe_load(CONFIG_PROYECTO.read_text(encoding="utf-8"))
    for ruta, valor in (cambios or {}).items():
        nodo = contenido
        *padres, hoja = ruta.split(".")
        for padre in padres:
            nodo = nodo.setdefault(padre, {})
        nodo[hoja] = valor
    (tmp_path / "config").mkdir()
    ruta_config = tmp_path / "config" / "config.yaml"
    ruta_config.write_text(yaml.safe_dump(contenido, allow_unicode=True), encoding="utf-8")
    if env:
        (tmp_path / ".env").write_text(env, encoding="utf-8")
    return ruta_config


def test_config_del_proyecto_es_valida() -> None:
    cfg = cargar_config(CONFIG_PROYECTO, entorno={})
    assert cfg.modo == "paper"
    assert (cfg.mercado.exchange, cfg.mercado.simbolo, cfg.mercado.temporalidad) == (
        "kraken",
        "BTC/EUR",
        "4h",
    )
    assert (cfg.mercado.base, cfg.mercado.cotizada) == ("BTC", "EUR")
    assert cfg.riesgo.riesgo_por_operacion == 0.01
    assert cfg.raiz == RAIZ
    assert cfg.ruta_db_velas == RAIZ / "data" / "velas.sqlite"


@pytest.mark.parametrize(
    ("campo", "valor", "mensaje"),
    [
        ("riesgo.riesgo_por_operacion", 0.5, "riesgo_por_operacion"),
        ("riesgo.perdida_diaria_max", 0.5, "perdida_diaria_max"),
        ("riesgo.exposicion_max", 2.0, "exposicion_max"),
        ("mercado.temporalidad", "4x", "temporalidad"),
        ("mercado.exchange", "noexiste", "exchange desconocido"),
        ("mercado.simbolo", "BTCEUR", "BASE/COTIZADA"),
        ("datos.desde", "ayer", "fecha no válida"),
        ("costes.comision_taker", -0.1, "comision_taker"),
        ("riesgo.riesgo_por_operacon", 0.01, "riesgo_por_operacon"),  # errata: campo desconocido
        ("modo", "demo", "modo"),
    ],
)
def test_valores_no_validos(tmp_path: Path, campo: str, valor: object, mensaje: str) -> None:
    with pytest.raises(ErrorConfig, match=mensaje):
        cargar_config(_proyecto(tmp_path, {campo: valor}), entorno={})


def test_fichero_inexistente(tmp_path: Path) -> None:
    with pytest.raises(ErrorConfig, match="No existe"):
        cargar_config(tmp_path / "no.yaml", entorno={})


def test_secretos_en_yaml_rechazados(tmp_path: Path) -> None:
    with pytest.raises(ErrorConfig, match="van en .env"):
        cargar_config(_proyecto(tmp_path, {"secretos.api_key": "x"}), entorno={})


def test_modo_real_exige_doble_cerrojo(tmp_path: Path) -> None:
    ruta = _proyecto(tmp_path, {"modo": "real"})
    with pytest.raises(ErrorConfig, match="GAA_PERMITIR_REAL"):
        cargar_config(ruta, entorno={"GAA_API_KEY": "k" * 10, "GAA_API_SECRET": "s" * 10})
    with pytest.raises(ErrorConfig, match="GAA_API_KEY"):
        cargar_config(ruta, entorno={"GAA_PERMITIR_REAL": "si"})
    cfg = cargar_config(
        ruta,
        entorno={"GAA_PERMITIR_REAL": "Si", "GAA_API_KEY": "k" * 10, "GAA_API_SECRET": "s" * 10},
    )
    assert cfg.modo == "real"


@pytest.mark.parametrize("valor", ["", "no", "true", "1", "sí"])
def test_permitir_real_solo_con_si(valor: str) -> None:
    cfg = cargar_config(CONFIG_PROYECTO, entorno={"GAA_PERMITIR_REAL": valor})
    assert cfg.secretos.permitir_real is False


def test_lee_env_y_no_expone_secretos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("GAA_API_KEY", "GAA_API_SECRET", "GAA_TELEGRAM_TOKEN", "GAA_PERMITIR_REAL"):
        monkeypatch.delenv(variable, raising=False)
    ruta = _proyecto(tmp_path, env=f"GAA_API_KEY={SECRETO}\nGAA_API_SECRET=otro-secreto-456\n")
    cfg = cargar_config(ruta)
    assert cfg.secretos.api_key is not None
    assert cfg.secretos.api_key.get_secret_value() == SECRETO
    assert sorted(cfg.secretos.valores()) == sorted([SECRETO, "otro-secreto-456"])
    for texto in (repr(cfg), str(cfg), str(cfg.resumen())):
        assert SECRETO not in texto
    assert cfg.resumen()["secretos"]["GAA_API_KEY"] == "definida"
    assert cfg.dir_datos == tmp_path / "data"


def test_variables_de_entorno_tienen_prioridad_sobre_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta = _proyecto(tmp_path, env="GAA_API_KEY=del-fichero-env\n")
    monkeypatch.setenv("GAA_API_KEY", "del-entorno-proceso")
    cfg = cargar_config(ruta)
    assert cfg.secretos.api_key is not None
    assert cfg.secretos.api_key.get_secret_value() == "del-entorno-proceso"
