"""Insomnia - sits in a Discord voice channel 24/7. Two commands: !insomniajoin, !insomnialeave."""
import asyncio
import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands, tasks

log = logging.getLogger("insomnia")

TOKEN = os.environ.get("DISCORD_TOKEN")
PREFIX = os.environ.get("PREFIX", "!")
# Permission that makes someone a "moderator" (admins always qualify).
MOD_PERMISSION = os.environ.get("MOD_PERMISSION", "move_members")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
WATCHDOG_SECONDS = 15


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.exception("Could not read %s, starting empty", STATE_FILE)
        return {}


def save_state(state: dict) -> None:
    # Atomic write so a crash or power loss can't leave a half-written file.
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


class Insomnia(commands.Bot):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.message_content = True  # privileged: enable in the developer portal
        super().__init__(
            command_prefix=PREFIX,
            intents=intents,
            help_command=None,
            max_messages=None,  # no message cache, saves RAM
        )
        # {guild_id: {"voice": channel_id, "text": channel_id}}
        self.state = load_state()
        self.pending_announce = set(self.state)  # guilds owed a "I'm back" message
        self.misses = {}  # guild_id -> consecutive unhealthy checks
        self.lock = asyncio.Lock()

    async def setup_hook(self):
        self.watchdog.start()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    # ---- state helpers -------------------------------------------------

    def forget(self, guild_id: int) -> bool:
        had = self.state.pop(str(guild_id), None) is not None
        self.pending_announce.discard(str(guild_id))
        self.misses.pop(guild_id, None)
        if had:
            save_state(self.state)
        return had

    # ---- voice ---------------------------------------------------------

    async def connect_to(self, guild: discord.Guild, channel: discord.abc.Connectable):
        vc = guild.voice_client
        if vc is not None:
            if vc.is_connected():
                if vc.channel.id != channel.id:
                    await vc.move_to(channel)
                return
            await vc.disconnect(force=True)
        await channel.connect(self_deaf=True)

    async def ensure_connected(self, guild_id: int) -> None:
        async with self.lock:
            entry = self.state.get(str(guild_id))
            if not entry:
                return
            guild = self.get_guild(guild_id)
            if guild is None:
                return  # guild unavailable right now, try again next tick
            channel = guild.get_channel(entry["voice"])
            if channel is None:
                log.warning("Saved channel %s no longer exists, dropping it", entry["voice"])
                self.forget(guild_id)
                return

            vc = guild.voice_client
            if vc is not None and vc.is_connected() and vc.channel.id == channel.id:
                self.misses.pop(guild_id, None)
                return

            # If the library is mid-reconnect, give it a couple of ticks first.
            n = self.misses[guild_id] = self.misses.get(guild_id, 0) + 1
            if vc is not None and n < 2:
                return

            log.info("Rejoining %s in guild %s", channel.name, guild.id)
            await self.connect_to(guild, channel)
            self.misses.pop(guild_id, None)

        await self.announce_back(guild)

    async def announce_back(self, guild: discord.Guild) -> None:
        key = str(guild.id)
        if key not in self.pending_announce:
            return
        self.pending_announce.discard(key)
        text_id = self.state.get(key, {}).get("text")
        channel = guild.get_channel_or_thread(text_id) if text_id else None
        if channel is None:
            return
        try:
            await channel.send("🌙 Insomnia is back and in the voice channel again.")
        except discord.HTTPException:
            log.warning("Could not send the 'back' message in %s", text_id)

    @tasks.loop(seconds=WATCHDOG_SECONDS)
    async def watchdog(self):
        for gid in list(self.state):
            try:
                await self.ensure_connected(int(gid))
            except Exception:
                log.exception("Rejoin attempt failed for guild %s", gid)

    @watchdog.before_loop
    async def _before_watchdog(self):
        await self.wait_until_ready()

    async def on_voice_state_update(self, member, before, after):
        # If a mod drags the bot to another channel, follow the move.
        if member.id != self.user.id or after.channel is None:
            return
        entry = self.state.get(str(member.guild.id))
        if entry and entry["voice"] != after.channel.id:
            entry["voice"] = after.channel.id
            save_state(self.state)


bot = Insomnia()


def is_mod():
    async def predicate(ctx: commands.Context) -> bool:
        perms = ctx.author.guild_permissions
        return perms.administrator or getattr(perms, MOD_PERMISSION)

    return commands.check(predicate)


@bot.command(name="insomniajoin")
@commands.guild_only()
@is_mod()
async def insomnia_join(ctx: commands.Context):
    voice = ctx.author.voice
    if voice is None or voice.channel is None:
        await ctx.send(f"Join a voice channel first, then run `{PREFIX}insomniajoin`.")
        return
    channel = voice.channel
    async with bot.lock:
        try:
            await bot.connect_to(ctx.guild, channel)
        except Exception:
            log.exception("Join failed")
            await ctx.send("Couldn't join that channel. Check that I have Connect permission there.")
            return
        bot.state[str(ctx.guild.id)] = {"voice": channel.id, "text": ctx.channel.id}
        bot.pending_announce.discard(str(ctx.guild.id))
        bot.misses.pop(ctx.guild.id, None)
        save_state(bot.state)
    await ctx.send(f"🌙 Joined **{channel.name}**. I'll stay here around the clock. Mods can dismiss me with `{PREFIX}insomnialeave`.")


@bot.command(name="insomnialeave")
@commands.guild_only()
@is_mod()
async def insomnia_leave(ctx: commands.Context):
    async with bot.lock:
        had_state = bot.forget(ctx.guild.id)  # forget first so the watchdog can't rejoin
        vc = ctx.guild.voice_client
        if vc is not None:
            await vc.disconnect(force=True)
    if had_state or vc is not None:
        await ctx.send("👋 Left the voice channel. Use `" + PREFIX + "insomniajoin` from a voice channel to bring me back.")
    else:
        await ctx.send("I'm not in a voice channel.")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.CommandNotFound, commands.NoPrivateMessage)):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("Only moderators can use that command.")
        return
    log.error("Command error in %s", ctx.command, exc_info=error)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set")
    if not hasattr(discord.Permissions, MOD_PERMISSION):
        raise SystemExit(f"MOD_PERMISSION {MOD_PERMISSION!r} is not a valid Discord permission")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot.run(TOKEN, log_handler=None)
