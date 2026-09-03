#!/usr/bin/env python3
"""
Serversuche - Angebote bei Hetzner Serverbörse und Prepaid-Hoster (PPH) prüfen.

Bewertet alle Angebote gegen die Kriterien aus server_specs.json (Tiers,
Hard-Disqualifier, Scoring-Gewichte, Alert-Profil).

Datenquellen (beides offizielle, wenn auch undokumentierte JSON-Endpunkte der
Anbieter-Frontends - kein HTML-Scraping):

  Hetzner  GET https://www.hetzner.com/_resources/app/data/app/live_data_sb.json
  PPH      GET https://api.pph.sh/public/products/dedicated?page=N

Nur Standardbibliothek, keine Installation nötig.

Beispiele:
  python serversuche.py                          # Alert-Profil aus server_specs.json
  python serversuche.py --tier recommended       # alles ab Tier "recommended"
  python serversuche.py --all --limit 30         # alles, auch Disqualifiziertes
  python serversuche.py --max-price 45 --json treffer.json
  python serversuche.py --watch 300 --webhook https://discord.com/api/webhooks/...
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, NamedTuple

# --------------------------------------------------------------------------- #
# Konstanten
# --------------------------------------------------------------------------- #

HETZNER_URL = "https://www.hetzner.com/_resources/app/data/app/live_data_sb.json"
# Freitextsuche als Query-Parameter statt als Fragment-Anker: nur so filtert die
# Börse schon beim ersten Laden auf die eine ID.
HETZNER_LINK = "https://www.hetzner.com/de/sb/?freetext={id}"
# Ohne chunk=true: eine Zeile pro tatsächlichem Server. Mit chunk=true fasst
# die API baugleiche Konfigurationen zusammen und liefert deutlich weniger.
PPH_URL = "https://api.pph.sh/public/products/dedicated?page={page}"
PPH_LINK = "https://www.prepaid-hoster.de/dedicated/dedicated-server-mieten.html"

USER_AGENT = "serversuche/1.0 (private offer monitor)"
VAT_FACTOR = 1.19  # deutsche USt., beide APIs liefern Nettopreise

# Obergrenzen für die Score-Normalisierung. Bewusst gedeckelt: ein EPYC mit
# 256 MB L3 soll die Skala nicht kaputtmachen, der Cache bringt für den
# Minecraft-Haupttick oberhalb von ~64 MB nichts mehr.
L3_CAP_MB = 64.0
RAM_CAP_GB = 128.0

STORAGE_LABELS = {"nvme": "NVMe", "ssd": "SSD", "hdd": "HDD", "unknown": "unbekannt"}

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SPECS = SCRIPT_DIR / "server_specs.json"
DEFAULT_STATE = SCRIPT_DIR / "state.json"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def http_get_json(url: str, timeout: int = 30, retries: int = 3) -> Any:
    """GET mit Retry/Backoff. Gibt geparstes JSON zurück."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Abruf fehlgeschlagen: {url}: {last_err}")


def post_webhook(url: str, text: str) -> None:
    """Schickt eine Discord-kompatible Textnachricht an einen Webhook."""
    payload = json.dumps({"content": text[:1900]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception as err:  # Webhook-Fehler darf den Lauf nicht killen
        print(f"  ! Webhook fehlgeschlagen: {err}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Datenmodell
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Offer:
    provider: str  # "hetzner" | "pph"
    offer_id: str
    cpu_raw: str  # CPU-Bezeichnung wie vom Anbieter geliefert
    ram_gb: int
    storage_gb: int  # Summe roher Kapazität (kein RAID berücksichtigt)
    storage_class: str  # "nvme" | "ssd" | "hdd" | "unknown"
    disks: list[str]
    price_net: float  # EUR/Monat netto, ohne IPv4
    setup_net: float
    datacenter: str
    url: str
    ecc: bool = False
    fixed_price: bool = False  # False = Auktion, Preis fällt noch
    ip_price_net: float = 0.0
    reduce_next_ts: int | None = None
    vendor_benchmark: int | None = None  # PassMark multi-thread, nur PPH

    # wird von evaluate() gefüllt
    cpu: dict[str, Any] | None = None
    tier: str | None = None
    score: float = 0.0
    disqualified: list[str] = dataclasses.field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.offer_id}"

    def total_net(self, include_ip: bool = False) -> float:
        return self.price_net + (self.ip_price_net if include_ip else 0.0)

    def price(self, gross: bool, include_ip: bool = False) -> float:
        p = self.total_net(include_ip)
        return round(p * VAT_FACTOR, 2) if gross else round(p, 2)


# --------------------------------------------------------------------------- #
# Provider: Hetzner Serverbörse
# --------------------------------------------------------------------------- #


def fetch_hetzner() -> list[Offer]:
    data = http_get_json(HETZNER_URL)
    if not isinstance(data, dict) or "server" not in data:
        raise RuntimeError("Hetzner-JSON hat unerwartete Struktur (Endpunkt geändert?)")

    offers: list[Offer] = []
    for s in data["server"]:
        hw = s.get("Hardware", {})
        storage = hw.get("Storage", {})
        det = storage.get("Details", {}) or {}
        details = s.get("Details", {}) or {}
        prices = s.get("Prices", {}) or {}
        timer = s.get("Timer", {}) or {}

        nvme = det.get("nvme") or []
        sata = det.get("sata") or []  # SATA-Slot in der Serverbörse == SATA-SSD
        hdd = det.get("hdd") or []

        if nvme:
            cls = "nvme"
        elif sata:
            cls = "ssd"
        elif hdd:
            cls = "hdd"
        else:
            cls = "unknown"

        offers.append(
            Offer(
                provider="hetzner",
                offer_id=str(s.get("Id")),
                cpu_raw=(hw.get("CPU") or {}).get("Name", ""),
                ram_gb=int((hw.get("RAM") or {}).get("Size") or 0),
                storage_gb=int(sum(nvme) + sum(sata) + sum(hdd)),
                storage_class=cls,
                disks=list(storage.get("Disks") or []),
                price_net=float((prices.get("monthly") or {}).get("EUR") or 0.0),
                setup_net=float((prices.get("setup") or {}).get("EUR") or 0.0),
                ip_price_net=float(((s.get("IPPrices") or {}).get("monthly") or {}).get("EUR") or 0.0),
                datacenter=((details.get("Datacenter") or {}).get("Name") or "?"),
                url=HETZNER_LINK.format(id=s.get("Id")),
                ecc=bool((hw.get("RAM") or {}).get("ecc")),
                fixed_price=bool(prices.get("fixed")),
                reduce_next_ts=timer.get("ReduceNextTimestamp"),
            )
        )
    return offers


# --------------------------------------------------------------------------- #
# Provider: Prepaid-Hoster
# --------------------------------------------------------------------------- #


def fetch_pph(max_pages: int = 25) -> list[Offer]:
    offers: list[Offer] = []
    page = 1
    while page <= max_pages:
        data = http_get_json(PPH_URL.format(page=page))
        if not isinstance(data, dict) or "data" not in data:
            raise RuntimeError("PPH-JSON hat unerwartete Struktur (Endpunkt geändert?)")

        for s in data["data"]:
            dt_info = s.get("disktype") or {}
            cls = (dt_info.get("type") or "unknown").lower()
            if cls not in ("nvme", "ssd", "hdd"):
                cls = "unknown"
            dc = s.get("datacenter_details") or {}

            offers.append(
                Offer(
                    provider="pph",
                    offer_id=str(s.get("serverid")),
                    cpu_raw=s.get("original_cpu") or s.get("cpu") or "",
                    ram_gb=int(s.get("memory") or 0),
                    storage_gb=int(s.get("disksize") or 0),
                    storage_class=cls,
                    disks=[d for d in (s.get("description") or []) if re.search(r"\b(SSD|HDD|NVMe)\b", d, re.I)],
                    price_net=float(s.get("price") or 0.0),
                    setup_net=float(s.get("setup") or 0.0),
                    datacenter=s.get("datacenter") or dc.get("location") or "?",
                    url=PPH_LINK,
                    ecc="ecc" in (s.get("disktags") or []),
                    fixed_price=True,  # PPH-Preise sind fix, keine fallende Auktion
                    vendor_benchmark=s.get("benchmark"),
                )
            )

        last_page = int(data.get("last_page") or page)
        if page >= last_page:
            break
        page += 1
    return offers


# --------------------------------------------------------------------------- #
# Bewertung gegen server_specs.json
# --------------------------------------------------------------------------- #


class Specs:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.tiers: dict[str, dict] = raw["tiers"]
        self.cpu_reference: list[dict] = raw["cpu_reference"]
        self.storage_classes: dict[str, dict] = raw["storage_classes"]
        self.disq: dict[str, Any] = raw["hard_disqualifiers"]
        self.weights: dict[str, float] = raw["scoring_weights"]
        self.alert: dict[str, Any] = raw.get("alert_profile", {})

        # Längste Muster zuerst, damit "i9-13900k" nicht fälschlich auf ein
        # kürzeres Muster eines anderen Eintrags fällt.
        self._cpu_index: list[tuple[str, dict]] = sorted(
            ((m.lower(), entry) for entry in self.cpu_reference for m in entry["match"]),
            key=lambda t: len(t[0]),
            reverse=True,
        )
        # Tiers absteigend nach rank: höchster erfüllter Tier gewinnt.
        self.tiers_desc = sorted(self.tiers.items(), key=lambda t: t[1]["rank"], reverse=True)

    def storage_rank(self, cls: str) -> int:
        return int(self.storage_classes.get(cls, {}).get("rank", -1))

    def match_cpu(self, raw_name: str) -> dict | None:
        name = re.sub(r"\s+", " ", raw_name.lower())
        for pattern, entry in self._cpu_index:
            if pattern in name:
                return entry
        return None


def evaluate(offer: Offer, specs: Specs, gross: bool, include_ip: bool,
             tier_price_cap: bool = True) -> None:
    """Setzt cpu, disqualified, tier und score auf dem Angebot.

    Alle Preisgrenzen (Tier-Caps wie CLI-Filter) beziehen sich auf die
    angezeigte Preisart - mit --gross also auf Bruttopreise.
    """
    offer.cpu = specs.match_cpu(offer.cpu_raw)
    price = offer.price(gross, include_ip)

    # --- Hard-Disqualifier ------------------------------------------------- #
    d = specs.disq
    reasons: list[str] = []

    if d.get("storage_only_hdd") and offer.storage_class in ("hdd", "unknown"):
        reasons.append("nur HDD-Storage")
    if offer.ram_gb < d.get("ram_total_gb_below", 0):
        reasons.append(f"RAM {offer.ram_gb} GB < {d['ram_total_gb_below']} GB")

    if offer.cpu is None:
        reasons.append("CPU nicht in cpu_reference")
    else:
        sts = offer.cpu["single_thread_score"]
        boost = offer.cpu["boost_ghz"]
        if sts < d.get("cpu_single_thread_score_below", 0):
            reasons.append(f"Single-Thread {sts} < {d['cpu_single_thread_score_below']}")
        if boost < d.get("cpu_boost_ghz_below", 0):
            reasons.append(f"Boost {boost} GHz < {d['cpu_boost_ghz_below']} GHz")

    offer.disqualified = reasons

    # --- Tier -------------------------------------------------------------- #
    offer.tier = None
    if not reasons and offer.cpu:
        for name, _ in specs.tiers_desc:
            if all(chk.ok for chk in tier_checks(offer, specs, name, price, tier_price_cap)):
                offer.tier = name
                break

    # --- Score ------------------------------------------------------------- #
    offer.score = round(sum(c.contribution for c in score_components(offer, specs)) * 100, 1)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


class Check(NamedTuple):
    """Eine einzelne Tier-Anforderung samt Ergebnis."""
    label: str
    ok: bool
    detail: str


class Component(NamedTuple):
    """Ein gewichteter Score-Bestandteil."""
    label: str
    weight: float
    normalized: float
    detail: str

    @property
    def contribution(self) -> float:
        return self.weight * self.normalized


def tier_checks(offer: Offer, specs: Specs, tier_name: str, price: float,
                price_cap: bool = True) -> list[Check]:
    """Alle Anforderungen eines Tiers einzeln geprüft.

    Ist bewusst die einzige Stelle, an der die Tier-Bedingungen stehen:
    evaluate() entscheidet damit, /warum erklärt damit. Zwei Formulierungen
    derselben Regel würden früher oder später auseinanderlaufen.
    """
    t = specs.tiers[tier_name]
    c = offer.cpu
    if c is None:
        return [Check("CPU bekannt", False, "nicht in cpu_reference")]

    checks = [
        Check("RAM", offer.ram_gb >= t["ram_total_gb_min"],
              f"{offer.ram_gb} GB, nötig {t['ram_total_gb_min']} GB"),
        Check("Kerne", c["cores"] >= t["cpu_cores_min"],
              f"{c['cores']}, nötig {t['cpu_cores_min']}"),
        Check("Threads", c["threads"] >= t["cpu_threads_min"],
              f"{c['threads']}, nötig {t['cpu_threads_min']}"),
        Check("Boost-Takt", c["boost_ghz"] >= t["cpu_boost_ghz_min"],
              f"{c['boost_ghz']} GHz, nötig {t['cpu_boost_ghz_min']} GHz"),
        Check("Single-Thread", c["single_thread_score"] >= t["cpu_single_thread_score_min"],
              f"{c['single_thread_score']}, nötig {t['cpu_single_thread_score_min']}"),
        Check("L3-Cache", c["l3_mb"] >= t["cpu_l3_cache_mb_min"],
              f"{c['l3_mb']} MB, nötig {t['cpu_l3_cache_mb_min']} MB"),
        Check("Storage-Klasse",
              specs.storage_rank(offer.storage_class) >= specs.storage_rank(t["storage_class_min"]),
              f"{STORAGE_LABELS.get(offer.storage_class, '?')}, "
              f"nötig {STORAGE_LABELS.get(t['storage_class_min'], '?')}"),
        Check("Storage-Größe", offer.storage_gb >= t["storage_gb_min"],
              f"{offer.storage_gb} GB, nötig {t['storage_gb_min']} GB"),
    ]
    if price_cap:
        # Komma als Dezimaltrenner: diese Texte sind deutsche Prosa und landen
        # im Discord-Embed, nicht in der ASCII-Tabelle der CLI.
        checks.append(Check("Preis", price <= t["price_eur_month_max"],
                            f"{price:.2f} €, Deckel {t['price_eur_month_max']:.2f} €"
                            .replace(".", ",")))
    return checks


def score_components(offer: Offer, specs: Specs) -> list[Component]:
    """Die gewichteten Score-Bestandteile - Grundlage der Rechnung und ihrer Erklärung."""
    if offer.cpu is None:
        return []
    w = specs.weights
    c = offer.cpu

    sts_floor = float(specs.disq.get("cpu_single_thread_score_below", 2400))
    sts_ceil = float(max(e["single_thread_score"] for e in specs.cpu_reference))
    ram_floor = float(specs.disq.get("ram_total_gb_below", 32))
    max_rank = max(v["rank"] for v in specs.storage_classes.values())

    return [
        Component("Single-Thread-Score", w["single_thread_score"],
                  _clamp01((c["single_thread_score"] - sts_floor) / max(1.0, sts_ceil - sts_floor)),
                  f"{c['single_thread_score']} auf Skala {sts_floor:.0f}–{sts_ceil:.0f}"),
        Component("L3-Cache", w["l3_cache_mb"],
                  _clamp01(c["l3_mb"] / L3_CAP_MB),
                  f"{c['l3_mb']} MB, Deckel {L3_CAP_MB:.0f} MB"),
        Component("Storage-Klasse", w["storage_class"],
                  _clamp01(specs.storage_rank(offer.storage_class) / max_rank),
                  STORAGE_LABELS.get(offer.storage_class, "?")),
        Component("RAM", w["ram_total_gb"],
                  _clamp01((offer.ram_gb - ram_floor) / max(1.0, RAM_CAP_GB - ram_floor)),
                  f"{offer.ram_gb} GB auf Skala {ram_floor:.0f}–{RAM_CAP_GB:.0f}"),
    ]


# --------------------------------------------------------------------------- #
# Filter
# --------------------------------------------------------------------------- #


FILTER_FLAGS = ("all", "tier", "max_price", "min_ram", "min_storage_class", "min_sts", "setup_fee_ok")


def build_filter(args: argparse.Namespace, specs: Specs) -> dict[str, Any]:
    """Baut den aktiven Filter.

    Ohne jedes Filter-Argument gilt das alert_profile aus server_specs.json.
    Sobald ein Filter-Argument gesetzt ist, zählt nur noch das explizit
    Angegebene - sonst würde z. B. --tier minimum still am 60-EUR-Deckel des
    Alert-Profils scheitern.
    """
    explicit = any(getattr(args, name) not in (None, False) for name in FILTER_FLAGS)

    if not explicit:
        a = specs.alert
        return {
            "max_price": a.get("price_eur_month_max"),
            "min_ram": a.get("ram_total_gb_min"),
            "min_storage_class": a.get("storage_class_min"),
            "min_sts": a.get("cpu_single_thread_score_min"),
            "no_setup_fee": bool(a.get("require_setup_fee_zero")),
            "_source": "alert_profile",
        }

    f: dict[str, Any] = {"_source": "CLI"}
    if args.tier:
        f["min_tier_rank"] = specs.tiers[args.tier]["rank"]
    if args.max_price is not None:
        f["max_price"] = args.max_price
    if args.min_ram is not None:
        f["min_ram"] = args.min_ram
    if args.min_storage_class is not None:
        f["min_storage_class"] = args.min_storage_class
    if args.min_sts is not None:
        f["min_sts"] = args.min_sts
    if not args.setup_fee_ok and not args.all:
        f["no_setup_fee"] = bool(specs.alert.get("require_setup_fee_zero"))
    return f


def describe_filter(f: dict[str, Any], specs: Specs, show_all: bool) -> str:
    parts = []
    if f.get("min_tier_rank"):
        names = [n for n, t in specs.tiers.items() if t["rank"] >= f["min_tier_rank"]]
        parts.append("Tier " + "/".join(sorted(names, key=lambda n: specs.tiers[n]["rank"])))
    if f.get("max_price") is not None:
        parts.append(f"max {f['max_price']:.0f} €")
    if f.get("min_ram") is not None:
        parts.append(f"min {f['min_ram']} GB RAM")
    if f.get("min_storage_class"):
        parts.append(f"min {f['min_storage_class'].upper()}")
    if f.get("min_sts") is not None:
        parts.append(f"min STS {f['min_sts']}")
    if f.get("no_setup_fee"):
        parts.append("ohne Setup-Gebühr")
    if f.get("provider"):  # nur vom Bot gesetzt, die CLI hat dafür --provider
        parts.append(f"nur {f['provider']}")
    if show_all:
        parts.append("inkl. disqualifizierter")
    return f"Filter ({f.get('_source', 'CLI')}): " + (", ".join(parts) if parts else "keiner")


def passes(offer: Offer, f: dict[str, Any], specs: Specs, gross: bool, include_ip: bool,
           show_all: bool) -> bool:
    if offer.disqualified and not show_all:
        return False

    price = offer.price(gross, include_ip)
    if f.get("max_price") is not None and price > f["max_price"]:
        return False
    if f.get("min_ram") is not None and offer.ram_gb < f["min_ram"]:
        return False
    if f.get("min_storage_class") and specs.storage_rank(offer.storage_class) < specs.storage_rank(
        f["min_storage_class"]
    ):
        return False
    if f.get("min_sts") is not None:
        sts = offer.cpu["single_thread_score"] if offer.cpu else 0
        if sts < f["min_sts"]:
            return False
    if f.get("no_setup_fee") and offer.setup_net > 0:
        return False
    if f.get("min_tier_rank") is not None:
        rank = specs.tiers[offer.tier]["rank"] if offer.tier else 0
        if rank < f["min_tier_rank"]:
            return False
    return True


# --------------------------------------------------------------------------- #
# Bibliotheks-API (wird vom Discord-Bot in bot.py genutzt)
# --------------------------------------------------------------------------- #

ALL_PROVIDERS = ("hetzner", "pph")


def load_specs(path: str | Path = DEFAULT_SPECS) -> Specs:
    return Specs(json.loads(Path(path).read_text(encoding="utf-8")))


def fetch_all(providers: Iterable[str] = ALL_PROVIDERS,
              on_error: Any = None) -> list[Offer]:
    """Holt die Rohangebote aller angegebenen Anbieter.

    Fällt ein Anbieter aus, läuft der Rest weiter; der Fehler geht an
    on_error(provider, exception), sofern gesetzt.
    """
    offers: list[Offer] = []
    for src in providers:
        try:
            offers.extend(fetch_hetzner() if src == "hetzner" else fetch_pph())
        except RuntimeError as err:
            if on_error:
                on_error(src, err)
            else:
                raise
    return offers


def evaluate_all(offers: Iterable[Offer], specs: Specs, gross: bool, include_ip: bool,
                 tier_price_cap: bool = True) -> None:
    for o in offers:
        evaluate(o, specs, gross, include_ip, tier_price_cap=tier_price_cap)


def filter_offers(offers: Iterable[Offer], f: dict[str, Any], specs: Specs,
                  gross: bool, include_ip: bool, show_all: bool = False) -> list[Offer]:
    """Filtert und sortiert (bester Score zuerst, bei Gleichstand günstiger zuerst)."""
    hits = [o for o in offers if passes(o, f, specs, gross, include_ip, show_all)]
    hits.sort(key=lambda o: (-o.score, o.price(gross, include_ip)))
    return hits


# --------------------------------------------------------------------------- #
# Ausgabe
# --------------------------------------------------------------------------- #

TIER_MARK = {"best": "***", "recommended": "**", "minimum": "*"}


def fmt_countdown(ts: int | None) -> str:
    if not ts:
        return ""
    secs = int(ts - time.time())
    if secs <= 0:
        return "jederzeit"
    h, m = divmod(secs // 60, 60)
    return f"in {h}h{m:02d}m" if h else f"in {m}m"


def print_table(offers: list[Offer], specs: Specs, gross: bool, include_ip: bool) -> None:
    if not offers:
        print("\nKeine Angebote entsprechen den Kriterien.")
        print("Tipp: --tier minimum, --max-price 80 oder --all zum Lockern.\n")
        return

    label = "Brutto" if gross else "Netto"
    header = (
        f"{'Anb.':<8}{'ID':<10}{'CPU':<24}{'STS':>5}{'C/T':>8}{'RAM':>6}"
        f"{'Storage':>16}{'DC':>11}{'EUR/M':>9}{'Setup':>7}{'Tier':>17}{'Score':>7}"
    )
    print()
    print(header)
    print("-" * len(header))

    for o in offers:
        cpu_name = (o.cpu["name"] if o.cpu else o.cpu_raw)[:23]
        sts = str(o.cpu["single_thread_score"]) if o.cpu else "?"
        ct = f"{o.cpu['cores']}/{o.cpu['threads']}" if o.cpu else "?"
        storage = f"{o.storage_gb} GB {o.storage_class.upper()}"
        tier = f"{TIER_MARK.get(o.tier, '')} {o.tier}" if o.tier else ("-" if not o.disqualified else "DISQ")
        print(
            f"{o.provider:<8}{o.offer_id:<10}{cpu_name:<24}{sts:>5}{ct:>8}"
            f"{str(o.ram_gb) + 'G':>6}{storage:>16}{o.datacenter[:10]:>11}"
            f"{o.price(gross, include_ip):>9.2f}{o.setup_net:>7.0f}{tier:>17}{o.score:>7.1f}"
        )

    print("-" * len(header))
    print(f"{len(offers)} Angebote  |  Preise in EUR/Monat ({label}"
          f"{', inkl. IPv4' if include_ip else ''})")
    print()

    best = offers[0]
    print("Top-Treffer:")
    print(f"  {best.provider.upper()} #{best.offer_id} - {best.cpu['name'] if best.cpu else best.cpu_raw}")
    print(f"  {best.ram_gb} GB RAM | {' + '.join(best.disks) if best.disks else best.storage_class.upper()}"
          f" | {best.datacenter}")
    print(f"  {best.price(gross, include_ip):.2f} EUR/Monat ({label}), Setup {best.setup_net:.0f} EUR"
          f" | Tier: {best.tier or '-'} | Score {best.score}")
    if best.reduce_next_ts:
        print(f"  Nächste Preissenkung: {fmt_countdown(best.reduce_next_ts)}")
    print(f"  {best.url}")
    print()


def print_unknown_cpus(offers: list[Offer]) -> None:
    unknown: dict[str, int] = {}
    for o in offers:
        if o.cpu is None and o.cpu_raw:
            unknown[o.cpu_raw] = unknown.get(o.cpu_raw, 0) + 1
    if not unknown:
        return
    print("CPUs ohne Eintrag in cpu_reference (werden nicht bewertet).")
    print("Vorlage zum Einfügen in server_specs.json -> cpu_reference:")
    for name, count in sorted(unknown.items(), key=lambda kv: -kv[1]):
        slug = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        stub = {
            "match": [slug], "name": name, "cores": 0, "threads": 0,
            "boost_ghz": 0.0, "l3_mb": 0, "single_thread_score": 0, "hybrid": False,
        }
        print(f"  {count:>3}x  {json.dumps(stub, ensure_ascii=False)},")
    print()


def export_json(offers: list[Offer], path: Path, gross: bool, include_ip: bool) -> None:
    rows = []
    for o in offers:
        row = dataclasses.asdict(o)
        row["price_shown"] = o.price(gross, include_ip)
        row["price_mode"] = "gross" if gross else "net"
        rows.append(row)
    path.write_text(
        json.dumps(
            {"fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "offers": rows},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"JSON geschrieben: {path}")


def export_csv(offers: list[Offer], path: Path, gross: bool, include_ip: bool) -> None:
    cols = ["provider", "offer_id", "cpu", "sts", "cores", "threads", "ram_gb",
            "storage_gb", "storage_class", "datacenter", "price", "setup", "tier", "score", "url"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cols)
        for o in offers:
            w.writerow([
                o.provider, o.offer_id, o.cpu["name"] if o.cpu else o.cpu_raw,
                o.cpu["single_thread_score"] if o.cpu else "",
                o.cpu["cores"] if o.cpu else "", o.cpu["threads"] if o.cpu else "",
                o.ram_gb, o.storage_gb, o.storage_class, o.datacenter,
                f"{o.price(gross, include_ip):.2f}", f"{o.setup_net:.2f}",
                o.tier or "", o.score, o.url,
            ])
    print(f"CSV geschrieben: {path}")


# --------------------------------------------------------------------------- #
# Watch-Modus: Zustand vergleichen
# --------------------------------------------------------------------------- #


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def diff_state(offers: list[Offer], state: dict[str, Any], gross: bool, include_ip: bool
               ) -> tuple[list[str], dict[str, Any]]:
    """Vergleicht mit dem letzten Lauf und gibt Meldungen + neuen Zustand zurück."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    new_state: dict[str, Any] = {}
    messages: list[str] = []

    for o in offers:
        price = o.price(gross, include_ip)
        prev = state.get(o.key)
        new_state[o.key] = {"price": price, "first_seen": (prev or {}).get("first_seen", now)}

        cpu_name = o.cpu["name"] if o.cpu else o.cpu_raw
        if prev is None:
            messages.append(
                f"NEU  {o.provider.upper()} #{o.offer_id} | {cpu_name} | {o.ram_gb} GB | "
                f"{o.storage_gb} GB {o.storage_class.upper()} | {price:.2f} EUR | "
                f"Tier {o.tier or '-'} | Score {o.score}"
            )
        elif price < prev["price"] - 0.005:
            messages.append(
                f"GÜNSTIGER  {o.provider.upper()} #{o.offer_id} | {cpu_name} | "
                f"{prev['price']:.2f} -> {price:.2f} EUR"
            )

    for key, prev in state.items():
        if key not in new_state:
            messages.append(f"WEG  {key} (zuletzt {prev.get('price', 0):.2f} EUR)")

    return messages, new_state


# --------------------------------------------------------------------------- #
# Ablauf
# --------------------------------------------------------------------------- #


def collect(args: argparse.Namespace, specs: Specs) -> tuple[list[Offer], list[Offer]]:
    """Holt, bewertet und filtert. Gibt (gefiltert, alle) zurück."""
    sources = ALL_PROVIDERS if args.provider == "all" else (args.provider,)

    def report(src: str, err: Exception) -> None:
        print(f"  {src:<8} FEHLER: {err}", file=sys.stderr)

    raw = fetch_all(sources, on_error=report)
    for src in sources:
        print(f"  {src:<8} {sum(1 for o in raw if o.provider == src):>4} Angebote geladen")

    evaluate_all(raw, specs, args.gross, args.include_ip,
                 tier_price_cap=not args.ignore_tier_price)

    f = build_filter(args, specs)
    print("  " + describe_filter(f, specs, args.all))
    hits = filter_offers(raw, f, specs, args.gross, args.include_ip, args.all)
    return hits, raw


def run_once(args: argparse.Namespace, specs: Specs) -> list[Offer]:
    print(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] Abruf ...")
    hits, raw = collect(args, specs)

    if args.limit:
        hits = hits[: args.limit]

    print_table(hits, specs, args.gross, args.include_ip)
    if args.unknown_cpus:
        print_unknown_cpus(raw)
    if args.json:
        export_json(hits, Path(args.json), args.gross, args.include_ip)
    if args.csv:
        export_csv(hits, Path(args.csv), args.gross, args.include_ip)
    return hits


def main() -> int:
    # Windows setzt stdout beim Umleiten in eine Datei oder Pipe auf cp1252,
    # woran jeder Umlaut scheitert. UTF-8 erzwingen, bevor irgendetwas läuft.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(
        description="Serverangebote bei Hetzner Serverbörse und Prepaid-Hoster prüfen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Beispiele:")[-1],
    )
    p.add_argument("--specs", default=str(DEFAULT_SPECS), help="Pfad zu server_specs.json")
    p.add_argument("--provider", choices=["all", "hetzner", "pph"], default="all")

    g = p.add_argument_group("Filter (überschreiben das alert_profile)")
    g.add_argument("--all", action="store_true",
                   help="Alle Angebote zeigen, auch disqualifizierte (kein Alert-Profil)")
    g.add_argument("--tier", choices=["minimum", "recommended", "best"],
                   help="Nur Angebote, die mindestens diesen Tier erreichen")
    g.add_argument("--max-price", type=float, help="Maximaler Monatspreis in EUR")
    g.add_argument("--min-ram", type=int, help="Minimaler Gesamt-RAM in GB")
    g.add_argument("--min-storage-class", choices=["hdd", "ssd", "nvme"])
    g.add_argument("--min-sts", type=int, help="Minimaler Single-Thread-Score")
    g.add_argument("--setup-fee-ok", action="store_true", help="Setup-Gebühr erlauben")
    g.add_argument("--ignore-tier-price", action="store_true",
                   help="Tier nur nach Hardware bestimmen, price_eur_month_max des Tiers ignorieren")
    g.add_argument("--limit", type=int, default=25, help="Maximal N Zeilen (0 = alle)")

    o = p.add_argument_group("Preisdarstellung")
    o.add_argument("--gross", action="store_true", help="Preise inkl. 19%% USt. anzeigen")
    o.add_argument("--include-ip", action="store_true",
                   help="Hetzner-IPv4-Monatspreis in den Preis einrechnen")

    e = p.add_argument_group("Export / Überwachung")
    e.add_argument("--json", help="Treffer als JSON schreiben")
    e.add_argument("--csv", help="Treffer als CSV (Semikolon) schreiben")
    e.add_argument("--unknown-cpus", action="store_true",
                   help="CPUs auflisten, die in cpu_reference fehlen")
    e.add_argument("--watch", type=int, metavar="SEK",
                   help="Alle SEK Sekunden erneut prüfen und Änderungen melden")
    e.add_argument("--state", default=str(DEFAULT_STATE), help="Zustandsdatei für --watch")
    e.add_argument("--webhook", help="Discord-kompatible Webhook-URL für Meldungen")

    args = p.parse_args()

    specs_path = Path(args.specs)
    if not specs_path.exists():
        print(f"server_specs.json nicht gefunden: {specs_path}", file=sys.stderr)
        return 2
    specs = Specs(json.loads(specs_path.read_text(encoding="utf-8")))

    if not args.watch:
        hits = run_once(args, specs)
        return 0 if hits else 1

    state_path = Path(args.state)
    state = load_state(state_path)
    print(f"Watch-Modus: alle {args.watch}s, Zustand in {state_path}. Abbruch mit Strg+C.")
    try:
        while True:
            hits = run_once(args, specs)
            messages, state = diff_state(hits, state, args.gross, args.include_ip)
            if messages:
                print("Änderungen seit dem letzten Lauf:")
                for m in messages:
                    print("  " + m)
                if args.webhook:
                    post_webhook(args.webhook, "**Serversuche**\n" + "\n".join(messages))
            else:
                print("Keine Änderungen.")
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\nBeendet.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
