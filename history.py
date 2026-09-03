"""
SQLite-Speicher für Angebots- und Preishistorie sowie Watchlists.

Bewusst schlank gehalten: pro Angebot wird nur dann eine Snapshot-Zeile
geschrieben, wenn sich der Preis geändert hat. Bei ~300 Angeboten und einem
Poll alle 5 Minuten bleibt die Datei dadurch dauerhaft im einstelligen
MB-Bereich, statt täglich 86.000 Zeilen anzusammeln.

Alle Aufrufe sind synchron. Die Abfragen laufen im einstelligen
Millisekundenbereich, deshalb blockieren sie den Event-Loop des Bots nicht
spürbar - ein Executor wäre hier reine Zeremonie.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Sequence

from serversuche import Offer

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    key            TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    offer_id       TEXT NOT NULL,
    cpu            TEXT,
    sts            INTEGER,
    ram_gb         INTEGER,
    storage_gb     INTEGER,
    storage_class  TEXT,
    datacenter     TEXT,
    tier           TEXT,
    score          REAL,
    url            TEXT,
    first_seen     INTEGER NOT NULL,
    last_seen      INTEGER NOT NULL,
    gone_at        INTEGER,
    first_price    REAL,
    last_price     REAL
);

CREATE TABLE IF NOT EXISTS prices (
    key   TEXT NOT NULL,
    ts    INTEGER NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (key, ts)
);
CREATE INDEX IF NOT EXISTS idx_prices_key ON prices (key);

CREATE TABLE IF NOT EXISTS watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    channel_id  INTEGER NOT NULL,
    creator_id  INTEGER,
    name        TEXT NOT NULL,
    filter_json TEXT NOT NULL,
    mention     TEXT,
    created_at  INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS watch_hits (
    watch_id     INTEGER NOT NULL,
    key          TEXT NOT NULL,
    price        REAL NOT NULL,
    notified_at  INTEGER NOT NULL,
    PRIMARY KEY (watch_id, key)
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL,
    channel_id   INTEGER NOT NULL,
    creator_id   INTEGER,
    target       REAL NOT NULL,
    mention      TEXT,
    created_at   INTEGER NOT NULL,
    closed_at    INTEGER,
    outcome      TEXT,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (active);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class WatchDiff(NamedTuple):
    """Was sich für einen Watch geändert hat, plus der Stand zum Festschreiben.

    fresh/cheaper/gone sind zum Anzeigen da, notified/gone_keys zum Schreiben.
    Getrennt, weil der Stand erst nach dem erfolgreichen Senden gelten darf.
    """

    fresh: list[tuple[Offer, float]]
    cheaper: list[tuple[Offer, float, float]]
    gone: list[sqlite3.Row]
    notified: list[tuple[str, float]]
    gone_keys: list[str]

    @property
    def has_news(self) -> bool:
        return bool(self.fresh or self.cheaper or self.gone_keys)


class History:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- Angebote / Preise ------------------------------------------------- #

    def record(self, offers: Sequence[Offer], prices: Sequence[float], ts: int | None = None,
               live_providers: Iterable[str] | None = None) -> dict[str, int]:
        """Schreibt einen Poll-Durchlauf. prices[i] gehört zu offers[i].

        live_providers grenzt ein, wessen Angebote überhaupt als verschwunden
        gelten dürfen. Ohne diese Grenze würde ein Abruffehler bei einem
        Anbieter dessen gesamten Bestand auf gone_at stempeln, obwohl er nur
        gerade nicht geantwortet hat.

        Gibt Zähler zurück: neu, preis_geändert, verschwunden.
        """
        ts = ts or int(time.time())
        seen: set[str] = set()
        stats = {"new": 0, "changed": 0, "gone": 0}

        for offer, price in zip(offers, prices):
            seen.add(offer.key)
            row = self.db.execute("SELECT last_price FROM offers WHERE key = ?", (offer.key,)).fetchone()
            cpu_name = offer.cpu["name"] if offer.cpu else offer.cpu_raw
            sts = offer.cpu["single_thread_score"] if offer.cpu else None

            if row is None:
                stats["new"] += 1
                self.db.execute(
                    """INSERT INTO offers (key, provider, offer_id, cpu, sts, ram_gb, storage_gb,
                           storage_class, datacenter, tier, score, url, first_seen, last_seen,
                           gone_at, first_price, last_price)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                    (offer.key, offer.provider, offer.offer_id, cpu_name, sts, offer.ram_gb,
                     offer.storage_gb, offer.storage_class, offer.datacenter, offer.tier,
                     offer.score, offer.url, ts, ts, price, price),
                )
            else:
                if abs(row["last_price"] - price) > 0.005:
                    stats["changed"] += 1
                # url wird mitgeschrieben, damit sich eine geänderte Link-Vorlage
                # beim nächsten Poll von selbst auf die Bestandszeilen durchsetzt.
                self.db.execute(
                    """UPDATE offers SET last_seen = ?, last_price = ?, tier = ?, score = ?,
                           url = ?, gone_at = NULL WHERE key = ?""",
                    (ts, price, offer.tier, offer.score, offer.url, offer.key),
                )

            # Snapshot nur bei Preisänderung (bzw. beim ersten Sehen).
            if row is None or abs(row["last_price"] - price) > 0.005:
                self.db.execute("INSERT OR REPLACE INTO prices (key, ts, price) VALUES (?,?,?)",
                                (offer.key, ts, price))

        live = None if live_providers is None else tuple(live_providers)
        if seen and live != ():
            marks = ",".join("?" * len(seen))
            q = f"UPDATE offers SET gone_at = ? WHERE gone_at IS NULL AND key NOT IN ({marks})"
            params: tuple = (ts, *seen)
            if live is not None:
                q += f" AND provider IN ({','.join('?' * len(live))})"
                params += live
            cur = self.db.execute(q, params)
            stats["gone"] = cur.rowcount or 0

        self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('last_poll', ?)", (str(ts),))
        self.db.commit()
        return stats

    def price_series(self, key: str, limit: int = 200) -> list[tuple[int, float]]:
        rows = self.db.execute(
            "SELECT ts, price FROM prices WHERE key = ? ORDER BY ts DESC LIMIT ?", (key, limit)
        ).fetchall()
        return [(r["ts"], r["price"]) for r in reversed(rows)]

    def offer_row(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM offers WHERE key = ?", (key,)).fetchone()

    def find_keys(self, offer_id: str) -> list[str]:
        """Findet Keys zu einer Angebots-ID - bei beiden Anbietern, falls vorhanden."""
        if ":" in offer_id:
            return [offer_id]
        rows = self.db.execute(
            "SELECT key FROM offers WHERE offer_id = ? ORDER BY provider", (offer_id,)
        ).fetchall()
        return [r["key"] for r in rows]

    def stats(self) -> dict[str, Any]:
        one = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        last = self.db.execute("SELECT v FROM meta WHERE k = 'last_poll'").fetchone()
        return {
            "offers_total": one("SELECT COUNT(*) FROM offers"),
            "offers_live": one("SELECT COUNT(*) FROM offers WHERE gone_at IS NULL"),
            "price_points": one("SELECT COUNT(*) FROM prices"),
            "watches": one("SELECT COUNT(*) FROM watches WHERE active = 1"),
            "last_poll": int(last["v"]) if last else None,
            # WAL-Datei mitzählen, sonst meldet der Bot direkt nach einem
            # Schreibvorgang eine viel zu kleine Datenbank.
            "db_bytes": sum(p.stat().st_size for p in
                            (self.path, self.path.with_name(self.path.name + "-wal"))
                            if p.exists()),
        }

    def prune(self, older_than_days: int = 60) -> int:
        """Entfernt verschwundene Angebote samt Preisreihe."""
        cutoff = int(time.time()) - older_than_days * 86400
        keys = [r["key"] for r in self.db.execute(
            "SELECT key FROM offers WHERE gone_at IS NOT NULL AND gone_at < ?", (cutoff,)).fetchall()]
        for key in keys:
            self.db.execute("DELETE FROM prices WHERE key = ?", (key,))
            self.db.execute("DELETE FROM offers WHERE key = ?", (key,))
            self.db.execute("DELETE FROM watch_hits WHERE key = ?", (key,))
        self.db.commit()
        return len(keys)

    # -- Watchlists --------------------------------------------------------- #

    def add_watch(self, guild_id: int | None, channel_id: int, creator_id: int,
                  name: str, filt: dict[str, Any], mention: str | None) -> int:
        cur = self.db.execute(
            """INSERT INTO watches (guild_id, channel_id, creator_id, name, filter_json,
                   mention, created_at, active) VALUES (?,?,?,?,?,?,?,1)""",
            (guild_id, channel_id, creator_id, name, json.dumps(filt), mention, int(time.time())),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def watches(self, channel_id: int | None = None) -> list[sqlite3.Row]:
        if channel_id is None:
            return self.db.execute("SELECT * FROM watches WHERE active = 1 ORDER BY id").fetchall()
        return self.db.execute(
            "SELECT * FROM watches WHERE active = 1 AND channel_id = ? ORDER BY id", (channel_id,)
        ).fetchall()

    def get_watch(self, watch_id: int, channel_id: int | None = None) -> sqlite3.Row | None:
        q = "SELECT * FROM watches WHERE id = ? AND active = 1"
        params: tuple = (watch_id,)
        if channel_id is not None:
            q += " AND channel_id = ?"
            params += (channel_id,)
        return self.db.execute(q, params).fetchone()

    def update_watch(self, watch_id: int, *, name: str | None = None,
                     filt: dict[str, Any] | None = None,
                     mention: str | None = None, clear_mention: bool = False) -> bool:
        """Ändert einzelne Felder. Nicht übergebene Felder bleiben, wie sie sind."""
        sets, params = [], []
        if name is not None:
            sets.append("name = ?"); params.append(name)
        if filt is not None:
            sets.append("filter_json = ?"); params.append(json.dumps(filt))
        if clear_mention:
            sets.append("mention = NULL")
        elif mention is not None:
            sets.append("mention = ?"); params.append(mention)
        if not sets:
            return False
        cur = self.db.execute(
            f"UPDATE watches SET {', '.join(sets)} WHERE id = ? AND active = 1",
            (*params, watch_id),
        )
        self.db.commit()
        return (cur.rowcount or 0) > 0

    def remove_watch(self, watch_id: int, channel_id: int | None = None) -> bool:
        q = "UPDATE watches SET active = 0 WHERE id = ? AND active = 1"
        params: tuple = (watch_id,)
        if channel_id is not None:
            q += " AND channel_id = ?"
            params += (channel_id,)
        cur = self.db.execute(q, params)
        self.db.commit()
        return (cur.rowcount or 0) > 0

    def diff_watch(self, watch_id: int, offers: Sequence[Offer], prices: Sequence[float],
                   drop_pct: float = 3.0, drop_abs: float = 2.0,
                   live_providers: Iterable[str] | None = None) -> WatchDiff:
        """Vergleicht die aktuellen Treffer eines Watches mit dem letzten Stand.

        Meldet einen Preisrutsch erst ab drop_pct Prozent ODER drop_abs Euro,
        damit die 1-Cent-Schritte der Hetzner-Auktion keinen Spam erzeugen.

        Schreibt bewusst nichts: der neue Stand wird erst mit
        commit_watch_diff() festgeschrieben, wenn die Meldung zugestellt ist.
        Andernfalls gälten die Treffer nach einem Sendefehler als gemeldet und
        wären für immer verloren.
        """
        live = None if live_providers is None else set(live_providers)
        known = {r["key"]: r["price"] for r in self.db.execute(
            "SELECT key, price FROM watch_hits WHERE watch_id = ?", (watch_id,)).fetchall()}

        fresh: list[tuple[Offer, float]] = []
        cheaper: list[tuple[Offer, float, float]] = []
        notified: list[tuple[str, float]] = []
        current: set[str] = set()

        for offer, price in zip(offers, prices):
            current.add(offer.key)
            prev = known.get(offer.key)
            if prev is None:
                fresh.append((offer, price))
            else:
                drop = prev - price
                if drop >= drop_abs or (prev > 0 and drop / prev * 100 >= drop_pct):
                    cheaper.append((offer, prev, price))
                else:
                    # Kein meldenswerter Unterschied: der alte Preis bleibt als
                    # Bezugspunkt stehen, damit sich viele kleine Schritte zu
                    # einem meldbaren Rutsch summieren können.
                    continue
            notified.append((offer.key, price))

        # Ein Anbieter, der gerade nicht geantwortet hat, ist nicht verschwunden.
        gone_keys = [k for k in known
                     if k not in current and (live is None or k.split(":", 1)[0] in live)]
        gone = [row for row in (self.offer_row(k) for k in gone_keys) if row is not None]

        return WatchDiff(fresh, cheaper, gone, notified, gone_keys)

    def commit_watch_diff(self, watch_id: int, diff: WatchDiff) -> None:
        """Schreibt den Stand fest. Erst aufrufen, wenn die Meldung raus ist."""
        ts = int(time.time())
        for key, price in diff.notified:
            self.db.execute(
                "INSERT OR REPLACE INTO watch_hits (watch_id, key, price, notified_at) VALUES (?,?,?,?)",
                (watch_id, key, price, ts),
            )
        for key in diff.gone_keys:
            self.db.execute("DELETE FROM watch_hits WHERE watch_id = ? AND key = ?", (watch_id, key))
        self.db.commit()

    # -- Zielpreis-Alarme ---------------------------------------------------- #

    def add_alert(self, key: str, channel_id: int, creator_id: int, target: float,
                  mention: str | None) -> int:
        cur = self.db.execute(
            """INSERT INTO alerts (key, channel_id, creator_id, target, mention,
                   created_at, active) VALUES (?,?,?,?,?,?,1)""",
            (key, channel_id, creator_id, target, mention, int(time.time())),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def alerts(self, channel_id: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM alerts WHERE active = 1"
        params: tuple = ()
        if channel_id is not None:
            q += " AND channel_id = ?"
            params = (channel_id,)
        return self.db.execute(q + " ORDER BY id", params).fetchall()

    def alert_for(self, key: str, channel_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM alerts WHERE key = ? AND channel_id = ? AND active = 1",
            (key, channel_id)).fetchone()

    def close_alert(self, alert_id: int, outcome: str, channel_id: int | None = None) -> bool:
        """outcome: 'hit' (Ziel erreicht), 'gone' (Angebot weg), 'cancelled'."""
        q = "UPDATE alerts SET active = 0, closed_at = ?, outcome = ? WHERE id = ? AND active = 1"
        params: tuple = (int(time.time()), outcome, alert_id)
        if channel_id is not None:
            q += " AND channel_id = ?"
            params += (channel_id,)
        cur = self.db.execute(q, params)
        self.db.commit()
        return (cur.rowcount or 0) > 0

    def seed_watch(self, watch_id: int, offers: Sequence[Offer], prices: Sequence[float],
                   replace: bool = False) -> int:
        """Setzt die aktuellen Treffer als bekannt, ohne zu melden.

        Verhindert, dass ein frisch angelegter Watch sofort 30 'NEU'-Meldungen
        auslöst - interessant ist ab dann nur noch, was dazukommt.

        replace=True räumt vorher alles weg. Das ist nach einer Filteränderung
        nötig: sonst gelten Angebote, die nur nicht mehr zum neuen Filter
        passen, beim nächsten Poll fälschlich als 'nicht mehr verfügbar'.
        """
        ts = int(time.time())
        if replace:
            self.db.execute("DELETE FROM watch_hits WHERE watch_id = ?", (watch_id,))
        for offer, price in zip(offers, prices):
            self.db.execute(
                "INSERT OR REPLACE INTO watch_hits (watch_id, key, price, notified_at) VALUES (?,?,?,?)",
                (watch_id, offer.key, price, ts),
            )
        self.db.commit()
        return len(offers)


# --------------------------------------------------------------------------- #
# Analyse
# --------------------------------------------------------------------------- #


class Forecast(NamedTuple):
    points: int              # Anzahl Preisänderungen in der Reihe
    span_hours: float        # beobachteter Zeitraum
    rate_per_day: float      # €/Tag, negativ = fällt
    current: float           # letzter bekannter Preis
    eta_ts: int | None       # geschätzter Zeitpunkt für das Ziel
    reliable: bool           # ob die Schätzung überhaupt etwas taugt
    note: str


def forecast(series: Sequence[tuple[int, float]], target: float | None = None,
             now: int | None = None) -> Forecast:
    """Schätzt aus der Preisreihe, wann ein Zielpreis erreicht wird.

    Kleinste-Quadrate-Gerade über (Zeit, Preis). Das ist bewusst simpel: die
    Hetzner-Auktion senkt in unregelmäßigen Stufen, ein aufwendigeres Modell
    würde eine Genauigkeit vortäuschen, die die Daten nicht hergeben. Die
    Schätzung sagt "in welcher Größenordnung", nicht "auf die Minute".

    reliable=False heißt: anzeigen ja, darauf verlassen nein.
    """
    now = now or int(time.time())
    if not series:
        return Forecast(0, 0.0, 0.0, 0.0, None, False, "noch keine Daten")

    # price_series() liefert bereits aufsteigend; defensiv sortieren, damit eine
    # unsortierte Reihe nicht zu einer negativen Zeitspanne und damit zu
    # stillem Unsinn führt.
    series = sorted(series, key=lambda tp: tp[0])

    current = series[-1][1]
    span_h = (series[-1][0] - series[0][0]) / 3600

    if target is not None and current <= target + 0.005:
        return Forecast(len(series), span_h, 0.0, current, now, True, "Ziel bereits erreicht")
    if len(series) < 3:
        return Forecast(len(series), span_h, 0.0, current, None, False,
                        f"erst {len(series)} Preispunkt(e) - zu wenig für einen Trend")

    t0 = series[0][0]
    xs = [(t - t0) / 86400 for t, _ in series]        # Tage seit Beginn
    ys = [p for _, p in series]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    rate = 0.0 if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den

    if span_h < 2:
        return Forecast(n, span_h, rate, current, None, False,
                        "Beobachtung läuft erst kurz - Trend noch nicht belastbar")
    if rate > -0.05:
        return Forecast(n, span_h, rate, current, None, True,
                        "kein klarer Abwärtstrend")

    reliable = n >= 5 and span_h >= 12
    eta = None
    if target is not None:
        eta = int(now + (current - target) / -rate * 86400)
    return Forecast(n, span_h, rate, current, eta, reliable,
                    "belastbar" if reliable else
                    f"grobe Schätzung aus {n} Punkten über {span_h:.0f} h")


# --------------------------------------------------------------------------- #
# Darstellung
# --------------------------------------------------------------------------- #

SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: Iterable[float], width: int = 32) -> str:
    vals = list(values)
    if not vals:
        return ""
    if len(vals) > width:  # gleichmäßig ausdünnen, letzter Wert bleibt erhalten
        step = len(vals) / width
        vals = [vals[min(int(i * step), len(vals) - 1)] for i in range(width - 1)] + [vals[-1]]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return SPARK[0] * len(vals)
    return "".join(SPARK[min(int((v - lo) / (hi - lo) * (len(SPARK) - 1)), len(SPARK) - 1)] for v in vals)
