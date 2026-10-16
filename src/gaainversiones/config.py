"""Configuración central.

Todos los parámetros se leen de ``config/config.yaml`` y los secretos de ``.env``
(o del entorno). Se validan al cargar: cualquier valor incoherente detiene el arranque
con un mensaje que dice qué campo está mal.

Los secretos se guardan como ``SecretStr``: su ``repr`` y ``str`` muestran asteriscos,
así que imprimir o registrar la configuración no los filtra.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import ccxt
import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from gaainversiones.tiempo import fecha_a_ms, timeframe_a_ms

RUTA_CONFIG_POR_DEFECTO = Path("config") / "config.yaml"


class ErrorConfig(Exception):
    """Configuración ausente o no válida."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validar_exchange(nombre: str) -> str:
    if nombre not in ccxt.exchanges:
        raise ValueError(f"exchange desconocido para ccxt: {nombre!r}")
    return nombre


class ConfigMercado(_Base):
    """Mercado en el que opera el bot."""

    exchange: str = "kraken"
    simbolo: str = "BTC/EUR"
    temporalidad: str = "4h"
    # Límites de orden del exchange para el backtest (en vivo se leen del propio exchange).
    cantidad_minima: float = Field(0.00005, ge=0)
    importe_minimo: float = Field(0.5, ge=0)
    decimales_cantidad: int = Field(8, ge=0, le=12)

    @field_validator("exchange")
    @classmethod
    def _exchange(cls, v: str) -> str:
        return _validar_exchange(v)

    @field_validator("simbolo")
    @classmethod
    def _simbolo(cls, v: str) -> str:
        partes = v.split("/")
        if len(partes) != 2 or not all(partes):
            raise ValueError(f"el símbolo debe tener la forma BASE/COTIZADA, p. ej. BTC/EUR: {v!r}")
        return v.upper()

    @field_validator("temporalidad")
    @classmethod
    def _temporalidad(cls, v: str) -> str:
        timeframe_a_ms(v)
        return v

    @property
    def base(self) -> str:
        """Activo que se compra (BTC en BTC/EUR)."""
        return self.simbolo.split("/")[0]

    @property
    def cotizada(self) -> str:
        """Moneda con la que se paga (EUR en BTC/EUR)."""
        return self.simbolo.split("/")[1]


class ConfigDatos(_Base):
    """Origen y caché local de las velas históricas."""

    fuente_historica: str = "bitstamp"
    desde: str = "2017-01-01"
    directorio: Path = Path("data")
    rellenar_huecos: bool = True

    @field_validator("fuente_historica")
    @classmethod
    def _fuente(cls, v: str) -> str:
        return _validar_exchange(v)

    @field_validator("desde")
    @classmethod
    def _desde(cls, v: str) -> str:
        try:
            fecha_a_ms(v)
        except ValueError as e:
            raise ValueError(f"fecha no válida: {v!r}") from e
        return v


class ConfigEstrategia(_Base):
    """Estrategia activa y sus parámetros (los valida la propia estrategia)."""

    nombre: str = "cruce_medias_rsi"
    parametros: dict[str, Any] = Field(default_factory=dict)


class ConfigRiesgo(_Base):
    """Límites de riesgo. La capa de riesgo manda siempre sobre la estrategia."""

    riesgo_por_operacion: float = Field(0.01, gt=0, le=0.05)
    atr_periodo: int = Field(14, ge=2, le=200)
    sl_atr: float = Field(2.0, gt=0, le=20)
    tp_atr: float = Field(3.0, gt=0, le=50)
    perdida_diaria_max: float = Field(0.03, gt=0, lt=1)
    caida_max: float = Field(0.15, gt=0, lt=1)
    exposicion_max: float = Field(1.0, gt=0, le=1.0)

    @model_validator(mode="after")
    def _coherencia(self) -> ConfigRiesgo:
        if self.perdida_diaria_max > self.caida_max:
            raise ValueError("perdida_diaria_max no puede ser mayor que caida_max")
        if self.riesgo_por_operacion >= self.perdida_diaria_max:
            raise ValueError(
                "riesgo_por_operacion debe ser menor que perdida_diaria_max "
                "(si no, una sola operación perdedora detendría el bot)"
            )
        return self


class ConfigCostes(_Base):
    """Comisiones y deslizamiento usados por el backtest y el PaperBroker."""

    comision_maker: float = Field(0.0025, ge=0, lt=0.05)
    comision_taker: float = Field(0.0040, ge=0, lt=0.05)
    deslizamiento_pb: float = Field(5.0, ge=0, le=500)


def _validar_fecha(v: str | None) -> str | None:
    if v is None:
        return v
    try:
        fecha_a_ms(v)
    except ValueError as e:
        raise ValueError(f"fecha no válida: {v!r}") from e
    return v


class ConfigWalkForward(_Base):
    """Ventanas del walk-forward: se optimiza en ``entrenamiento`` y se prueba en ``prueba``."""

    entrenamiento_meses: int = Field(36, ge=3, le=120)
    prueba_meses: int = Field(6, ge=1, le=36)


class ConfigBacktest(_Base):
    """Periodo, separación in-sample / out-of-sample, optimización e informes."""

    desde: str | None = None  # por defecto, datos.desde
    hasta: str | None = None  # por defecto, hasta la última vela
    inicio_oos: str = "2023-01-01"
    metrica_optimizacion: Literal["sharpe", "sortino", "cagr", "calmar", "profit_factor"] = "sharpe"
    operaciones_minimas: int = Field(5, ge=0)
    detener_en_caida_max: bool = True
    walkforward: ConfigWalkForward = Field(default_factory=ConfigWalkForward)
    rejilla: dict[str, list[Any]] = Field(default_factory=dict)
    directorio_informes: Path = Path("informes")

    @field_validator("desde", "hasta", "inicio_oos")
    @classmethod
    def _fechas(cls, v: str | None) -> str | None:
        return _validar_fecha(v)

    @field_validator("rejilla")
    @classmethod
    def _rejilla(cls, v: dict[str, list[Any]]) -> dict[str, list[Any]]:
        vacios = [nombre for nombre, valores in v.items() if not valores]
        if vacios:
            raise ValueError(f"la rejilla tiene parámetros sin valores: {', '.join(vacios)}")
        return v


class ConfigPaper(_Base):
    """Parámetros del modo simulado."""

    capital_inicial: float = Field(1000.0, gt=0)
    profundidad_libro: int = Field(50, ge=1, le=500)


class ConfigBot(_Base):
    """Bucle en vivo (paper y real)."""

    intervalo_s: int = Field(30, ge=5, le=3600)  # revisión de stops, kill switch, latido
    retraso_cierre_s: int = Field(15, ge=0, le=600)  # espera tras el cierre antes de pedir la vela
    velas_historia: int = Field(720, ge=50, le=720)  # Kraken no da más de 720 velas
    max_errores_seguidos: int = Field(10, ge=1, le=1000)


class ConfigRegistro(_Base):
    """Logging a consola y a fichero rotativo."""

    nivel: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    directorio: Path = Path("logs")
    fichero: str = "gaainversiones.log"
    max_bytes: int = Field(5_000_000, ge=10_000)
    copias: int = Field(5, ge=1, le=100)


class Secretos(_Base):
    """Secretos leídos de ``.env`` o del entorno. Nunca se escriben en ficheros ni logs."""

    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    telegram_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    permitir_real: bool = False

    @classmethod
    def desde_entorno(cls, entorno: Mapping[str, str | None]) -> Secretos:
        """Construye los secretos con las variables ``GAA_*``; las vacías cuentan como ausentes."""

        def leer(nombre: str) -> str | None:
            valor = (entorno.get(nombre) or "").strip()
            return valor or None

        def secreto(nombre: str) -> SecretStr | None:
            valor = leer(nombre)
            return SecretStr(valor) if valor else None

        return cls(
            api_key=secreto("GAA_API_KEY"),
            api_secret=secreto("GAA_API_SECRET"),
            telegram_token=secreto("GAA_TELEGRAM_TOKEN"),
            telegram_chat_id=leer("GAA_TELEGRAM_CHAT_ID"),
            permitir_real=(leer("GAA_PERMITIR_REAL") or "").lower() == "si",
        )

    def valores(self) -> list[str]:
        """Valores en claro de los secretos presentes (para enmascararlos en los logs)."""
        secretos = (self.api_key, self.api_secret, self.telegram_token)
        return [s.get_secret_value() for s in secretos if s is not None]

    def resumen(self) -> dict[str, str]:
        """Qué secretos están definidos, sin revelar su valor."""
        return {
            "GAA_API_KEY": "definida" if self.api_key else "vacía",
            "GAA_API_SECRET": "definida" if self.api_secret else "vacía",
            "GAA_TELEGRAM_TOKEN": "definido" if self.telegram_token else "vacío",
            "GAA_TELEGRAM_CHAT_ID": "definido" if self.telegram_chat_id else "vacío",
            "GAA_PERMITIR_REAL": "si" if self.permitir_real else "no",
        }


class Config(_Base):
    """Configuración completa y validada."""

    modo: Literal["paper", "real"] = "paper"
    mercado: ConfigMercado = Field(default_factory=ConfigMercado)
    datos: ConfigDatos = Field(default_factory=ConfigDatos)
    estrategia: ConfigEstrategia = Field(default_factory=ConfigEstrategia)
    riesgo: ConfigRiesgo = Field(default_factory=ConfigRiesgo)
    costes: ConfigCostes = Field(default_factory=ConfigCostes)
    backtest: ConfigBacktest = Field(default_factory=ConfigBacktest)
    paper: ConfigPaper = Field(default_factory=ConfigPaper)
    bot: ConfigBot = Field(default_factory=ConfigBot)
    registro: ConfigRegistro = Field(default_factory=ConfigRegistro)
    secretos: Secretos = Field(default_factory=Secretos, exclude=True)
    raiz: Path = Field(default_factory=Path.cwd, exclude=True)

    @model_validator(mode="after")
    def _cerrojo_modo_real(self) -> Config:
        if self.modo == "real":
            if not self.secretos.permitir_real:
                raise ValueError("modo real: falta GAA_PERMITIR_REAL=si en .env")
            if not (self.secretos.api_key and self.secretos.api_secret):
                raise ValueError("modo real: faltan GAA_API_KEY y/o GAA_API_SECRET en .env")
        return self

    def ruta(self, ruta: Path) -> Path:
        """Resuelve una ruta de la configuración respecto a la raíz del proyecto."""
        return ruta if ruta.is_absolute() else self.raiz / ruta

    @property
    def dir_datos(self) -> Path:
        return self.ruta(self.datos.directorio)

    @property
    def dir_logs(self) -> Path:
        return self.ruta(self.registro.directorio)

    @property
    def dir_informes(self) -> Path:
        return self.ruta(self.backtest.directorio_informes)

    @property
    def ruta_db_velas(self) -> Path:
        """Base de datos SQLite con la caché de velas."""
        return self.dir_datos / "velas.sqlite"

    def ruta_db_estado(self, modo: str) -> Path:
        """Base de datos SQLite con el estado del bot en ese modo (paper o real)."""
        return self.dir_datos / f"estado_{modo}.sqlite"

    def resumen(self) -> dict[str, Any]:
        """Configuración en forma de diccionario, sin secretos (apta para mostrar o registrar)."""
        datos: dict[str, Any] = self.model_dump(mode="json")
        datos["secretos"] = self.secretos.resumen()
        return datos


def _raiz_proyecto(ruta_config: Path) -> Path:
    """La raíz es la carpeta que contiene ``config/``; si no, la carpeta del fichero."""
    carpeta = ruta_config.resolve().parent
    return carpeta.parent if carpeta.name == "config" else carpeta


def _formatear_errores(error: ValidationError) -> str:
    lineas = []
    for detalle in error.errors():
        campo = ".".join(str(p) for p in detalle["loc"]) or "(configuración)"
        lineas.append(f"  - {campo}: {detalle['msg']}")
    return "\n".join(lineas)


def cargar_config(
    ruta: Path | str | None = None,
    *,
    entorno: Mapping[str, str | None] | None = None,
) -> Config:
    """Carga y valida la configuración.

    Args:
        ruta: fichero YAML. Por defecto ``config/config.yaml`` en el directorio actual.
        entorno: variables con los secretos. Si es ``None`` se combinan el ``.env`` de la
            raíz del proyecto y las variables de entorno del proceso (estas tienen prioridad).

    Raises:
        ErrorConfig: si el fichero no existe o algún valor no es válido.
    """
    ruta_config = Path(ruta) if ruta is not None else RUTA_CONFIG_POR_DEFECTO
    if not ruta_config.is_file():
        raise ErrorConfig(f"No existe el fichero de configuración: {ruta_config}")
    raiz = _raiz_proyecto(ruta_config)

    try:
        contenido = yaml.safe_load(ruta_config.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ErrorConfig(f"{ruta_config} no es un YAML válido: {e}") from None
    if not isinstance(contenido, dict):
        raise ErrorConfig(f"{ruta_config} debe contener un diccionario de secciones")
    for prohibida in ("secretos", "raiz"):
        if prohibida in contenido:
            raise ErrorConfig(
                f"{ruta_config}: la sección {prohibida!r} no se admite en el YAML "
                "(los secretos van en .env)"
            )

    if entorno is None:
        ruta_env = raiz / ".env"
        entorno = {**dotenv_values(ruta_env), **os.environ} if ruta_env.is_file() else os.environ

    try:
        return Config(**contenido, secretos=Secretos.desde_entorno(entorno), raiz=raiz)
    except ValidationError as e:
        raise ErrorConfig(
            f"Configuración no válida ({ruta_config}):\n{_formatear_errores(e)}"
        ) from None
