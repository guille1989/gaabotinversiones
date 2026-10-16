"""Bot en vivo: bucle, avisos, Telegram y kill switch."""

from gaainversiones.bot.bucle import Bot, BotDetenido, BotYaEnMarcha, VelaNoDisponible
from gaainversiones.bot.killswitch import FicheroParada, OrdenManual, escribir_parada
from gaainversiones.bot.notificador import Notificador, NotificadorLog

__all__ = [
    "Bot",
    "BotDetenido",
    "BotYaEnMarcha",
    "FicheroParada",
    "Notificador",
    "NotificadorLog",
    "OrdenManual",
    "VelaNoDisponible",
    "escribir_parada",
]
