#!/usr/bin/env python3
"""
Discord-Bot für die Serversuche.

Pollt Hetzner Serverbörse und Prepaid-Hoster im Hintergrund, schreibt jede
Preisänderung in eine SQLite-Historie und meldet Treffer pro Watchlist in den
Kanal, in dem sie angelegt wurde.

Slash-Commands:
  /suche    Angebote abfragen, blätterbar
  /verlauf  Preisverlauf eines Angebots
  /watch    add | list | remove - Dauerüberwachung für diesen Kanal
  /status   Poll-Zustand und Datenbank
  /poll     Sofortigen Durchlauf auslösen
  /hilfe    Kurzübersicht

Konfiguration über Umgebungsvariablen oder eine .env-Datei neben diesem
Skript (siehe config.example.env).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import discord
    from discord import app_commands
    from discord.ext import commands, tasks
except ImportError:
    sys.exit("discord.py fehlt. Installieren mit:  pip install -r requirements.txt")

import serversuche as ss
from history import Forecast, History, forecast, sparkline

SCRIPT_DIR = Path(__file__).resolve().parent
log = logging.getLogger("serversuche.bot")

# Aus serversuche abgeleitet, damit der Satz nur an einer Stelle steht.
VAT_PCT = round((ss.VAT_FACTOR - 1) * 100)


def with_vat(net: float) -> float:
    return round(net * ss.VAT_FACTOR, 2)


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #


def load_dotenv(path: Path) -> None:
    """Minimaler .env-Leser. Bereits gesetzte Umgebungsvariablen gewinnen."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalise_mention(raw: str) -> str:
    """`123`, `<@123>`, `@here` -> sendefertige Erwähnung. Leer bleibt leer.

    Eine nackte Zahl wird als Benutzer-ID gelesen — der Normalfall. Eine Rolle
    muss ausgeschrieben werden (`<@&123>`), weil sich beides sonst nicht
    unterscheiden lässt.
    """
    raw = raw.strip()
    return f"<@{raw}>" if raw.isdigit() else raw


class Config:
    def __init__(self) -> None:
        load_dotenv(SCRIPT_DIR / ".env")
        self.token = os.environ.get("DISCORD_TOKEN", "")
        guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
        self.guild_id = int(guild) if guild.isdigit() else None
        self.poll_minutes = max(1, int(os.environ.get("POLL_INTERVAL_MINUTES", "5")))
        self.db_path = Path(os.environ.get("DB_PATH", SCRIPT_DIR / "serversuche.db"))
        self.specs_path = Path(os.environ.get("SPECS_PATH", SCRIPT_DIR / "server_specs.json"))
        self.gross = os.environ.get("PRICE_MODE", "net").lower() == "gross"
        self.include_ip = os.environ.get("INCLUDE_IP", "0") not in ("0", "", "false", "False")
        self.providers = tuple(
            p.strip() for p in os.environ.get("PROVIDERS", "hetzner,pph").split(",") if p.strip()
        )
        self.drop_pct = float(os.environ.get("ALERT_DROP_PCT", "3"))
        self.drop_abs = float(os.environ.get("ALERT_DROP_ABS", "2"))
        self.prune_days = int(os.environ.get("PRUNE_AFTER_DAYS", "60"))
        self.default_mention = normalise_mention(os.environ.get("DEFAULT_MENTION", ""))

    def validate(self) -> None:
        if not self.token:
            sys.exit("DISCORD_TOKEN ist nicht gesetzt (Umgebungsvariable oder .env).")
        if not self.specs_path.exists():
            sys.exit(f"server_specs.json nicht gefunden: {self.specs_path}")
        for p in self.providers:
            if p not in ss.ALL_PROVIDERS:
                sys.exit(f"Unbekannter Anbieter in PROVIDERS: {p}")
        # Lieber hier abbrechen als monatelang stumm die falsche Zeichenkette
        # verschicken, die Discord dann als Text statt als Ping darstellt.
        if self.default_mention and not self.default_mention.startswith(("<@", "@")):
            sys.exit(f"DEFAULT_MENTION sieht nicht nach einer Erwähnung aus: "
                     f"{self.default_mention!r} — erwartet wird eine Benutzer-ID (123456), "
                     f"<@123456>, <@&123456> für eine Rolle oder @here.")

    def mention_for(self, stored: str | None) -> str | None:
        """Erwähnung eines Watch/Alarms, ersatzweise der Standard aus der .env."""
        return (stored or "").strip() or self.default_mention or None

    @property
    def price_mode_label(self) -> str:
        """Preisart, auf die sich Filter, Zielpreise und Tier-Deckel beziehen."""
        return "brutto" if self.gross else "netto"

    @property
    def price_note(self) -> str:
        return (f"netto & brutto ({VAT_PCT} % USt.) · Filter {self.price_mode_label}"
                + (" · inkl. IPv4" if self.include_ip else ""))


# --------------------------------------------------------------------------- #
# Darstellung
# --------------------------------------------------------------------------- #

PROVIDER_LABEL = {"hetzner": "HZ", "pph": "PPH"}
PROVIDER_NAME = {"hetzner": "Hetzner Serverbörse", "pph": "Prepaid-Hoster"}
TIER_BADGE = {"best": "🥇", "recommended": "🥈", "minimum": "🥉"}
TIER_COLOUR = {"best": 0xF1C40F, "recommended": 0x5865F2, "minimum": 0x2ECC71}
NEUTRAL_COLOUR = 0x99AAB5
STORAGE_ICON = {"nvme": "⚡", "ssd": "💾", "hdd": "🐌", "unknown": "❔"}
STORAGE_LABEL = ss.STORAGE_LABELS  # eine Quelle, damit CLI und Bot gleich benennen
LOCATION_NAME = {"FSN": "Falkenstein", "HEL": "Helsinki", "NBG": "Nürnberg"}


def fmt_eur(value: float) -> str:
    """1234.5 -> '1.234,50 €' (deutsches Zahlenformat)."""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    thousands = f"{int(whole):,}".replace(",", ".")
    return f"{'-' if value < 0 else ''}{thousands},{frac} €"


def fmt_num(value: float, decimals: int = 1) -> str:
    """Dezimalzahl mit Komma statt Punkt."""
    return f"{value:.{decimals}f}".replace(".", ",")


def fmt_size(gb: int) -> str:
    if gb >= 1000:
        return f"{gb / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " TB"
    return f"{gb} GB"


def location_name(datacenter: str) -> str:
    """'FSN1-DC12' -> 'Falkenstein'. Unbekanntes bleibt, wie es ist."""
    prefix = "".join(c for c in datacenter[:3] if c.isalpha()).upper()
    return LOCATION_NAME.get(prefix, datacenter)


def score_bar(score: float, width: int = 10) -> str:
    filled = max(0, min(width, round(score / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def price_line(offer: ss.Offer, price: float, cfg: Config) -> str:
    """Der angezeigte Preis und daneben die jeweils andere Preisart.

    Beide APIs liefern netto. Wer den Server privat bezahlt, interessiert sich
    aber für den Betrag inklusive USt. — also stehen beide da, statt dass man
    im Kopf rechnet oder PRICE_MODE umstellt.
    """
    other = offer.price(not cfg.gross, cfg.include_ip)
    return f"**{fmt_eur(price)}**/Monat · {fmt_eur(other)} {'netto' if cfg.gross else 'brutto'}"


def vat_counterpart(value: float, cfg: Config) -> str:
    """Die andere Preisart zu einem frei eingegebenen Betrag, z. B. einem Zielpreis.

    Anders als bei einem Angebot gibt es hier keinen Nettowert aus der API —
    es bleibt nur, mit dem Steuersatz zu rechnen.
    """
    other = round(value / ss.VAT_FACTOR, 2) if cfg.gross else with_vat(value)
    return f"{fmt_eur(other)} {'netto' if cfg.gross else 'brutto'}"


def offer_card(offer: ss.Offer, price: float, cfg: Config) -> str:
    """Ein Angebot als kompakte, auf dem Handy lesbare Karte.

    Bewusst keine Monospace-Tabelle: die bricht auf schmalen Displays um und
    wird dann unlesbar.
    """
    badge = TIER_BADGE.get(offer.tier or "", "▫️")
    cpu = offer.cpu["name"] if offer.cpu else offer.cpu_raw
    icon = STORAGE_ICON.get(offer.storage_class, "❔")

    facts = [
        price_line(offer, price, cfg),
        f"{offer.ram_gb} GB RAM",
        f"{fmt_size(offer.storage_gb)} {icon} {STORAGE_LABEL.get(offer.storage_class, '?')}",
        f"📍 {location_name(offer.datacenter)}",
    ]
    if offer.setup_net > 0:
        setup = offer.setup_net if not cfg.gross else with_vat(offer.setup_net)
        other = with_vat(offer.setup_net) if not cfg.gross else offer.setup_net
        facts.append(f"⚠️ {fmt_eur(setup)} Setup ({fmt_eur(other)} "
                     f"{'netto' if cfg.gross else 'brutto'})")
    if offer.reduce_next_ts:
        facts.append(f"⏳ <t:{offer.reduce_next_ts}:R>")

    detail = [f"`{score_bar(offer.score)}` {offer.score:.0f}"]
    if offer.cpu:
        c = offer.cpu
        detail += [f"{c['cores']}C/{c['threads']}T", f"{c['boost_ghz']} GHz", f"{c['l3_mb']} MB L3"]

    return (
        f"{badge} **{cpu}** — [`{PROVIDER_LABEL.get(offer.provider, offer.provider)} "
        f"{offer.offer_id}`]({offer.url})\n"
        f"> {' · '.join(facts)}\n"
        f"> {' · '.join(detail)}"
    )


def change_card(offer: ss.Offer, old: float, new: float, cfg: Config) -> str:
    cpu = offer.cpu["name"] if offer.cpu else offer.cpu_raw
    pct = (old - new) / old * 100 if old else 0.0
    other = offer.price(not cfg.gross, cfg.include_ip)
    return (
        f"📉 **{cpu}** — [`{PROVIDER_LABEL.get(offer.provider, offer.provider)} "
        f"{offer.offer_id}`]({offer.url})\n"
        f"> ~~{fmt_eur(old)}~~ → **{fmt_eur(new)}** ({fmt_eur(other)} "
        f"{'netto' if cfg.gross else 'brutto'})  ·  −{fmt_eur(old - new)} (−{fmt_num(pct)} %)"
    )


FIELD_LIMIT = 1024  # Discord: Höchstlänge eines Embed-Feldwerts


def block_within_limit(cards: list[str], total: int, max_chars: int = FIELD_LIMIT) -> str:
    """Setzt Karten aneinander, bis das Embed-Feldlimit erreicht ist.

    Nur die Anzahl zu deckeln genügt nicht: eine Karte ist je nach Setup-Gebühr
    und Countdown zwischen gut 200 und knapp 290 Zeichen lang, vier davon
    sprengen die 1024. Discord antwortet dann mit HTTP 400 — und die Meldung
    wäre verloren, weil der Trefferstand zu dem Zeitpunkt schon geschrieben ist.
    """
    reserve = len("\n\n*… und 9999 weitere*")
    out: list[str] = []
    used = 0
    for card in cards:
        need = len(card) + (2 if out else 0)
        # Die erste Karte kommt immer rein - ein überlanges Feld ist immer noch
        # besser als ein leeres, und einzeln bleibt jede Karte weit unter 1024.
        if out and used + need > max_chars - reserve:
            break
        out.append(card)
        used += need

    body = "\n\n".join(out)
    rest = total - len(out)
    if rest > 0:
        body += f"\n\n*… und {rest} {'weiteres' if rest == 1 else 'weitere'}*"
    return body


def cards_block(pairs: list[tuple[ss.Offer, float]], limit: int, cfg: Config) -> str:
    return block_within_limit([offer_card(o, p, cfg) for o, p in pairs[:limit]], len(pairs))


def forecast_line(fc: Forecast, target: float | None = None) -> str:
    """Eine Zeile Prognose, ehrlich über ihre eigene Unsicherheit."""
    if fc.points < 3 or fc.rate_per_day == 0.0:
        return f"🔮 {fc.note}"

    trend = f"📉 **{fmt_eur(abs(fc.rate_per_day))}/Tag** günstiger"
    if fc.rate_per_day > 0:
        trend = f"📈 {fmt_eur(fc.rate_per_day)}/Tag teurer"

    if target is None or fc.eta_ts is None:
        return f"{trend} · {fc.note}"
    if fc.note == "Ziel bereits erreicht":
        return f"🎯 Ziel {fmt_eur(target)} bereits erreicht"

    marker = "" if fc.reliable else " *(grob)*"
    return f"{trend} · 🎯 {fmt_eur(target)} etwa <t:{fc.eta_ts}:R>{marker}"


def tier_colour(offers: list[ss.Offer]) -> int:
    for tier in ("best", "recommended", "minimum"):
        if any(o.tier == tier for o in offers):
            return TIER_COLOUR[tier]
    return NEUTRAL_COLOUR


def ping_summary(cfg: Config, stored: str | None) -> str:
    """Wer bei einem Treffer tatsächlich gepingt wird — inklusive Standard."""
    ping = cfg.mention_for(stored)
    if ping is None:
        return "niemand (weder `erwähnen` gesetzt noch `DEFAULT_MENTION` in der .env)"
    return f"{ping}" + ("" if stored else " *(Standard aus der .env)*")


def filter_summary(filt: dict[str, Any], specs: ss.Specs) -> str:
    text = ss.describe_filter(filt, specs, show_all=False).replace("Filter (CLI): ", "")
    return text if text != "keiner" else "ohne Einschränkung"


# --------------------------------------------------------------------------- #
# Filter
# --------------------------------------------------------------------------- #


def build_filter(specs: ss.Specs, tier: str | None, max_preis: float | None,
                 min_ram: int | None, storage: str | None, min_sts: int | None,
                 anbieter: str | None, setup_gebühr_ok: bool) -> dict[str, Any]:
    filt: dict[str, Any] = {"_source": "CLI"}
    if tier:
        filt["min_tier_rank"] = specs.tiers[tier]["rank"]
    if max_preis is not None:
        filt["max_price"] = max_preis
    if min_ram is not None:
        filt["min_ram"] = min_ram
    if storage:
        filt["min_storage_class"] = storage
    if min_sts is not None:
        filt["min_sts"] = min_sts
    if anbieter and anbieter != "all":
        filt["provider"] = anbieter
    if not setup_gebühr_ok:
        filt["no_setup_fee"] = True
    return filt


def apply_filter(offers: list[ss.Offer], filt: dict[str, Any], specs: ss.Specs,
                 cfg: Config) -> list[ss.Offer]:
    """serversuche.filter_offers plus der Bot-eigene Anbieter-Filter."""
    hits = ss.filter_offers(offers, filt, specs, cfg.gross, cfg.include_ip)
    if filt.get("provider"):
        hits = [o for o in hits if o.provider == filt["provider"]]
    return hits


def resolve_offers(bot: "ServerBot", server_id: str) -> list[ss.Offer]:
    """Findet Angebote zu '3062623' oder 'hetzner:3062623' im aktuellen Bestand."""
    sid = server_id.strip().lower()
    return [o for o in bot.offers if o.key.lower() == sid or o.offer_id.lower() == sid]


TIER_CHOICES = [
    app_commands.Choice(name="🥉 minimum", value="minimum"),
    app_commands.Choice(name="🥈 recommended", value="recommended"),
    app_commands.Choice(name="🥇 best", value="best"),
]
STORAGE_CHOICES = [
    app_commands.Choice(name="💾 SSD oder besser", value="ssd"),
    app_commands.Choice(name="⚡ nur NVMe", value="nvme"),
]
PROVIDER_CHOICES = [
    app_commands.Choice(name="Beide Anbieter", value="all"),
    app_commands.Choice(name="Hetzner Serverbörse", value="hetzner"),
    app_commands.Choice(name="Prepaid-Hoster", value="pph"),
]

# Welche Filterschlüssel /watch edit über "zurücksetzen" wieder entfernen kann.
RESET_KEYS = {
    "tier": ("min_tier_rank",),
    "max_preis": ("max_price",),
    "min_ram": ("min_ram",),
    "storage": ("min_storage_class",),
    "min_sts": ("min_sts",),
    "anbieter": ("provider",),
}
RESET_CHOICES = [
    app_commands.Choice(name="Tier", value="tier"),
    app_commands.Choice(name="Preisgrenze", value="max_preis"),
    app_commands.Choice(name="RAM-Grenze", value="min_ram"),
    app_commands.Choice(name="Storage-Klasse", value="storage"),
    app_commands.Choice(name="Score-Grenze", value="min_sts"),
    app_commands.Choice(name="Anbieter-Einschränkung", value="anbieter"),
]


# --------------------------------------------------------------------------- #
# Blätter-Ansicht
# --------------------------------------------------------------------------- #


class OfferPager(discord.ui.View):
    """Blätterbare Trefferliste mit Aktualisieren-Knopf.

    Nur wer den Befehl abgesetzt hat, darf blättern - sonst springt die Ansicht
    unter den Händen anderer Leute im Kanal herum.
    """

    def __init__(self, bot: "ServerBot", user_id: int, filt: dict[str, Any],
                 hits: list[ss.Offer], per_page: int = 5):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        self.filt = filt
        self.hits = hits
        self.per_page = per_page
        self.page = 0
        self.total_scanned = len(bot.offers)
        self.message: discord.Message | None = None
        self._sync()

    # -- Zustand ------------------------------------------------------------ #

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.hits) // self.per_page))

    def _sync(self) -> None:
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.custom_id == "prev":
                child.disabled = self.page <= 0
            elif child.custom_id == "next":
                child.disabled = self.page >= self.pages - 1
            elif child.custom_id == "page":
                child.label = f"{self.page + 1}/{self.pages}"

    def embed(self) -> discord.Embed:
        start = self.page * self.per_page
        chunk = self.hits[start:start + self.per_page]
        prices = [o.price(self.bot.cfg.gross, self.bot.cfg.include_ip) for o in chunk]

        embed = discord.Embed(
            title=f"🔍 {len(self.hits)} Treffer von {self.total_scanned} Angeboten",
            description=cards_block(list(zip(chunk, prices)), self.per_page, self.bot.cfg),
            colour=tier_colour(chunk),
        )
        embed.add_field(name="Filter", value=filter_summary(self.filt, self.bot.specs), inline=False)
        age = int(time.time() - self.bot.last_fetch)
        embed.set_footer(text=f"Seite {self.page + 1}/{self.pages} · Preise "
                              f"{self.bot.cfg.price_note} · Daten {age} s alt")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Diese Liste gehört jemand anderem. Setz `/suche` selbst ab, dann kannst du blättern.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # -- Knöpfe ------------------------------------------------------------- #

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary,
                       custom_id="page", disabled=True)
    async def indicator(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        pass  # reine Anzeige

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary, custom_id="next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="🔄", label="Aktualisieren", style=discord.ButtonStyle.primary,
                       custom_id="refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            offers = await self.bot.refresh(force=True)
        except RuntimeError as err:
            await interaction.followup.send(f"⚠️ Abruf fehlgeschlagen: {err}", ephemeral=True)
            return
        self.hits = apply_filter(offers, self.filt, self.bot.specs, self.bot.cfg)
        self.total_scanned = len(offers)
        self.page = min(self.page, self.pages - 1)
        self._sync()
        await interaction.edit_original_response(embed=self.embed(), view=self)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #


class ServerBot(commands.Bot):
    def __init__(self, cfg: Config):
        super().__init__(command_prefix="!ss ", intents=discord.Intents.default())
        self.cfg = cfg
        self.specs = ss.load_specs(cfg.specs_path)
        self.store = History(cfg.db_path)
        self.offers: list[ss.Offer] = []
        self.prices: list[float] = []
        self.last_fetch: float = 0.0
        self.last_error: str | None = None
        self.live_providers: set[str] = set()
        self.last_poll_ok: float = 0.0
        self._lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        register_commands(self)
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands für Guild %s synchronisiert", self.cfg.guild_id)
        else:
            await self.tree.sync()
            log.info("Globale Commands synchronisiert (bis zu 1 h Verzögerung)")

        self.poll_loop.change_interval(minutes=self.cfg.poll_minutes)
        self.poll_loop.start()

    async def on_ready(self) -> None:
        log.info("Angemeldet als %s", self.user)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="Season 2"))

    # -- Datenbeschaffung --------------------------------------------------- #

    async def refresh(self, force: bool = False, max_age: float = 60.0) -> list[ss.Offer]:
        """Holt und bewertet die Angebote, mit kurzem Cache gegen Doppelabrufe."""
        async with self._lock:
            if not force and self.offers and time.time() - self.last_fetch < max_age:
                return self.offers

            errors: list[str] = []
            failed: set[str] = set()

            def note(src: str, err: Exception) -> None:
                failed.add(src)
                errors.append(f"{src}: {err}")

            offers = await asyncio.to_thread(ss.fetch_all, self.cfg.providers, note)
            if not offers:
                self.last_error = "; ".join(errors) or "keine Angebote geliefert"
                raise RuntimeError(self.last_error)

            ss.evaluate_all(offers, self.specs, self.cfg.gross, self.cfg.include_ip)
            self.offers = offers
            self.prices = [o.price(self.cfg.gross, self.cfg.include_ip) for o in offers]
            # Wessen Bestand dieser Abruf vollständig abbildet. Nur für diese
            # Anbieter darf ein fehlendes Angebot "verschwunden" bedeuten.
            self.live_providers = {p for p in self.cfg.providers if p not in failed}
            self.last_fetch = time.time()
            self.last_error = "; ".join(errors) or None
            return offers

    # -- Hintergrund-Poll ---------------------------------------------------- #

    @tasks.loop(minutes=5)
    async def poll_loop(self) -> None:
        # Alles gekapselt: discord.py beendet einen tasks.loop endgültig, wenn
        # eine Ausnahme durchschlägt, die nicht in seiner Reconnect-Liste steht
        # (ein sqlite3.OperationalError etwa). Der Bot bliebe online und würde
        # nie wieder pollen.
        try:
            await self._poll_once()
        except Exception:
            log.exception("Poll abgebrochen, nächster Versuch in %d Min.", self.cfg.poll_minutes)

    async def _poll_once(self) -> None:
        try:
            offers = await self.refresh(force=True)
        except RuntimeError as err:
            log.warning("Poll fehlgeschlagen: %s", err)
            return

        # Preise aus genau dieser Liste, nicht aus self.prices: ein paralleler
        # /suche-Abruf kann self.prices während der folgenden awaits ersetzen.
        prices = [o.price(self.cfg.gross, self.cfg.include_ip) for o in offers]
        live = self.live_providers
        if len(live) < len(self.cfg.providers):
            log.warning("Nur %s haben geantwortet - deren Bestand gilt weiter als vorhanden",
                        ", ".join(sorted(live)) or "keine Anbieter")

        # Die SQLite-Aufrufe laufen bewusst inline: gemessene 6-9 ms pro Poll.
        # In einem Executor-Thread wären sie sogar falsch, weil die Verbindung
        # an den Thread gebunden ist, der sie erzeugt hat.
        stats = self.store.record(offers, prices, live_providers=live)
        log.info("Poll: %d Angebote, %d neu, %d Preisänderungen, %d verschwunden",
                 len(offers), stats["new"], stats["changed"], stats["gone"])
        self.last_poll_ok = time.time()

        for watch in self.store.watches():
            try:
                await self.process_watch(watch, offers)
            except Exception:
                log.exception("Watch %s fehlgeschlagen", watch["id"])

        try:
            await self.process_alerts(offers, prices)
        except Exception:
            log.exception("Alarm-Prüfung fehlgeschlagen")

        if self.cfg.prune_days:
            removed = self.store.prune(self.cfg.prune_days)
            if removed:
                log.info("%d alte Angebote entfernt", removed)

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()

    @poll_loop.error
    async def poll_error(self, err: BaseException) -> None:
        """Letztes Netz. Ohne das bliebe die Schleife nach einem Fehler tot."""
        log.exception("Poll-Schleife abgestürzt, wird neu gestartet", exc_info=err)
        try:
            self.poll_loop.restart()
        except RuntimeError:
            log.error("Neustart der Poll-Schleife fehlgeschlagen — /status zeigt den Zustand an")

    async def resolve_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        """Kanal holen. None heißt: dauerhaft weg, nicht bloß gerade gestört.

        Discord unterscheidet das sauber - 404 für gelöscht, 403 für kein
        Zugriff mehr. Alles andere (5xx, Netz) fliegt weiter und wird erneut
        versucht, statt eine funktionierende Überwachung wegzuwerfen.
        """
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    async def process_watch(self, watch: Any, offers: list[ss.Offer]) -> None:
        filt = json.loads(watch["filter_json"])
        hits = apply_filter(offers, filt, self.specs, self.cfg)
        prices = [o.price(self.cfg.gross, self.cfg.include_ip) for o in hits]

        diff = self.store.diff_watch(watch["id"], hits, prices, self.cfg.drop_pct,
                                     self.cfg.drop_abs, live_providers=self.live_providers)
        if not diff.has_news:
            return

        channel = await self.resolve_channel(watch["channel_id"])
        if channel is None:
            self.store.remove_watch(watch["id"])
            log.warning("Watch %s abgeschaltet: Kanal %s ist weg oder gesperrt",
                        watch["id"], watch["channel_id"])
            return

        fresh, cheaper, gone = diff.fresh, diff.cheaper, diff.gone
        headline = "🔔 Neue Treffer" if fresh else ("📉 Preise gefallen" if cheaper else "👋 Angebot weg")
        embed = discord.Embed(
            title=f"{headline} · {watch['name']}",
            description=filter_summary(filt, self.specs),
            colour=tier_colour([o for o, _ in fresh]) if fresh else 0x3498DB,
        )
        if fresh:
            embed.add_field(name=f"✨ Neu ({len(fresh)})",
                            value=cards_block(fresh, 4, self.cfg), inline=False)
        if cheaper:
            block = block_within_limit(
                [change_card(o, a, n, self.cfg) for o, a, n in cheaper[:4]], len(cheaper))
            embed.add_field(name=f"📉 Günstiger ({len(cheaper)})", value=block, inline=False)
        if gone:
            names = " · ".join(f"`{r['provider']} {r['offer_id']}`" for r in gone[:10])
            embed.add_field(name=f"👋 Nicht mehr verfügbar ({len(gone)})", value=names, inline=False)

        embed.set_footer(text=f"Watch #{watch['id']} · Preise {self.cfg.price_note}")
        embed.timestamp = discord.utils.utcnow()
        await channel.send(content=self.cfg.mention_for(watch["mention"]), embed=embed)
        # Erst jetzt gilt der Stand als gemeldet. Schlägt das Senden fehl,
        # bleibt alles offen und der nächste Poll versucht es erneut.
        self.store.commit_watch_diff(watch["id"], diff)

    async def process_alerts(self, offers: list[ss.Offer], prices: list[float]) -> None:
        """Prüft die Zielpreis-Alarme und schließt sie beim Auslösen."""
        by_key = {o.key: (o, p) for o, p in zip(offers, prices)}

        for alert in self.store.alerts():
            # Hat der Anbieter diesmal nicht geantwortet, ist über diesen Alarm
            # nichts zu sagen. Ihn als "Angebot weg" zu schließen wäre endgültig.
            if alert["key"].split(":", 1)[0] not in self.live_providers:
                continue

            found = by_key.get(alert["key"])
            channel = await self.resolve_channel(alert["channel_id"])
            if channel is None:
                self.store.close_alert(alert["id"], "cancelled")
                log.warning("Alarm %s abgebrochen: Kanal %s ist weg oder gesperrt",
                            alert["id"], alert["channel_id"])
                continue

            if found is None:
                row = self.store.offer_row(alert["key"])
                lines = [f"`{alert['key']}`" + (f" — {row['cpu']}" if row else "")]
                if row:
                    lines.append(f"Zuletzt {fmt_eur(row['last_price'])}, "
                                 f"dein Ziel war {fmt_eur(alert['target'])}.")
                else:
                    lines.append(f"Dein Ziel war {fmt_eur(alert['target'])}.")
                embed = discord.Embed(
                    title="👋 Beobachtetes Angebot ist weg",
                    description="\n".join(lines),
                    colour=NEUTRAL_COLOUR,
                )
                embed.set_footer(text=f"Alarm #{alert['id']} wurde beendet")
                await channel.send(content=self.cfg.mention_for(alert["mention"]), embed=embed)
                self.store.close_alert(alert["id"], "gone")
                continue

            offer, price = found
            if price > alert["target"] + 0.005:
                continue

            embed = discord.Embed(
                title="🎯 Zielpreis erreicht!",
                description=offer_card(offer, price, self.cfg),
                colour=0x2ECC71,
            )
            row = self.store.offer_row(alert["key"])
            if row:
                embed.add_field(
                    name="Verlauf",
                    value=f"Ziel war {fmt_eur(alert['target'])} · "
                          f"beobachtet seit <t:{alert['created_at']}:R> · "
                          f"Start {fmt_eur(row['first_price'])}",
                    inline=False,
                )
            embed.set_footer(text=f"Alarm #{alert['id']} · Auktionsserver sind schnell weg")
            await channel.send(content=self.cfg.mention_for(alert["mention"]), embed=embed)
            self.store.close_alert(alert["id"], "hit")


# --------------------------------------------------------------------------- #
# Slash-Commands
# --------------------------------------------------------------------------- #

FILTER_DESCRIPTIONS = {
    "tier": "Mindest-Einstufung aus server_specs.json",
    "max_preis": "Höchster Monatspreis in Euro",
    "min_ram": "Mindest-RAM in GB",
    "storage": "Mindest-Storage-Klasse",
    "min_sts": "Mindest-Single-Thread-Score",
    "anbieter": "Nur ein Anbieter",
    "setup_gebühr_ok": "Auch Angebote mit Setup-Gebühr zeigen",
}


def register_commands(bot: ServerBot) -> None:
    cfg = bot.cfg

    # -- /suche ------------------------------------------------------------- #

    @bot.tree.command(name="suche", description="Aktuelle Serverangebote abfragen")
    @app_commands.describe(**FILTER_DESCRIPTIONS, pro_seite="Angebote pro Seite (3-8)")
    @app_commands.choices(tier=TIER_CHOICES, storage=STORAGE_CHOICES, anbieter=PROVIDER_CHOICES)
    async def suche(
        interaction: discord.Interaction,
        tier: app_commands.Choice[str] | None = None,
        max_preis: float | None = None,
        min_ram: int | None = None,
        storage: app_commands.Choice[str] | None = None,
        min_sts: int | None = None,
        anbieter: app_commands.Choice[str] | None = None,
        setup_gebühr_ok: bool = False,
        pro_seite: app_commands.Range[int, 3, 8] = 5,
    ) -> None:
        await interaction.response.defer()
        try:
            offers = await bot.refresh()
        except RuntimeError as err:
            await interaction.followup.send(f"⚠️ Abruf fehlgeschlagen: {err}")
            return

        filt = build_filter(bot.specs, tier.value if tier else None, max_preis, min_ram,
                            storage.value if storage else None, min_sts,
                            anbieter.value if anbieter else None, setup_gebühr_ok)
        hits = apply_filter(offers, filt, bot.specs, cfg)

        if not hits:
            embed = discord.Embed(
                title="🔍 Keine Treffer",
                description=f"**Filter:** {filter_summary(filt, bot.specs)}\n"
                            f"Durchsucht: {len(offers)} Angebote.",
                colour=NEUTRAL_COLOUR,
            )
            # Hilfreicher als ein blankes "nichts gefunden": zeigen, wie weit
            # das Preislimit danebenliegt.
            if filt.get("max_price") is not None:
                relaxed = {k: v for k, v in filt.items() if k != "max_price"}
                near = apply_filter(offers, relaxed, bot.specs, cfg)
                near.sort(key=lambda o: o.price(cfg.gross, cfg.include_ip))
                if near:
                    pairs = [(o, o.price(cfg.gross, cfg.include_ip)) for o in near[:3]]
                    embed.add_field(
                        name=f"💡 Knapp über deinem Limit von {fmt_eur(filt['max_price'])}",
                        value=cards_block(pairs, 3, cfg), inline=False)
            await interaction.followup.send(embed=embed)
            return

        view = OfferPager(bot, interaction.user.id, filt, hits, pro_seite)
        await interaction.followup.send(embed=view.embed(), view=view)
        view.message = await interaction.original_response()

    # -- /verlauf ----------------------------------------------------------- #

    @bot.tree.command(name="verlauf", description="Preisverlauf eines Angebots")
    @app_commands.describe(server_id="Angebots-ID, z. B. 3062623 oder hetzner:3062623")
    async def verlauf(interaction: discord.Interaction, server_id: str) -> None:
        await interaction.response.defer()
        keys = bot.store.find_keys(server_id.strip())
        if not keys:
            await interaction.followup.send(embed=discord.Embed(
                title="📭 Nichts in der Historie",
                description=f"Zu `{server_id}` liegt noch nichts vor.\n"
                            "Der Bot kennt nur Angebote, die er seit seinem Start gesehen hat.",
                colour=NEUTRAL_COLOUR,
            ))
            return

        embed = discord.Embed(title=f"📈 Preisverlauf · {server_id}", colour=0xF1C40F)
        for key in keys[:2]:
            row = bot.store.offer_row(key)
            series = bot.store.price_series(key)
            if row is None or not series:
                continue
            values = [p for _, p in series]
            lo, hi, cur, start = min(values), max(values), values[-1], values[0]
            trend = cur - start
            if trend < -0.005:
                arrow, mood = "📉", f"fällt · −{fmt_eur(abs(trend))} seit Start"
            elif trend > 0.005:
                arrow, mood = "📈", f"steigt · +{fmt_eur(trend)} seit Start"
            else:
                arrow, mood = "➡️", "unverändert seit Start"
            status = "🟢 aktiv" if row["gone_at"] is None else "⚪ verschwunden"
            at_low = "  🏷️ **auf Tiefstand**" if cur <= lo + 0.005 else ""

            embed.add_field(
                name=f"{PROVIDER_LABEL.get(row['provider'], row['provider'])} "
                     f"{row['offer_id']} · {row['cpu']}",
                value=(f"```\n{sparkline(values)}\n```"
                       f"**{fmt_eur(cur)}** jetzt{at_low}\n"
                       f"{arrow} {mood} ({fmt_eur(start)})\n"
                       f"Tief {fmt_eur(lo)} · Hoch {fmt_eur(hi)}\n"
                       f"{forecast_line(forecast(series))}\n"
                       f"{len(series)} "
                       f"{'Änderung' if len(series) == 1 else 'Änderungen'}, "
                       f"beobachtet seit <t:{row['first_seen']}:R>\n"
                       f"{status} · {row['ram_gb']} GB RAM · "
                       f"{fmt_size(row['storage_gb'])} "
                       f"{STORAGE_ICON.get(row['storage_class'], '')} "
                       f"{STORAGE_LABEL.get(row['storage_class'], '')} · "
                       f"[Angebot öffnen]({row['url']})"),
                inline=False,
            )
        embed.set_footer(text=f"Sparkline: älteste Änderung links · Preise {cfg.price_note}")
        await interaction.followup.send(embed=embed)

    # -- /watch ------------------------------------------------------------- #

    watch_group = app_commands.Group(name="watch", description="Dauerüberwachung für diesen Kanal")

    @watch_group.command(name="add", description="Neue Überwachung in diesem Kanal anlegen")
    @app_commands.describe(name="Bezeichnung, z. B. 'ATM11 Budget'",
                           erwähnen="Wer bei Treffern gepingt wird",
                           **FILTER_DESCRIPTIONS)
    @app_commands.choices(tier=TIER_CHOICES, storage=STORAGE_CHOICES, anbieter=PROVIDER_CHOICES)
    async def watch_add(
        interaction: discord.Interaction,
        name: str,
        tier: app_commands.Choice[str] | None = None,
        max_preis: float | None = None,
        min_ram: int | None = None,
        storage: app_commands.Choice[str] | None = None,
        min_sts: int | None = None,
        anbieter: app_commands.Choice[str] | None = None,
        setup_gebühr_ok: bool = False,
        erwähnen: discord.Role | discord.Member | None = None,
    ) -> None:
        await interaction.response.defer()
        filt = build_filter(bot.specs, tier.value if tier else None, max_preis, min_ram,
                            storage.value if storage else None, min_sts,
                            anbieter.value if anbieter else None, setup_gebühr_ok)
        if len(filt) <= 2:  # nur _source und no_setup_fee
            await interaction.followup.send(
                "⚠️ Bitte mindestens ein Kriterium angeben — sonst meldet die Überwachung "
                "jedes neue Angebot der Börse."
            )
            return

        watch_id = bot.store.add_watch(
            interaction.guild_id, interaction.channel_id, interaction.user.id,
            name, filt, erwähnen.mention if erwähnen else None,
        )

        try:
            offers = await bot.refresh()
        except RuntimeError as err:
            await interaction.followup.send(
                f"✅ Überwachung #{watch_id} angelegt, aber der Abruf schlug fehl: {err}")
            return

        hits = apply_filter(offers, filt, bot.specs, cfg)
        prices = [o.price(cfg.gross, cfg.include_ip) for o in hits]
        bot.store.seed_watch(watch_id, hits, prices)

        embed = discord.Embed(
            title=f"✅ Überwachung #{watch_id} · {name}",
            description=f"**Filter:** {filter_summary(filt, bot.specs)}\n"
                        f"**Ping:** {ping_summary(cfg, erwähnen.mention if erwähnen else None)}",
            colour=0x2ECC71,
        )
        embed.add_field(
            name=f"Aktueller Stand ({len(hits)} Treffer)",
            value=cards_block(list(zip(hits, prices)), 3, cfg) if hits
                  else "Derzeit nichts Passendes — du hörst von mir, sobald sich das ändert.",
            inline=False,
        )
        embed.set_footer(
            text=f"Prüfung alle {cfg.poll_minutes} Min. · Der aktuelle Stand gilt als bekannt, "
                 f"gemeldet wird nur Neues oder ein Rutsch um "
                 f"{cfg.drop_abs:.0f} € bzw. {cfg.drop_pct:.0f} %."
        )
        await interaction.followup.send(embed=embed)

    @watch_group.command(name="edit", description="Bestehende Überwachung ändern")
    @app_commands.describe(
        watch_id="ID aus /watch list",
        name="Neue Bezeichnung",
        erwähnen="Wer künftig gepingt wird",
        zurücksetzen="Ein Kriterium wieder entfernen",
        **FILTER_DESCRIPTIONS,
    )
    @app_commands.choices(tier=TIER_CHOICES, storage=STORAGE_CHOICES, anbieter=PROVIDER_CHOICES,
                          zurücksetzen=RESET_CHOICES)
    async def watch_edit(
        interaction: discord.Interaction,
        watch_id: int,
        name: str | None = None,
        tier: app_commands.Choice[str] | None = None,
        max_preis: float | None = None,
        min_ram: int | None = None,
        storage: app_commands.Choice[str] | None = None,
        min_sts: int | None = None,
        anbieter: app_commands.Choice[str] | None = None,
        setup_gebühr_ok: bool | None = None,
        erwähnen: discord.Role | discord.Member | None = None,
        zurücksetzen: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        row = bot.store.get_watch(watch_id, interaction.channel_id)
        if row is None:
            await interaction.followup.send(
                f"⚠️ Überwachung #{watch_id} gibt es in diesem Kanal nicht. "
                "Bestehende zeigt `/watch list`.")
            return

        old_filt = json.loads(row["filter_json"])
        filt = dict(old_filt)

        if tier:
            filt["min_tier_rank"] = bot.specs.tiers[tier.value]["rank"]
        if max_preis is not None:
            filt["max_price"] = max_preis
        if min_ram is not None:
            filt["min_ram"] = min_ram
        if storage:
            filt["min_storage_class"] = storage.value
        if min_sts is not None:
            filt["min_sts"] = min_sts
        if anbieter:
            if anbieter.value == "all":
                filt.pop("provider", None)
            else:
                filt["provider"] = anbieter.value
        if setup_gebühr_ok is not None:
            if setup_gebühr_ok:
                filt.pop("no_setup_fee", None)
            else:
                filt["no_setup_fee"] = True
        if zurücksetzen:
            for key in RESET_KEYS[zurücksetzen.value]:
                filt.pop(key, None)

        touched = filt != old_filt or name is not None or erwähnen is not None
        if not touched:
            await interaction.followup.send(
                "Nichts geändert — gib mindestens ein Feld an, das anders sein soll.")
            return

        criteria = [k for k in filt if k not in ("_source", "no_setup_fee")]
        if not criteria:
            await interaction.followup.send(
                "⚠️ Damit bliebe kein Kriterium übrig, und die Überwachung würde jedes "
                "neue Angebot der Börse melden. Änderung verworfen.")
            return

        bot.store.update_watch(watch_id, name=name, filt=filt,
                               mention=erwähnen.mention if erwähnen else None)

        try:
            offers = await bot.refresh()
        except RuntimeError as err:
            await interaction.followup.send(
                f"✅ Überwachung #{watch_id} geändert, aber der Abruf schlug fehl: {err}")
            return

        hits = apply_filter(offers, filt, bot.specs, cfg)
        prices = [o.price(cfg.gross, cfg.include_ip) for o in hits]
        # Kompletter Neustart des Trefferstands. Ohne replace=True wuerden
        # Angebote, die nur nicht mehr zum neuen Filter passen, beim naechsten
        # Poll als "nicht mehr verfuegbar" gemeldet.
        bot.store.seed_watch(watch_id, hits, prices, replace=True)

        embed = discord.Embed(
            title=f"✏️ Überwachung #{watch_id} · {name or row['name']}",
            description=f"**Ping:** {ping_summary(cfg, erwähnen.mention if erwähnen else row['mention'])}",
            colour=0x5865F2,
        )
        if filt != old_filt:
            embed.add_field(name="Vorher", value=filter_summary(old_filt, bot.specs), inline=False)
            embed.add_field(name="Jetzt", value=filter_summary(filt, bot.specs), inline=False)
        else:
            embed.add_field(name="Filter", value=filter_summary(filt, bot.specs), inline=False)
        embed.add_field(
            name=f"Aktueller Stand ({len(hits)} Treffer)",
            value=cards_block(list(zip(hits, prices)), 3, cfg) if hits
                  else "Derzeit nichts Passendes — du hörst von mir, sobald sich das ändert.",
            inline=False,
        )
        embed.set_footer(text="Der neue Stand gilt als bekannt, gemeldet wird nur, "
                              "was ab jetzt dazukommt oder fällt.")
        await interaction.followup.send(embed=embed)

    @watch_group.command(name="list", description="Überwachungen dieses Kanals anzeigen")
    async def watch_list(interaction: discord.Interaction) -> None:
        rows = bot.store.watches(interaction.channel_id)
        if not rows:
            await interaction.response.send_message(embed=discord.Embed(
                title="📭 Keine Überwachung in diesem Kanal",
                description="Anlegen mit `/watch add`.",
                colour=NEUTRAL_COLOUR))
            return
        embed = discord.Embed(
            title=f"👀 {len(rows)} {'Überwachung' if len(rows) == 1 else 'Überwachungen'}",
            colour=0x5865F2)
        for r in rows:
            embed.add_field(
                name=f"#{r['id']} · {r['name']}",
                value=f"{filter_summary(json.loads(r['filter_json']), bot.specs)}\n"
                      f"Angelegt <t:{r['created_at']}:R>\n"
                      f"Pingt: {ping_summary(cfg, r['mention'])}",
                inline=False,
            )
        embed.set_footer(text="Ändern mit /watch edit <id> · entfernen mit /watch remove <id>")
        await interaction.response.send_message(embed=embed)

    @watch_group.command(name="remove", description="Überwachung entfernen")
    @app_commands.describe(watch_id="ID aus /watch list")
    async def watch_remove(interaction: discord.Interaction, watch_id: int) -> None:
        ok = bot.store.remove_watch(watch_id, interaction.channel_id)
        await interaction.response.send_message(
            f"🗑️ Überwachung #{watch_id} entfernt." if ok
            else f"⚠️ Überwachung #{watch_id} gibt es in diesem Kanal nicht."
        )

    bot.tree.add_command(watch_group)

    # -- /alarm -------------------------------------------------------------- #

    alarm_group = app_commands.Group(name="alarm", description="Zielpreis für einen bestimmten Server")

    @alarm_group.command(name="setzen", description="Ping, sobald dieser Server unter deinen Preis fällt")
    @app_commands.describe(server_id="Angebots-ID, z. B. 3062623 oder hetzner:3062623",
                           zielpreis="Preis in Euro, ab dem du gepingt wirst",
                           erwähnen="Wer gepingt wird")
    async def alarm_set(interaction: discord.Interaction, server_id: str, zielpreis: float,
                        erwähnen: discord.Role | discord.Member | None = None) -> None:
        await interaction.response.defer()
        try:
            await bot.refresh()
        except RuntimeError as err:
            await interaction.followup.send(f"⚠️ Abruf fehlgeschlagen: {err}")
            return

        found = resolve_offers(bot, server_id)
        if not found:
            await interaction.followup.send(
                f"⚠️ `{server_id}` ist gerade nicht in der Börse. Prüf die ID mit `/suche`.")
            return
        if len(found) > 1:
            keys = " · ".join(f"`{o.key}`" for o in found)
            await interaction.followup.send(
                f"Diese ID gibt es bei beiden Anbietern. Bitte genauer: {keys}")
            return

        offer = found[0]
        price = offer.price(cfg.gross, cfg.include_ip)

        if bot.store.alert_for(offer.key, interaction.channel_id):
            await interaction.followup.send(
                f"Für `{offer.key}` läuft in diesem Kanal schon ein Alarm. "
                "Erst `/alarm entfernen`, dann neu setzen.")
            return
        if zielpreis >= price:
            await interaction.followup.send(
                f"Der Server kostet schon {fmt_eur(price)} und liegt damit unter deinem Ziel "
                f"von {fmt_eur(zielpreis)} — der Alarm würde sofort auslösen. "
                f"Setz das Ziel niedriger, wenn du auf einen weiteren Rutsch warten willst.")
            return

        alert_id = bot.store.add_alert(offer.key, interaction.channel_id, interaction.user.id,
                                       zielpreis, erwähnen.mention if erwähnen else None)
        fc = forecast(bot.store.price_series(offer.key), zielpreis)

        embed = discord.Embed(title=f"🔔 Alarm #{alert_id} gesetzt", colour=0x5865F2)
        embed.description = offer_card(offer, price, cfg)
        embed.add_field(
            name=f"Ziel {fmt_eur(zielpreis)} ({vat_counterpart(zielpreis, cfg)})",
            value=f"noch {fmt_eur(price - zielpreis)} zu gehen\n{forecast_line(fc, zielpreis)}\n"
                  f"Ping: {ping_summary(cfg, erwähnen.mention if erwähnen else None)}",
            inline=False,
        )
        embed.set_footer(text=f"Prüfung alle {cfg.poll_minutes} Min. · Zielpreis "
                              f"{cfg.price_mode_label} · Der Alarm endet, sobald er auslöst "
                              "oder das Angebot weg ist.")
        await interaction.followup.send(embed=embed)

    @alarm_group.command(name="liste", description="Laufende Alarme dieses Kanals")
    async def alarm_list(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        rows = bot.store.alerts(interaction.channel_id)
        if not rows:
            await interaction.followup.send(embed=discord.Embed(
                title="📭 Keine Alarme in diesem Kanal",
                description="Setzen mit `/alarm setzen server_id:<id> zielpreis:<€>`.",
                colour=NEUTRAL_COLOUR))
            return
        try:
            await bot.refresh()
        except RuntimeError:
            pass

        by_key = {o.key: o.price(cfg.gross, cfg.include_ip) for o in bot.offers}
        embed = discord.Embed(
            title=f"🔔 {len(rows)} {'Alarm' if len(rows) == 1 else 'Alarme'}", colour=0x5865F2)
        for a in rows:
            row = bot.store.offer_row(a["key"])
            price = by_key.get(a["key"])
            fc = forecast(bot.store.price_series(a["key"]), a["target"])
            if price is None:
                value = "⚪ derzeit nicht in der Börse"
            else:
                value = (f"Jetzt **{fmt_eur(price)}** · noch {fmt_eur(price - a['target'])} "
                         f"bis zum Ziel\n{forecast_line(fc, a['target'])}")
            embed.add_field(
                name=f"#{a['id']} · {row['cpu'] if row else a['key']} → {fmt_eur(a['target'])}",
                value=value, inline=False)
        embed.set_footer(text="Entfernen mit /alarm entfernen <id>")
        await interaction.followup.send(embed=embed)

    @alarm_group.command(name="entfernen", description="Alarm abbrechen")
    @app_commands.describe(alarm_id="ID aus /alarm liste")
    async def alarm_remove(interaction: discord.Interaction, alarm_id: int) -> None:
        ok = bot.store.close_alert(alarm_id, "cancelled", interaction.channel_id)
        await interaction.response.send_message(
            f"🗑️ Alarm #{alarm_id} abgebrochen." if ok
            else f"⚠️ Alarm #{alarm_id} gibt es in diesem Kanal nicht.")

    bot.tree.add_command(alarm_group)

    # -- /warum -------------------------------------------------------------- #

    @bot.tree.command(name="warum", description="Wie kommt ein Angebot zu seinem Score und Tier?")
    @app_commands.describe(server_id="Angebots-ID, z. B. 3062623 oder hetzner:3062623")
    async def warum(interaction: discord.Interaction, server_id: str) -> None:
        await interaction.response.defer()
        try:
            await bot.refresh()
        except RuntimeError as err:
            await interaction.followup.send(f"⚠️ Abruf fehlgeschlagen: {err}")
            return

        found = resolve_offers(bot, server_id)
        if not found:
            await interaction.followup.send(
                f"⚠️ `{server_id}` ist gerade nicht in der Börse. IDs findest du mit `/suche`.")
            return

        offer = found[0]
        price = offer.price(cfg.gross, cfg.include_ip)
        embed = discord.Embed(
            title=f"🔬 Bewertung · {PROVIDER_LABEL.get(offer.provider)} {offer.offer_id}",
            description=offer_card(offer, price, cfg),
            colour=tier_colour([offer]),
        )
        if len(found) > 1:
            embed.description += f"\n*Dieselbe Hardware gibt es auch als `{found[1].key}`.*"

        # 1. Ausschlusskriterien
        if offer.disqualified:
            embed.add_field(
                name="❌ Ausgeschlossen",
                value="\n".join(f"• {r}" for r in offer.disqualified)
                      + "\n\n*Ausgeschlossene Angebote tauchen in `/suche` gar nicht erst auf.*",
                inline=False)
        else:
            embed.add_field(name="✅ Ausschlusskriterien",
                            value="Keins verletzt — RAM, Storage, Takt und Single-Thread "
                                  "liegen alle über der Schmerzgrenze.", inline=False)

        # 2. Tier-Prüfung. Interessant ist die naechsthoehere Stufe: die
        #    erreichte kennt man, an der naechsten sieht man, was fehlt.
        ranks = sorted(bot.specs.tiers.items(), key=lambda kv: kv[1]["rank"])
        current_rank = bot.specs.tiers[offer.tier]["rank"] if offer.tier else 0
        nxt = next((n for n, t in ranks if t["rank"] == current_rank + 1), None)

        if offer.tier:
            embed.add_field(
                name=f"{TIER_BADGE.get(offer.tier, '')} Erreicht: {offer.tier}",
                value=bot.specs.tiers[offer.tier]["label"], inline=False)
        if nxt and offer.cpu:
            checks = ss.tier_checks(offer, bot.specs, nxt, price)
            failed = [c for c in checks if not c.ok]
            embed.add_field(
                name=f"{TIER_BADGE.get(nxt, '')} Gegen „{nxt}“ geprüft — "
                     f"{len(failed)} von {len(checks)} nicht erfüllt",
                value="\n".join(f"{'✅' if c.ok else '❌'} **{c.label}** — {c.detail}"
                                for c in checks) if failed
                      else "Alles erfüllt.", inline=False)
        elif not nxt:
            embed.add_field(name="🏆 Höchste Stufe",
                            value="Über `best` geht nichts mehr.", inline=False)

        # 3. Score-Zerlegung
        comps = ss.score_components(offer, bot.specs)
        if comps:
            # Kein Breiten-Padding innerhalb der Sternchen: ein Leerzeichen direkt
            # hinter ** hebt die Markdown-Auszeichnung in Discord auf.
            lines = [f"`{score_bar(c.normalized * 100, 8)}` **{c.contribution * 100:.1f}** "
                     f"von {c.weight * 100:.0f} — {c.label}\n> {c.detail}" for c in comps]
            embed.add_field(
                name=f"📊 Score {offer.score} von 100",
                value="\n".join(lines), inline=False)

        # 4. Preisverlauf, falls vorhanden
        series = bot.store.price_series(offer.key)
        if len(series) >= 2:
            fc = forecast(series)
            embed.add_field(name="📈 Preis",
                            value=f"`{sparkline([p for _, p in series])}`\n{forecast_line(fc)}",
                            inline=False)

        embed.set_footer(text="Gewichte und Grenzwerte stehen in server_specs.json")
        await interaction.followup.send(embed=embed)

    # -- /status, /poll, /hilfe ---------------------------------------------- #

    @bot.tree.command(name="status", description="Poll-Zustand und Datenbank")
    async def status(interaction: discord.Interaction) -> None:
        s = bot.store.stats()
        # Nicht nur "gab es je einen Poll?", sondern "läuft die Schleife und war
        # der letzte Durchlauf rechtzeitig?". Sonst meldet /status noch Wochen
        # nach einem toten Poll-Task ein grünes "Alles läuft".
        running = bot.poll_loop.is_running()
        overdue = bool(bot.last_poll_ok) and time.time() - bot.last_poll_ok > cfg.poll_minutes * 180
        healthy = running and not overdue and bot.last_error is None and s["last_poll"] is not None

        embed = discord.Embed(
            title=("🟢 Alles läuft" if healthy else "🟡 Mit Einschränkungen"),
            description=(f"Letzter Poll <t:{s['last_poll']}:R>" if s["last_poll"]
                         else "Noch kein Durchlauf erfolgt."),
            colour=0x2ECC71 if healthy else 0xE67E22,
        )
        if not running:
            embed.add_field(
                name="🔴 Poll-Schleife steht",
                value="Der Hintergrund-Task läuft nicht mehr. `/poll` löst einen "
                      "einzelnen Durchlauf aus; für den Dauerbetrieb muss der Bot neu "
                      "gestartet werden.", inline=False)
        elif overdue:
            embed.add_field(
                name="🟠 Poll überfällig",
                value=f"Der letzte erfolgreiche Durchlauf ist länger als "
                      f"{cfg.poll_minutes * 3} Min. her.", inline=False)
        embed.add_field(name="⏱️ Intervall", value=f"alle {cfg.poll_minutes} Min.")
        embed.add_field(name="🏢 Anbieter",
                        value="\n".join(PROVIDER_NAME.get(p, p) for p in cfg.providers))
        embed.add_field(name="💶 Preise", value=cfg.price_note)
        embed.add_field(name="📦 Angebote live", value=f"{s['offers_live']}")
        embed.add_field(name="🗃️ Je gesehen", value=f"{s['offers_total']}")
        embed.add_field(name="📈 Preispunkte", value=f"{s['price_points']}")
        embed.add_field(name="👀 Überwachungen", value=f"{s['watches']}")
        embed.add_field(name="💾 Datenbank", value=f"{s['db_bytes'] / 1024:.0f} KB")
        embed.add_field(name="🔔 Alert ab",
                        value=f"−{cfg.drop_abs:.0f} € oder −{cfg.drop_pct:.0f} %")
        embed.add_field(name="📣 Standard-Ping",
                        value=cfg.default_mention or "keiner (`DEFAULT_MENTION` leer)")
        if bot.last_error:
            embed.add_field(name="⚠️ Letzter Fehler", value=bot.last_error[:1000], inline=False)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="poll", description="Sofortigen Durchlauf auslösen")
    async def poll_now(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await bot.poll_loop()
        s = bot.store.stats()
        await interaction.followup.send(
            f"✅ Durchlauf erledigt — {s['offers_live']} Angebote live.", ephemeral=True)

    @bot.tree.command(name="hilfe", description="Kurzübersicht der Befehle")
    async def hilfe(interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🖥️ Serversuche",
            description="Beobachtet die **Hetzner Serverbörse** und **Prepaid-Hoster** und "
                        "bewertet jedes Angebot gegen deine Kriterien aus `server_specs.json`.",
            colour=0x5865F2,
        )
        embed.add_field(
            name="Befehle",
            value=("`/suche` — Angebote abfragen, blätterbar\n"
                   "`/warum <id>` — wie Score und Tier zustande kommen\n"
                   "`/alarm setzen <id> <€>` — Ping, wenn *dieser* Server fällt\n"
                   "`/alarm liste` · `/alarm entfernen` — Alarme verwalten\n"
                   "`/watch add` — Dauerüberwachung nach Filter\n"
                   "`/watch edit` · `/watch list` · `/watch remove` — verwalten\n"
                   "`/verlauf <id>` — Preisverlauf mit Prognose\n"
                   "`/status` · `/poll` — Zustand, sofort nachsehen"),
            inline=False,
        )
        tiers = []
        for name, t in sorted(bot.specs.tiers.items(), key=lambda kv: kv[1]["rank"]):
            tiers.append(f"{TIER_BADGE.get(name, '')} **{name}** — {t['label']}\n"
                         f"> ab {t['cpu_single_thread_score_min']} STS · "
                         f"{t['ram_total_gb_min']} GB RAM · "
                         f"{STORAGE_LABEL.get(t['storage_class_min'], '')} · "
                         f"bis {fmt_eur(t['price_eur_month_max'])}")
        embed.add_field(name="Einstufungen", value="\n".join(tiers), inline=False)
        embed.add_field(
            name="Gut zu wissen",
            value=("• Prepaid-Hoster verkauft dieselben Hetzner-Auktionsserver weiter — "
                   "gleiche ID, höherer Preis.\n"
                   "• Der Score gewichtet Single-Thread-Leistung, L3-Cache, Storage und RAM. "
                   "`/warum` zeigt die Rechnung.\n"
                   "• **Watch** = Filter über alle Angebote, **Alarm** = ein bestimmter Server.\n"
                   "• Prognosen sind Schätzungen aus der beobachteten Senkungsrate — "
                   "Auktionsserver können jederzeit weggekauft werden.\n"
                   f"• Jede Karte zeigt **netto und brutto** ({VAT_PCT} % USt.). "
                   f"Filter, Zielpreise und Tier-Deckel rechnen "
                   f"**{cfg.price_mode_label}**.\n"
                   f"• Bei Treffern wird "
                   + (f"{cfg.default_mention} gepingt, sofern der Watch keine eigene "
                      "Erwähnung hat." if cfg.default_mention
                      else "nur gepingt, wer per `erwähnen` am Watch oder Alarm hinterlegt "
                           "ist (oder `DEFAULT_MENTION` in der .env setzen).")),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = Config()
    cfg.validate()
    bot = ServerBot(cfg)
    try:
        bot.run(cfg.token, log_handler=None)
    finally:
        bot.store.close()


if __name__ == "__main__":
    main()
