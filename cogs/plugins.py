import asyncio
import io
import json
import os
import shutil
import sys
import typing
import zipfile
from importlib import invalidate_caches
from difflib import get_close_matches
from pathlib import Path, PurePath
from re import match
from site import USER_SITE
from subprocess import PIPE

import discord
from discord.ext import commands

from pkg_resources import parse_version

from core import checks
from core.models import PermissionLevel, getLogger
from core.paginator import EmbedPaginatorSession
from core.utils import truncate, trigger_typing

logger = getLogger(__name__)


class InvalidPluginError(commands.BadArgument):
    pass


class Plugin:
    def __init__(self, user, repo, name, branch=None):
        self.user = user
        self.repo = repo
        self.name = name
        self.branch = branch if branch is not None else "master"
        self.url = f"https://github.com/{user}/{repo}/archive/{self.branch}.zip"
        self.link = f"https://github.com/{user}/{repo}/tree/{self.branch}/{name}"

    @property
    def path(self):
        return PurePath("plugins") / self.user / self.repo / f"{self.name}-{self.branch}"

    @property
    def abs_path(self):
        return Path(__file__).absolute().parent.parent / self.path

    @property
    def cache_path(self):
        return (
            Path(__file__).absolute().parent.parent
            / "temp"
            / "plugins-cache"
            / f"{self.user}-{self.repo}-{self.branch}.zip"
        )

    @property
    def ext_string(self):
        return f"plugins.{self.user}.{self.repo}.{self.name}-{self.branch}.{self.name}"

    def __str__(self):
        return f"{self.user}/{self.repo}/{self.name}@{self.branch}"

    def __lt__(self, other):
        return self.name.lower() < other.name.lower()

    @classmethod
    def from_string(cls, s, strict=False):
        if not strict:
            m = match(r"^(.+?)/(.+?)/(.+?)(?:@(.+?))?$", s)
        else:
            m = match(r"^(.+?)/(.+?)/(.+?)@(.+?)$", s)
        if m is not None:
            return Plugin(*m.groups())
        raise InvalidPluginError("No se puede descifrar %s.", s)  # pylint: disable=raising-format-tuple

    def __hash__(self):
        return hash((self.user, self.repo, self.name, self.branch))

    def __repr__(self):
        return f"<Plugins: {self.__str__()}>"

    def __eq__(self, other):
        return isinstance(other, Plugin) and self.__str__() == other.__str__()


class Plugins(commands.Cog):
    """
    Plugins expand Modmail functionality by allowing third-party addons.

    These addons could have a range of features from moderation to simply
    making your life as a moderator easier!
    Learn how to create a plugin yourself here:
    https://github.com/kyb3r/modmail/wiki/Plugins
    """

    def __init__(self, bot):
        self.bot = bot
        self.registry = {}
        self.loaded_plugins = set()
        self._ready_event = asyncio.Event()

        self.bot.loop.create_task(self.populate_registry())

        if self.bot.config.get("enable_plugins"):
            self.bot.loop.create_task(self.initial_load_plugins())
        else:
            logger.info("Plugins no serán cargados mientras: ENABLE_PLUGINS=false.")

    async def populate_registry(self):
        url = "https://raw.githubusercontent.com/kyb3r/modmail/master/plugins/registry.json"
        async with self.bot.session.get(url) as resp:
            self.registry = json.loads(await resp.text())

    async def initial_load_plugins(self):
        await self.bot.wait_for_connected()

        for plugin_name in list(self.bot.config["plugins"]):
            try:
                plugin = Plugin.from_string(plugin_name, strict=True)
            except InvalidPluginError:
                self.bot.config["plugins"].remove(plugin_name)
                try:
                    # For backwards compat
                    plugin = Plugin.from_string(plugin_name)
                except InvalidPluginError:
                    logger.error("Fallo al analizar el plugin: %s.", plugin_name, exc_info=True)
                    continue

                logger.info("Migrado el plugin hereditario con el nobre %s, ahora %s.", plugin_name, str(plugin))
                self.bot.config["plugins"].append(str(plugin))

            try:
                await self.download_plugin(plugin)
                await self.load_plugin(plugin)
            except Exception:
                logger.error("Error al cargar el plugin %s.", plugin, exc_info=True)
                continue

        logger.debug("Finalizado el proceso de carga de plugins.")
        self._ready_event.set()
        await self.bot.config.update()

    async def download_plugin(self, plugin, force=False):
        if plugin.abs_path.exists() and not force:
            return

        plugin.abs_path.mkdir(parents=True, exist_ok=True)

        if plugin.cache_path.exists() and not force:
            plugin_io = plugin.cache_path.open("rb")
            logger.debug("Cargando en caché %s.", plugin.cache_path)

        else:
            async with self.bot.session.get(plugin.url) as resp:
                logger.debug("Descargando %s.", plugin.url)
                raw = await resp.read()
                plugin_io = io.BytesIO(raw)
                if not plugin.cache_path.parent.exists():
                    plugin.cache_path.parent.mkdir(parents=True)

                with plugin.cache_path.open("wb") as f:
                    f.write(raw)

        with zipfile.ZipFile(plugin_io) as zipf:
            for info in zipf.infolist():
                path = PurePath(info.filename)
                if len(path.parts) >= 3 and path.parts[1] == plugin.name:
                    plugin_path = plugin.abs_path / Path(*path.parts[2:])
                    if info.is_dir():
                        plugin_path.mkdir(parents=True, exist_ok=True)
                    else:
                        plugin_path.parent.mkdir(parents=True, exist_ok=True)
                        with zipf.open(info) as src, plugin_path.open("wb") as dst:
                            shutil.copyfileobj(src, dst)

        plugin_io.close()

    async def load_plugin(self, plugin):
        if not (plugin.abs_path / f"{plugin.name}.py").exists():
            raise InvalidPluginError(f"{plugin.name}.py no encontrado.")

        req_txt = plugin.abs_path / "requirements.txt"

        if req_txt.exists():
            # Install PIP requirements

            venv = hasattr(sys, "real_prefix") or hasattr(sys, "base_prefix") # in a virtual env
            user_install = " --user" if not venv else ""
            proc = await asyncio.create_subprocess_shell(
                f"{sys.executable} -m pip install --upgrade{user_install} -r {req_txt} -q -q",
                stderr=PIPE,
                stdout=PIPE,
            )

            logger.debug("Descargando requisitos para %s.", plugin.ext_string)

            stdout, stderr = await proc.communicate()

            if stdout:
                logger.debug("[stdout]\n%s.", stdout.decode())

            if stderr:
                logger.debug("[stderr]\n%s.", stderr.decode())
                logger.error(
                    "No se han podido descargar los requisitos para %s.", plugin.ext_string, exc_info=True
                )
                raise InvalidPluginError(
                    f"No se pueden descargar los requisitos: ```\n{stderr.decode()}\n```"
                )

            if os.path.exists(USER_SITE):
                sys.path.insert(0, USER_SITE)

        try:
            self.bot.load_extension(plugin.ext_string)
            logger.info("Plugin cargado %s", plugin.ext_string.split(".")[-1])
            self.loaded_plugins.add(plugin)

        except commands.ExtensionError as exc:
            logger.error("Fallo en la catga del plugin: %s", plugin.ext_string, exc_info=True)
            raise InvalidPluginError("No se puede cargar la extensión, plugin inválido.") from exc

    async def parse_user_input(self, ctx, plugin_name, check_version=False):

        if not self._ready_event.is_set():
            embed = discord.Embed(
                description="Los plugins todavía están cargando, por favor, intenta de nuevo más tarde.",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
            return

        if plugin_name in self.registry:
            details = self.registry[plugin_name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            if check_version:
                required_version = details.get("bot_version", False)

                if required_version and self.bot.version < parse_version(required_version):
                    embed = discord.Embed(
                        description="La versión del BOT es muy baja"
                        f"Este plugin requiere la versión `{required_version}`.",
                        color=self.bot.error_color,
                    )
                    await ctx.send(embed=embed)
                    return

            plugin = Plugin(user, repo, plugin_name, branch)

        else:
            try:
                plugin = Plugin.from_string(plugin_name)
            except InvalidPluginError:
                embed = discord.Embed(
                    description="Nombre del plugin inválido, compruebe dos veces el nombre del plugin "
                    "o usa uno de los siguientes formatos:"
                    "username/repo/plugin, username/repo/plugin@branch.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed)
                return
        return plugin

    @commands.group(aliases=["plugin"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins(self, ctx):
        """
        Manage plugins for Modmail.
        """

        await ctx.send_help(ctx.command)

    @plugins.command(name="add", aliases=["install", "load"])
    @checks.has_permissions(PermissionLevel.OWNER)
    @trigger_typing
    async def plugins_add(self, ctx, *, plugin_name: str):
        """
        Install a new plugin for the bot.

        `plugin_name` can be the name of the plugin found in `{prefix}plugin registry`,
        or a direct reference to a GitHub hosted plugin (in the format `user/repo/name[@branch]`).
        """

        plugin = await self.parse_user_input(ctx, plugin_name, check_version=True)
        if plugin is None:
            return

        if str(plugin) in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="Este plugin ya está instalado.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        if plugin.name in self.bot.cogs:
            # another class with the same name
            embed = discord.Embed(
                description="No se puede instalar este plugin (nombre de pieza engañoso)",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            description=f"Iniciando la descarga de {plugin.link}...",
            color=self.bot.main_color,
        )
        msg = await ctx.send(embed=embed)

        try:
            await self.download_plugin(plugin, force=True)
        except Exception:
            logger.warning("No se pudo descargar el plugin %s.", plugin, exc_info=True)

            embed = discord.Embed(
                description="Fallo al descargar el plugin, revisa los registros para ver el error.",
                color=self.bot.error_color,
            )

            return await msg.edit(embed=embed)

        self.bot.config["plugins"].append(str(plugin))
        await self.bot.config.update()

        if self.bot.config.get("enable_plugins"):

            invalidate_caches()

            try:
                await self.load_plugin(plugin)
            except Exception:
                logger.warning("No se pudo cargar el plugin %s.", plugin, exc_info=True)

                embed = discord.Embed(
                    description="Fallo al descargar el plugin, revisa los registros para ver el error.",
                    color=self.bot.error_color,
                )

            else:
                embed = discord.Embed(
                    description="Plugin instalado correctamente.\n"
                    "Solo instala plugins que sean de desarrolladores de confianza, pues tienen acceso total al BOT.",
                    color=self.bot.main_color,
                )
        else:
            embed = discord.Embed(
                description="Plugin instalado correctamenten.\n"
                "Solo instala plugins que sean de desarrolladores de confianza, pues tienen acceso total al BOT."
                "Este plugin permanecerá deshabilitado debido a que la opción: `ENABLE_PLUGINS` está en `false`. "
                "Para rehabilitar el plugin, cambia `ENABLE_PLUGINS` a `true` y reinicia al BOT.",
                color=self.bot.main_color,
            )
        return await msg.edit(embed=embed)

    @plugins.command(name="remove", aliases=["del", "delete"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_remove(self, ctx, *, plugin_name: str):
        """
        Remove an installed plugin of the bot.

        `plugin_name` can be the name of the plugin found in `{prefix}plugin registry`, or a direct reference
        to a GitHub hosted plugin (in the format `user/repo/name[@branch]`).
        """
        plugin = await self.parse_user_input(ctx, plugin_name)
        if plugin is None:
            return

        if str(plugin) not in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="El plugin no está instalado.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        if self.bot.config.get("enable_plugins"):
            try:
                self.bot.unload_extension(plugin.ext_string)
                self.loaded_plugins.remove(plugin)
            except (commands.ExtensionNotLoaded, KeyError):
                logger.warning("El plugin nunca fue cargado.")

        self.bot.config["plugins"].remove(str(plugin))
        await self.bot.config.update()
        shutil.rmtree(
            plugin.abs_path,
            onerror=lambda *args: logger.warning(
                "Fallo al remover archivos del plugin %s: %s", plugin, str(args[2])
            ),
        )
        try:
            plugin.abs_path.parent.rmdir()
            plugin.abs_path.parent.parent.rmdir()
        except OSError:
            pass  # dir not empty

        embed = discord.Embed(
            description="El plugin fue correctamente desinstalado.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    async def update_plugin(self, ctx, plugin_name):
        logger.debug("Actualizando %s.", plugin_name)
        plugin = await self.parse_user_input(ctx, plugin_name, check_version=True)
        if plugin is None:
            return

        if str(plugin) not in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="El plugin no está instalado.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        async with ctx.typing():
            await self.download_plugin(plugin, force=True)
            if self.bot.config.get("enable_plugins"):
                try:
                    self.bot.unload_extension(plugin.ext_string)
                except commands.ExtensionError:
                    logger.warning("Fallo al quitar el plugin.", exc_info=True)
                await self.load_plugin(plugin)
            logger.debug("Actualizado %s.", plugin_name)
            embed = discord.Embed(
                description=f"Actualizado {plugin.name} correctamente.", color=self.bot.main_color
            )
            return await ctx.send(embed=embed)

    @plugins.command(name="update")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_update(self, ctx, *, plugin_name: str = None):
        """
        Update a plugin for the bot.

        `plugin_name` can be the name of the plugin found in `{prefix}plugin registry`, or a direct reference
        to a GitHub hosted plugin (in the format `user/repo/name[@branch]`).

        To update all plugins, do `{prefix}plugins update`.
        """

        if plugin_name is None:
            # pylint: disable=redefined-argument-from-local
            for plugin_name in self.bot.config["plugins"]:
                await self.update_plugin(ctx, plugin_name)
        else:
            await self.update_plugin(ctx, plugin_name)

    @plugins.command(name="loaded", aliases=["enabled", "installed"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_loaded(self, ctx):
        """
        Show a list of currently loaded plugins.
        """

        if not self.bot.config.get("enable_plugins"):
            embed = discord.Embed(
                description="No hay plugins cargados debido a `ENABLE_PLUGINS=false`, "
                "para rehabilitar los plugins, pon `ENABLE_PLUGINS=true` y reincia al BOT.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        if not self._ready_event.is_set():
            embed = discord.Embed(
                description="Los plugins todavía están cargando, por favor intenta de nuevo más tarde.",
                color=self.bot.main_color,
            )
            return await ctx.send(embed=embed)

        if not self.loaded_plugins:
            embed = discord.Embed(
                description="No hay plugins cargados.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        loaded_plugins = map(str, sorted(self.loaded_plugins))
        pages = ["```\n"]
        for plugin in loaded_plugins:
            msg = str(plugin) + "\n"
            if len(msg) + len(pages[-1]) + 3 <= 2048:
                pages[-1] += msg
            else:
                pages[-1] += "```"
                pages.append(f"```\n{msg}")

        if pages[-1][-3:] != "```":
            pages[-1] += "```"

        embeds = []
        for page in pages:
            embed = discord.Embed(
                title="Plugins cargados:", description=page, color=self.bot.main_color
            )
            embeds.append(embed)
        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()

    @plugins.group(invoke_without_command=True, name="registry", aliases=["list", "info"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_registry(self, ctx, *, plugin_name: typing.Union[int, str] = None):
        """
        Shows a list of all approved plugins.

        Usage:
        `{prefix}plugin registry` Details about all plugins.
        `{prefix}plugin registry plugin-name` Details about the indicated plugin.
        `{prefix}plugin registry page-number` Jump to a page in the registry.
        """

        await self.populate_registry()

        embeds = []

        registry = sorted(self.registry.items(), key=lambda elem: elem[0])

        if isinstance(plugin_name, int):
            index = plugin_name - 1
            if index < 0:
                index = 0
            if index >= len(registry):
                index = len(registry) - 1
        else:
            index = next((i for i, (n, _) in enumerate(registry) if plugin_name == n), 0)

        if not index and plugin_name is not None:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f'No se pudo encontrar un plugin con el nombre "{plugin_name}" dentro del registro.',
            )

            matches = get_close_matches(plugin_name, self.registry.keys())

            if matches:
                embed.add_field(
                    name="Quizás quisiste decir:", value="\n".join(f"`{m}`" for m in matches)
                )

            return await ctx.send(embed=embed)

        for name, details in registry:
            details = self.registry[name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            plugin = Plugin(user, repo, name, branch)

            embed = discord.Embed(
                color=self.bot.main_color,
                description=details["description"],
                url=plugin.link,
                title=details["repository"],
            )

            embed.add_field(
                name="Installation", value=f"```{self.bot.prefix}plugins add {name}```"
            )

            embed.set_author(
                name=details["title"], icon_url=details.get("icon_url"), url=plugin.link
            )

            if details.get("thumbnail_url"):
                embed.set_thumbnail(url=details.get("thumbnail_url"))

            if details.get("image_url"):
                embed.set_image(url=details.get("image_url"))

            if plugin in self.loaded_plugins:
                embed.set_footer(text="Este plugin ya está cargado.")
            else:
                required_version = details.get("bot_version", False)
                if required_version and self.bot.version < parse_version(required_version):
                    embed.set_footer(
                        text="El BOT no puede cargar este plugin, "
                        f"la versión mínima requerida es v{required_version}."
                    )
                else:
                    embed.set_footer(text="El BOT es capaz de instalar este plugin.")

            embeds.append(embed)

        paginator = EmbedPaginatorSession(ctx, *embeds)
        paginator.current = index
        await paginator.run()

    @plugins_registry.command(name="compact", aliases=["slim"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_registry_compact(self, ctx):
        """
        Shows a compact view of all plugins within the registry.
        """

        await self.populate_registry()

        registry = sorted(self.registry.items(), key=lambda elem: elem[0])

        pages = [""]

        for plugin_name, details in registry:
            details = self.registry[plugin_name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            plugin = Plugin(user, repo, plugin_name, branch)

            desc = discord.utils.escape_markdown(details["description"].replace("\n", ""))

            name = f"[`{plugin.name}`]({plugin.link})"
            fmt = f"{name} - {desc}"

            if plugin_name in self.loaded_plugins:
                limit = 75 - len(plugin_name) - 4 - 8 + len(name)
                if limit < 0:
                    fmt = plugin.name
                    limit = 75
                fmt = truncate(fmt, limit) + "[cargado]\n"
            else:
                limit = 75 - len(plugin_name) - 4 + len(name)
                if limit < 0:
                    fmt = plugin.name
                    limit = 75
                fmt = truncate(fmt, limit) + "\n"

            if len(fmt) + len(pages[-1]) <= 2048:
                pages[-1] += fmt
            else:
                pages.append(fmt)

        embeds = []

        for page in pages:
            embed = discord.Embed(color=self.bot.main_color, description=page)
            embed.set_author(name="Registro de Plugins", icon_url=self.bot.user.avatar_url)
            embeds.append(embed)

        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()


def setup(bot):
    bot.add_cog(Plugins(bot))
