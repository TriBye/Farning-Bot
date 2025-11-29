"""Discord-Bot fuer Moderation und Kursverwaltung."""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta
from typing import Iterable, List, Optional, Sequence

from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands

load_dotenv()


def _env_int(name: str, fallback: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return fallback
    try:
        return int(raw_value)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise SystemExit(f"Umgebungsvariable {name} muss eine Ganzzahl sein.") from exc


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Umgebungsvariable {name} fehlt.")
    return value


# ---------------------------------------------------------------------------
# Konfiguration (alle IDs/Keys hier pflegen)
# ---------------------------------------------------------------------------
TOKEN: str = _require_env("DISCORD_TOKEN")
GUILD_ID: int = _env_int("DISCORD_GUILD_ID", 1436158090981675091)
KURS_CATEGORY_ID: int = _env_int("DISCORD_KURS_CATEGORY_ID", 1436199620081356854)
MODERATOR_ROLE_ID: int = _env_int("DISCORD_MODERATOR_ROLE_ID", 1436194208892325888)
TEACHER_ROLE_ID: int = _env_int("DISCORD_TEACHER_ROLE_ID", 1436197056992645120)

# Maximale Timeout-Dauer laut Discord (28 Tage).
MAX_TIMEOUT_SECONDS = 60 * 60 * 24 * 28
# Datei-Uploads koennen je nach Server-Limit variieren – 25 MB ist Standard fuer Nitro-lose Server.
MAX_FILE_UPLOAD_BYTES = 25 * 1024 * 1024

# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("farning_bot")

TIME_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MENTION_PATTERN = re.compile(r"<@!?(\d+)>")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True


class FarningBot(commands.Bot):
    """Bot mit eigenem setup_hook zum Synchronisieren der Slash-Befehle."""

    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:  # noqa: D401
        """Sync der Slash-Commands sobald der Bot startet."""
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            logger.info("Slash-Befehle fuer Gilde %s synchronisiert", GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Slash-Befehle global synchronisiert (keine GUILD_ID gesetzt)")


bot = FarningBot()


def _member_has_role(member: discord.Member, role_id: int) -> bool:
    return bool(role_id) and any(role.id == role_id for role in getattr(member, "roles", []))


def _member_has_any_role(member: discord.Member, role_ids: Iterable[int]) -> bool:
    return any(_member_has_role(member, rid) for rid in role_ids if rid)


def _format_reason(interaction: discord.Interaction, reason: Optional[str]) -> str:
    base = f"Aktion von {interaction.user} ({interaction.user.id})"
    return f"{base}: {reason}" if reason else base


def _normalize_course_name(name: str) -> str:
    return " ".join(name.strip().split())


def _slugify(name: str) -> str:
    slug = name.strip().lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "-", slug)[:90] or "kurs"


async def _fetch_course_role(guild: discord.Guild, kurs_name: str) -> Optional[discord.Role]:
    name = _normalize_course_name(kurs_name).casefold()
    return discord.utils.find(lambda r: r.name.casefold() == name, guild.roles)  # type: ignore[arg-type]


def _require_moderator():
    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        if isinstance(user, discord.Member):
            perms = user.guild_permissions
            if perms.administrator or perms.manage_guild or perms.moderate_members:
                return True
            if _member_has_role(user, MODERATOR_ROLE_ID):
                return True
        raise app_commands.CheckFailure("Dir fehlen die Moderationsrechte.")

    return app_commands.check(predicate)


def _require_course_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        if isinstance(user, discord.Member):
            perms = user.guild_permissions
            if perms.administrator or perms.manage_guild:
                return True
            if _member_has_any_role(user, (MODERATOR_ROLE_ID, TEACHER_ROLE_ID)):
                return True
        raise app_commands.CheckFailure("Nur Moderator:innen oder Lehrer:innen duerfen das tun.")

    return app_commands.check(predicate)


def _extract_member_ids(raw: str) -> Sequence[int]:
    ids = {int(match.group(1)) for match in MENTION_PATTERN.finditer(raw)}
    for chunk in re.split(r"[,\s]+", raw):
        if chunk.isdigit():
            ids.add(int(chunk))
    return list(ids)


def _parse_embed_color(raw: Optional[str]) -> discord.Color:
    if not raw:
        return discord.Color.blurple()
    cleaned = raw.strip().lower()
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    if len(cleaned) not in (3, 6):
        raise ValueError("Hexfarbe muss 3 oder 6 Zeichen lang sein.")
    if len(cleaned) == 3:
        cleaned = "".join(ch * 2 for ch in cleaned)
    if not re.fullmatch(r"[0-9a-f]{6}", cleaned):
        raise ValueError("Hexfarbe enthaelt ungueltige Zeichen.")
    return discord.Color(int(cleaned, 16))


async def _resolve_members(guild: discord.Guild, members_input: str) -> List[discord.Member]:
    ids = _extract_member_ids(members_input)
    resolved: List[discord.Member] = []
    for member_id in ids:
        member = guild.get_member(member_id)
        if member is None:
            try:
                member = await guild.fetch_member(member_id)
            except discord.NotFound:
                logger.warning("Mitglied %s nicht gefunden", member_id)
                continue
        resolved.append(member)
    return resolved


def _parse_duration(duration: str) -> Optional[int]:
    cleaned = duration.strip().lower()
    match = re.fullmatch(r"(\d+)([smhd])?", cleaned)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2) or "m"
    return value * TIME_MULTIPLIERS[unit]


async def _get_course_category(guild: discord.Guild) -> discord.CategoryChannel:
    if not KURS_CATEGORY_ID:
        raise RuntimeError("Bitte KURS_CATEGORY_ID konfigurieren.")
    category = guild.get_channel(KURS_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("Die angegebene KURS_CATEGORY_ID ist keine Kategorie.")
    return category


def _get_kurs_logs_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Suche nach dem kurs-logs Kanal (case-insensitiv)."""
    return discord.utils.find(
        lambda ch: isinstance(ch, discord.TextChannel) and ch.name.casefold() == "kurs-logs",
        guild.text_channels,
    )


class RegisterModal(discord.ui.Modal):
    def __init__(self, log_channel: discord.TextChannel) -> None:
        super().__init__(title="Kurs registrieren")
        self.log_channel = log_channel
        self.wochentag: discord.ui.TextInput[str] = discord.ui.TextInput(
            label="Wochentag",
            placeholder="Gebe den Wochentag deines Kurses an",
            required=True,
        )
        self.zeit: discord.ui.TextInput[str] = discord.ui.TextInput(
            label="Zeit",
            placeholder="Gebe die Zeit deines Kurses an",
            required=True,
        )
        self.add_item(self.wochentag)
        self.add_item(self.zeit)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # noqa: D401
        """Loggt die Eingaben in #kurs-logs."""
        content = (
            f"Neue Kurs-Registrierung von {interaction.user.mention} ({interaction.user.id})\n"
            f"Wochentag: {self.wochentag.value}\n"
            f"Zeit: {self.zeit.value}"
        )
        try:
            await self.log_channel.send(content)
            await interaction.response.send_message("Danke! Deine Angaben wurden gespeichert.", ephemeral=True)
        except (discord.HTTPException, discord.Forbidden) as exc:
            logger.exception("Kurs-Registrierung konnte nicht geloggt werden: %s", exc)
            await interaction.response.send_message(
                "Fehler beim Speichern deiner Angaben. Bitte versuche es spaeter erneut.",
                ephemeral=True,
            )


@bot.event
async def on_ready() -> None:
    logger.info("Bot eingeloggt als %s (ID %s)", bot.user, bot.user.id if bot.user else "?")


@bot.tree.command(name="register", description="Registriert Kursdaten im Kurs-Log.")
async def register(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Bitte benutze diesen Befehl auf dem Server.", ephemeral=True)
        return

    log_channel = _get_kurs_logs_channel(guild)
    if not log_channel:
        await interaction.response.send_message("Kanal #kurs-logs wurde nicht gefunden.", ephemeral=True)
        return

    await interaction.response.send_modal(RegisterModal(log_channel))


@bot.tree.command(name="timeout", description="Setzt ein Mitglied fuer eine Zeitspanne auf Timeout.")
@_require_moderator()
@app_commands.describe(
    member="Mitglied, das stummgeschaltet werden soll",
    dauer="Dauer z.B. 30m, 2h oder 7d (Standard: Minuten)",
    reason="Optionaler Grund fuer das Timeout",
)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    dauer: str,
    reason: Optional[str] = None,
) -> None:
    seconds = _parse_duration(dauer)
    if not seconds:
        await interaction.response.send_message(
            "Bitte verwende ein gueltiges Format (z.B. 30m, 2h, 1d).",
            ephemeral=True,
        )
        return
    if seconds > MAX_TIMEOUT_SECONDS:
        await interaction.response.send_message(
            "Timeouts sind maximal 28 Tage lang moeglich.",
            ephemeral=True,
        )
        return

    try:
        await member.timeout(duration=timedelta(seconds=seconds), reason=_format_reason(interaction, reason))
    except discord.Forbidden:
        await interaction.response.send_message("Ich habe keine Berechtigung fuer dieses Mitglied.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"{member.mention} wurde fuer {dauer} stummgeschaltet.",
        ephemeral=True,
    )


@bot.tree.command(name="kick", description="Wirft ein Mitglied vom Server.")
@_require_moderator()
@app_commands.describe(member="Mitglied, das gekickt werden soll", reason="Optionaler Grund")
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: Optional[str] = None,
) -> None:
    try:
        await member.kick(reason=_format_reason(interaction, reason))
    except discord.Forbidden:
        await interaction.response.send_message("Kick fehlgeschlagen - fehlen Berechtigungen?", ephemeral=True)
        return
    await interaction.response.send_message(f"{member} wurde gekickt.", ephemeral=True)


@bot.tree.command(name="ban", description="Bannt ein Mitglied vom Server.")
@_require_moderator()
@app_commands.describe(member="Mitglied, das gebannt werden soll", reason="Optionaler Grund")
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: Optional[str] = None,
) -> None:
    try:
        await interaction.guild.ban(member, reason=_format_reason(interaction, reason))  # type: ignore[union-attr]
    except discord.Forbidden:
        await interaction.response.send_message("Ban fehlgeschlagen - fehlen Berechtigungen?", ephemeral=True)
        return
    await interaction.response.send_message(f"{member} wurde gebannt.", ephemeral=True)


@bot.tree.command(name="clear-chat", description="Loescht eine Anzahl Nachrichten in diesem Kanal.")
@_require_moderator()
@app_commands.describe(anzahl="Anzahl Nachrichten (max. 200)", reason="Optionaler Grund")
async def clear_chat(
    interaction: discord.Interaction,
    anzahl: app_commands.Range[int, 1, 200],
    reason: Optional[str] = None,
) -> None:
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("Dieser Befehl funktioniert nur in Textkanaelen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await channel.purge(limit=anzahl, reason=_format_reason(interaction, reason))
    await interaction.followup.send(f"{len(deleted)} Nachrichten geloescht.", ephemeral=True)


@bot.tree.command(name="create-kurs", description="Erstellt einen Kurs-Kanal plus Rolle.")
@_require_course_staff()
@app_commands.describe(name="Name des Kurses", reason="Optionaler Grund fuer die Erstellung")
async def create_kurs(
    interaction: discord.Interaction,
    name: str,
    reason: Optional[str] = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Dieser Befehl funktioniert nur auf Servern.", ephemeral=True)
        return

    kurs_name = _normalize_course_name(name)
    if not kurs_name:
        await interaction.response.send_message("Bitte gib einen Kursnamen an.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    existing_role = await _fetch_course_role(guild, kurs_name)
    if existing_role:
        await interaction.followup.send("Es existiert bereits eine Kursrolle mit diesem Namen.", ephemeral=True)
        return

    try:
        category = await _get_course_category(guild)
    except RuntimeError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

    kurs_role = await guild.create_role(
        name=kurs_name,
        mentionable=True,
        reason=_format_reason(interaction, reason),
    )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        kurs_role: discord.PermissionOverwrite(view_channel=True),
    }
    mod_role = guild.get_role(MODERATOR_ROLE_ID)
    teacher_role = guild.get_role(TEACHER_ROLE_ID)
    if mod_role:
        overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, manage_messages=True)
    if teacher_role:
        overwrites[teacher_role] = discord.PermissionOverwrite(view_channel=True, manage_messages=True)

    channel = await guild.create_text_channel(
        name=_slugify(kurs_name),
        category=category,
        overwrites=overwrites,
        reason=_format_reason(interaction, reason),
    )

    await interaction.followup.send(
        f"Kurs **{kurs_name}** erstellt.\nRolle: {kurs_role.mention}\nKanal: {channel.mention}",
        ephemeral=True,
    )


@bot.tree.command(name="add-member", description="Fuegt Mitglieder zu einem Kurs hinzu.")
@_require_course_staff()
@app_commands.describe(
    kurs_name="Exakter Rollenname des Kurses",
    members="Mitglieder als Erwaehnungen oder IDs (mehrere mit Leerzeichen/Komma trennen)",
    reason="Optionaler Grund",
)
async def add_member(
    interaction: discord.Interaction,
    kurs_name: str,
    members: str,
    reason: Optional[str] = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Dieser Befehl funktioniert nur auf Servern.", ephemeral=True)
        return

    kurs_role = await _fetch_course_role(guild, kurs_name)
    if not kurs_role:
        await interaction.response.send_message("Keine Kursrolle mit diesem Namen gefunden.", ephemeral=True)
        return

    resolved = await _resolve_members(guild, members)
    if not resolved:
        await interaction.response.send_message("Ich konnte keine Mitglieder finden.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    updated = []
    for member in resolved:
        if kurs_role in member.roles:
            continue
        try:
            await member.add_roles(kurs_role, reason=_format_reason(interaction, reason))
            updated.append(member.display_name)
        except discord.Forbidden:
            logger.warning("Rolle konnte nicht an %s vergeben werden (fehlende Berechtigung)", member)

    if updated:
        await interaction.followup.send(
            f"{len(updated)} Mitglied(er) hinzugefuegt: {', '.join(updated)}",
            ephemeral=True,
        )
    else:
        await interaction.followup.send("Niemand wurde hinzugefuegt (hatten evtl. schon die Rolle).", ephemeral=True)


@bot.tree.command(name="remove-member", description="Entfernt Mitglieder aus einem Kurs.")
@_require_course_staff()
@app_commands.describe(
    kurs_name="Exakter Rollenname des Kurses",
    members="Mitglieder als Erwaehnungen oder IDs",
    reason="Optionaler Grund",
)
async def remove_member(
    interaction: discord.Interaction,
    kurs_name: str,
    members: str,
    reason: Optional[str] = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Dieser Befehl funktioniert nur auf Servern.", ephemeral=True)
        return

    kurs_role = await _fetch_course_role(guild, kurs_name)
    if not kurs_role:
        await interaction.response.send_message("Keine Kursrolle mit diesem Namen gefunden.", ephemeral=True)
        return

    resolved = await _resolve_members(guild, members)
    if not resolved:
        await interaction.response.send_message("Ich konnte keine Mitglieder finden.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    removed = []
    for member in resolved:
        if kurs_role not in member.roles:
            continue
        try:
            await member.remove_roles(kurs_role, reason=_format_reason(interaction, reason))
            removed.append(member.display_name)
        except discord.Forbidden:
            logger.warning("Rolle konnte nicht von %s entfernt werden (fehlende Berechtigung)", member)

    if removed:
        await interaction.followup.send(
            f"{len(removed)} Mitglied(er) entfernt: {', '.join(removed)}",
            ephemeral=True,
        )
    else:
        await interaction.followup.send("Niemand wurde entfernt (hatte evtl. die Rolle nicht).", ephemeral=True)


@bot.tree.command(name="upload-file", description="Laesst den Bot eine Datei erneut posten.")
@app_commands.describe(
    attachment="Datei, die erneut gepostet werden soll",
    message="Optionaler Begleittext",
)
async def upload_file(
    interaction: discord.Interaction,
    attachment: discord.Attachment,
    message: Optional[str] = None,
) -> None:
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("Ich kann in diesem Kontext nichts senden.", ephemeral=True)
        return

    if attachment.size and attachment.size > MAX_FILE_UPLOAD_BYTES:
        limit_mb = MAX_FILE_UPLOAD_BYTES // (1024 * 1024)
        await interaction.response.send_message(
            f"Dateien duerfen maximal {limit_mb} MB gross sein.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        discord_file = await attachment.to_file(spoiler=attachment.is_spoiler())
        content = (message or "").strip() or None
        await channel.send(content=content, file=discord_file)
    except (discord.HTTPException, discord.Forbidden) as exc:
        logger.exception("Datei konnte nicht hochgeladen werden: %s", exc)
        await interaction.followup.send("Die Datei konnte nicht gesendet werden.", ephemeral=True)
        return

    await interaction.followup.send("Datei gepostet.", ephemeral=True)


@bot.tree.command(name="echo", description="Sendet eine Nachricht ueber den Bot.")
@app_commands.describe(
    text="Nachricht, die ausgegeben werden soll",
    allow_mentions="True, wenn Erwaehnungen erlaubt sein sollen",
)
async def echo(
    interaction: discord.Interaction,
    text: str,
    allow_mentions: bool = False,
) -> None:
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("Ich kann in diesem Kontext nichts senden.", ephemeral=True)
        return

    cleaned = text.strip()
    if not cleaned:
        await interaction.response.send_message("Bitte gib eine Nachricht an.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    allowed_mentions = discord.AllowedMentions.all() if allow_mentions else discord.AllowedMentions.none()
    try:
        await channel.send(cleaned, allowed_mentions=allowed_mentions)
    except (discord.HTTPException, discord.Forbidden) as exc:
        logger.exception("Echo fehlgeschlagen: %s", exc)
        await interaction.followup.send("Die Nachricht konnte nicht gesendet werden.", ephemeral=True)
        return

    await interaction.followup.send("Nachricht gesendet.", ephemeral=True)


@bot.tree.command(name="create-embed", description="Erstellt ein einfaches Embed ueber den Bot.")
@app_commands.describe(
    title="Titel der Nachricht",
    description="Beschreibung / Inhalt",
    color_hex="Hexfarbe (z.B. #ff8800)",
    footer="Optionaler Footer",
)
async def create_embed(
    interaction: discord.Interaction,
    title: str,
    description: str,
    color_hex: Optional[str] = None,
    footer: Optional[str] = None,
) -> None:
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("Ich kann in diesem Kontext nichts senden.", ephemeral=True)
        return

    clean_title = title.strip()
    clean_description = description.strip()
    if not clean_title or not clean_description:
        await interaction.response.send_message("Titel und Beschreibung duerfen nicht leer sein.", ephemeral=True)
        return
    if len(clean_title) > 256:
        await interaction.response.send_message("Der Titel darf maximal 256 Zeichen lang sein.", ephemeral=True)
        return
    if len(clean_description) > 4096:
        await interaction.response.send_message("Die Beschreibung darf maximal 4096 Zeichen lang sein.", ephemeral=True)
        return

    try:
        color = _parse_embed_color(color_hex)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    embed = discord.Embed(title=clean_title, description=clean_description, color=color)
    embed.timestamp = discord.utils.utcnow()
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    if footer and footer.strip():
        embed.set_footer(text=footer.strip())

    await interaction.response.defer(ephemeral=True)
    try:
        await channel.send(embed=embed)
    except (discord.HTTPException, discord.Forbidden) as exc:
        logger.exception("Embed konnte nicht gesendet werden: %s", exc)
        await interaction.followup.send("Das Embed konnte nicht gesendet werden.", ephemeral=True)
        return

    await interaction.followup.send("Embed gesendet.", ephemeral=True)


def main() -> None:
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
