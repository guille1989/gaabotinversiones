"""Confirmación manual del modo real.

Para operar con dinero real hacen falta tres cosas: ``modo: real`` en ``config.yaml``,
``GAA_PERMITIR_REAL=si`` en ``.env`` y escribir a mano la frase de confirmación en una
terminal al arrancar ``gaa real``.

La confirmación se guarda ligada a una **huella** de la configuración (parámetros,
riesgo, mercado y la clave de API, sin guardarla). Los reinicios automáticos (systemd,
Docker) la reutilizan mientras nada cambie. Se pierde, y hay que volver a confirmar, si:

- cambia cualquier parámetro de la configuración o la clave de API;
- se usa el kill switch o el bot se detiene por la caída máxima;
- se ejecuta ``gaa reanudar`` en modo real.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from gaainversiones.config import Config

FRASE_CONFIRMACION = "OPERAR CON DINERO REAL"


def ruta_autorizacion(config: Config) -> Path:
    return config.dir_datos / "autorizacion_real.json"


def huella(config: Config) -> str:
    """Huella de todo lo que, si cambia, exige volver a confirmar."""
    datos = config.model_dump(mode="json")  # sin secretos (excluidos del volcado)
    clave = config.secretos.api_key.get_secret_value() if config.secretos.api_key else ""
    datos["_clave_api"] = hashlib.sha256(clave.encode()).hexdigest()
    texto = json.dumps(datos, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(texto.encode()).hexdigest()


def autorizacion_vigente(config: Config) -> bool:
    ruta = ruta_autorizacion(config)
    try:
        guardada = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(guardada.get("huella") == huella(config))


def conceder(config: Config) -> None:
    ruta = ruta_autorizacion(config)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        json.dumps({"huella": huella(config), "fecha": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )


def revocar(config: Config) -> None:
    ruta_autorizacion(config).unlink(missing_ok=True)
