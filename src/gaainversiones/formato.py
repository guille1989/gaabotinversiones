"""Formato de números al estilo español (1.234,56) para consola, avisos e informes."""

from __future__ import annotations

import math

_SIN_VALOR = ("—", "∞", "-∞")


def _miles(texto: str) -> str:
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def formato_numero(valor: float | None, decimales: int = 2, *, signo: bool = False) -> str:
    """``1234.5`` -> ``1.234,50``; NaN -> ``—``; infinito -> ``∞``."""
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return "—"
    if math.isinf(valor):
        return "∞" if valor > 0 else "-∞"
    return _miles(f"{valor:{'+' if signo else ''},.{decimales}f}")


def formato_eur(valor: float | None, *, signo: bool = False) -> str:
    texto = formato_numero(valor, 2, signo=signo)
    return texto if texto in _SIN_VALOR else f"{texto} €"


def formato_dinero(valor: float | None, moneda: str, *, signo: bool = False) -> str:
    """Importe con su moneda: ``1.000,00 EUR``."""
    texto = formato_numero(valor, 2, signo=signo)
    return texto if texto in _SIN_VALOR else f"{texto} {moneda}"


def formato_pct(valor: float, decimales: int = 1, *, signo: bool = False) -> str:
    texto = formato_numero(valor * 100 if math.isfinite(valor) else valor, decimales, signo=signo)
    return texto if texto in _SIN_VALOR else f"{texto} %"


def formato_entero(valor: float) -> str:
    return formato_numero(valor, 0)
