import asyncio
import json
import os
import re
import typing
from copy import deepcopy

from dotenv import load_dotenv
import isodate

import discord
from discord.ext.commands import BadArgument

from core._color_data import ALL_COLORS
from core.models import InvalidConfigError, Default, getLogger
from core.time import UserFriendlyTimeSync
from core.utils import strtobool

logger = getLogger(__name__)
load_dotenv()


class ConfigManager:

    public_keys = {
        # Actividad
        "twitch_url": "https://www.twitch.tv/#",
        # Ajustes del BOT
        "main_category_id": None,
        "fallback_category_id": None,
        "prefix": "/",
        "mention": "@here",
        "main_color": str(discord.Color.blurple()),
        "error_color": str(discord.Color.red()),
        "user_typing": True,
        "mod_typing": False,
        "account_age": isodate.Duration(),
        "guild_age": isodate.Duration(),
        "thread_cooldown": isodate.Duration(),
        "reply_without_command": False,
        "anon_reply_without_command": False,
        # Registros
        "log_channel_id": None,
        # Hilos
        "sent_emoji": "✅",
        "blocked_emoji": "🚫",
        "close_emoji": "🔒",
        "recipient_thread_close": False,
        "thread_auto_close_silently": False,
        "thread_auto_close": isodate.Duration(),
        "thread_auto_close_response": "Este hilo se ha cerrado automáticamente debido a una inactividad luego de {timeout}.",
        "thread_creation_response": "Hemos recibido tu mensaje! Nuestro Equipo te estará respondiendo pronto. Ten paciencia!",
        "thread_creation_footer": "Tu mensaje fue enviado",
        "thread_self_closable_creation_footer": "Clickea en el candado para cerrar el hilo",
        "thread_creation_title": "Hilo creado",
        "thread_close_footer": "Responder creará otro hilo",
        "thread_close_title": "Hilo cerrado",
        "thread_close_response": "{closer.mention} ha cerrado este hilo.",
        "thread_self_close_response": "Tú has cerrado este hilo.",
        "thread_move_notify": False,
        "thread_move_response": "Este hilo fue movido.",
        "disabled_new_thread_title": "Mensaje no enviado.",
        "disabled_new_thread_response": "No estamos aceptando nuevos hilos, solo respondemos a hilos creados.",
        "disabled_new_thread_footer": "Por favor, inténtalo de nuevo más tarde.",
        "disabled_current_thread_title": "Mensaje no enviado.",
        "disabled_current_thread_response": "No estamos aceptando ningún mensaje.",
        "disabled_current_thread_footer": "Por favor, inténtalo de nuevo más tarde.",
        # Moderación
        "recipient_color": str(discord.Color.gold()),
        "mod_color": str(discord.Color.green()),
        "mod_tag": None,
        # Mensajes anónimos
        "anon_username": None,
        "anon_avatar_url": None,
        "anon_tag": "Respuesta",
    }

    private_keys = {
        # Presencia del BOT
        "activity_message": "Envíame un mensaje con tu duda / problema o reporte!",
        "activity_type": None,
        "status": None,
        # dm_disabled 0 = ninguno, 1 = nuevos hilos, 2 = todos los hilos
        # TODO: use emum
        "dm_disabled": 0,
        "oauth_whitelist": [],
        # Moderación
        "blocked": {},
        "blocked_whitelist": [],
        "command_permissions": {},
        "level_permissions": {},
        "override_command_level": {},
        # Hilos
        "snippets": {},
        "notification_squad": {},
        "subscriptions": {},
        "closures": {},
        # Misceláneo
        "plugins": [],
        "aliases": {},
    }

    protected_keys = {
        # ModMail
        "modmail_guild_id": None,
        "guild_id": None,
        "log_url": "https://example.com/",
        "log_url_prefix": "/logs",
        "mongo_uri": None,
        "owners": None,
        # BOT
        "token": None,
        # Registros
        "log_level": "INFO",
        "enable_plugins": True,
    }

    colors = {"mod_color", "recipient_color", "main_color", "error_color"}

    time_deltas = {"account_age", "guild_age", "thread_auto_close", "thread_cooldown"}

    booleans = {
        "user_typing",
        "mod_typing",
        "reply_without_command",
        "anon_reply_without_command",
        "recipient_thread_close",
        "thread_auto_close_silently",
        "thread_move_notify",
        "enable_plugins",
    }

    special_types = {"status", "activity_type"}

    defaults = {**public_keys, **private_keys, **protected_keys}
    all_keys = set(defaults.keys())

    def __init__(self, bot):
        self.bot = bot
        self._cache = {}
        self.ready_event = asyncio.Event()
        self.config_help = {}

    def __repr__(self):
        return repr(self._cache)

    def populate_cache(self) -> dict:
        data = deepcopy(self.defaults)

        # populate from env var and .env file
        data.update({k.lower(): v for k, v in os.environ.items() if k.lower() in self.all_keys})
        config_json = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
        )
        if os.path.exists(config_json):
            logger.debug("Loading envs from config.json.")
            with open(config_json, "r", encoding="utf-8") as f:
                # Config json should override env vars
                try:
                    data.update(
                        {
                            k.lower(): v
                            for k, v in json.load(f).items()
                            if k.lower() in self.all_keys
                        }
                    )
                except json.JSONDecodeError:
                    logger.critical("Falló al cargar valores de variables de .ENV", exc_info=True)
        self._cache = data

        config_help_json = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config_help.json"
        )
        with open(config_help_json, "r", encoding="utf-8") as f:
            self.config_help = dict(sorted(json.load(f).items()))

        return self._cache

    async def update(self):
        """Updates the config with data from the cache"""
        await self.bot.api.update_config(self.filter_default(self._cache))

    async def refresh(self) -> dict:
        """Refreshes internal cache with data from database"""
        for k, v in (await self.bot.api.get_config()).items():
            k = k.lower()
            if k in self.all_keys:
                self._cache[k] = v
        if not self.ready_event.is_set():
            self.ready_event.set()
            logger.debug("Se obtuvo la información de la base de datos correctamente.")
        return self._cache

    async def wait_until_ready(self) -> None:
        await self.ready_event.wait()

    def __setitem__(self, key: str, item: typing.Any) -> None:
        key = key.lower()
        logger.info("Setting %s.", key)
        if key not in self.all_keys:
            raise InvalidConfigError(f'Clave de configuración "{key}" es inválida.')
        self._cache[key] = item

    def __getitem__(self, key: str) -> typing.Any:
        key = key.lower()
        if key not in self.all_keys:
            raise InvalidConfigError(f'Clave de configuración "{key}" es inválida.')
        if key not in self._cache:
            self._cache[key] = deepcopy(self.defaults[key])
        return self._cache[key]

    def __delitem__(self, key: str) -> None:
        return self.remove(key)

    def get(self, key: str, convert=True) -> typing.Any:
        value = self.__getitem__(key)

        if not convert:
            return value

        if key in self.colors:
            try:
                return int(value.lstrip("#"), base=16)
            except ValueError:
                logger.error("Inválido %s.", key)
            value = int(self.remove(key).lstrip("#"), base=16)

        elif key in self.time_deltas:
            if not isinstance(value, isodate.Duration):
                try:
                    value = isodate.parse_duration(value)
                except isodate.ISO8601Error:
                    logger.warning(
                        "El límite de edad de la cuenta ${account} debe ser un"
                        'ISO-8601 duración formateada, no "%s".',
                        value,
                    )
                    value = self.remove(key)

        elif key in self.booleans:
            try:
                value = strtobool(value)
            except ValueError:
                value = self.remove(key)

        elif key in self.special_types:
            if value is None:
                return None

            if key == "status":
                try:
                    # noinspection PyArgumentList
                    value = discord.Status(value)
                except ValueError:
                    logger.warning("Estado inválido %s.", value)
                    value = self.remove(key)

            elif key == "activity_type":
                try:
                    # noinspection PyArgumentList
                    value = discord.ActivityType(value)
                except ValueError:
                    logger.warning("Actividad inválida %s.", value)
                    value = self.remove(key)

        return value

    def set(self, key: str, item: typing.Any, convert=True) -> None:
        if not convert:
            return self.__setitem__(key, item)

        if key in self.colors:
            try:
                hex_ = str(item)
                if hex_.startswith("#"):
                    hex_ = hex_[1:]
                if len(hex_) == 3:
                    hex_ = "".join(s for s in hex_ for _ in range(2))
                if len(hex_) != 6:
                    raise InvalidConfigError("Nombre de color o HEX inválido.")
                try:
                    int(hex_, 16)
                except ValueError:
                    raise InvalidConfigError("Nombre de color o HEX inválido.")

            except InvalidConfigError:
                name = str(item).lower()
                name = re.sub(r"[\-+|. ]+", " ", name)
                hex_ = ALL_COLORS.get(name)
                if hex_ is None:
                    name = re.sub(r"[\-+|. ]+", "", name)
                    hex_ = ALL_COLORS.get(name)
                    if hex_ is None:
                        raise
            return self.__setitem__(key, "#" + hex_)

        if key in self.time_deltas:
            try:
                isodate.parse_duration(item)
            except isodate.ISO8601Error:
                try:
                    converter = UserFriendlyTimeSync()
                    time = converter.convert(None, item)
                    if time.arg:
                        raise ValueError
                except BadArgument as exc:
                    raise InvalidConfigError(*exc.args)
                except Exception as e:
                    logger.debug(e)
                    raise InvalidConfigError(
                        "Tiempo no reconocido, por favor use el formato de duración ISO-8601 "
                        'o un tiempo de "lectura humana" más simple.'
                    )
                item = isodate.duration_isoformat(time.dt - converter.now)
            return self.__setitem__(key, item)

        if key in self.booleans:
            try:
                return self.__setitem__(key, strtobool(item))
            except ValueError:
                raise InvalidConfigError("Debe ser un valor de Sí/No.")

        # elif key in self.special_types:
        #     if key == "status":

        return self.__setitem__(key, item)

    def remove(self, key: str) -> typing.Any:
        key = key.lower()
        logger.info("Removiendo %s.", key)
        if key not in self.all_keys:
            raise InvalidConfigError(f'Clave de configuración "{key}" es inválida.')
        if key in self._cache:
            del self._cache[key]
        self._cache[key] = deepcopy(self.defaults[key])
        return self._cache[key]

    def items(self) -> typing.Iterable:
        return self._cache.items()

    @classmethod
    def filter_valid(cls, data: typing.Dict[str, typing.Any]) -> typing.Dict[str, typing.Any]:
        return {
            k.lower(): v
            for k, v in data.items()
            if k.lower() in cls.public_keys or k.lower() in cls.private_keys
        }

    @classmethod
    def filter_default(cls, data: typing.Dict[str, typing.Any]) -> typing.Dict[str, typing.Any]:
        # TODO: use .get to prevent errors
        filtered = {}
        for k, v in data.items():
            default = cls.defaults.get(k.lower(), Default)
            if default is Default:
                logger.error("Configuración inesperada detectada: %s.", k)
                continue
            if v != default:
                filtered[k.lower()] = v
        return filtered
