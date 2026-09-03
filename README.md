# Serversuche

Prüft Serverangebote bei **Hetzner Serverbörse** und **Prepaid-Hoster (PPH)** gegen die
Kriterien aus `server_specs.json` und sortiert sie nach dem dort definierten Score.

| Datei | Zweck |
|---|---|
| `serversuche.py` | CLI und Bibliothek. Nur Standardbibliothek, keine Installation nötig. |
| `bot.py` | Discord-Bot mit Slash-Commands und Hintergrund-Poll. Braucht `discord.py`. |
| `history.py` | SQLite-Speicher für Preishistorie und Watchlists. |
| `server_specs.json` | Deine Kriterien: Tiers, CPU-Referenz, Scoring, Alert-Profil. |
| `INSTALL.md` | Aufsetzen auf einem Linux-Server, von Discord-Token bis systemd. |

Getestet mit Python 3.13 und discord.py 2.7.

## Datenquellen

Beide Anbieter werden über die JSON-Endpunkte ihrer eigenen Frontends abgefragt —
kein HTML-Scraping:

| Anbieter | Endpunkt |
|---|---|
| Hetzner | `GET https://www.hetzner.com/_resources/app/data/app/live_data_sb.json` |
| PPH | `GET https://api.pph.sh/public/products/dedicated?page=N` (paginiert) |

Beides sind öffentliche, aber **undokumentierte** Endpunkte. Sie können sich ohne
Vorwarnung ändern; das Skript prüft die Struktur und bricht mit einer klaren
Meldung ab, statt stillschweigend Unsinn zu liefern.

> Hinweis: PPH verkauft im Dedicated-Bereich dieselben Hetzner-Auktionsserver
> weiter — die `serverid` ist identisch. Derselbe Server taucht daher zweimal
> auf, bei PPH mit Aufschlag und Setup-Gebühr. Der Vergleich ist damit direkt.

## Nutzung

```bash
python serversuche.py                      # Alert-Profil aus server_specs.json
python serversuche.py --tier recommended   # alles ab Tier "recommended"
python serversuche.py --all --limit 30     # alles, auch Disqualifiziertes
python serversuche.py --max-price 80 --min-storage-class nvme --min-sts 3300
python serversuche.py --gross --include-ip # Brutto inkl. Hetzner-IPv4
python serversuche.py --json treffer.json --csv treffer.csv
python serversuche.py --unknown-cpus       # CPUs, die in cpu_reference fehlen
```

### Dauerüberwachung

```bash
python serversuche.py --tier recommended --watch 300 \
       --webhook https://discord.com/api/webhooks/...
```

Prüft alle 5 Minuten, merkt sich den Stand in `state.json` und meldet nur
Änderungen: `NEU`, `GÜNSTIGER`, `WEG`. Der Webhook ist optional und
Discord-kompatibel.

> Auf Windows wird `stdout` beim Umleiten in eine Datei oder Pipe auf cp1252
> gesetzt, woran jeder Umlaut scheitern würde. `serversuche.py` stellt beim Start
> selbst auf UTF-8 um — `python serversuche.py > treffer.txt` funktioniert also.

## Filterlogik

- **Ohne Filter-Argument** gilt `alert_profile` aus `server_specs.json`.
- **Sobald ein Filter-Argument gesetzt ist**, zählt nur noch das explizit
  Angegebene. Sonst würde z. B. `--tier minimum` still am 60-EUR-Deckel des
  Alert-Profils scheitern.
- Alle Preisgrenzen beziehen sich auf die **angezeigte** Preisart — mit
  `--gross` also auf Bruttopreise.

## Bewertung

1. **Hard-Disqualifier** (`hard_disqualifiers`): nur-HDD, zu wenig RAM,
   Single-Thread-Score oder Boost-Takt unter der Grenze. Disqualifizierte
   Angebote erscheinen nur mit `--all`.
2. **Tier** (`tiers`): der höchste Tier, dessen Anforderungen komplett erfüllt
   sind. Der Preis-Deckel des Tiers zählt mit; `--ignore-tier-price` schaltet
   ihn ab, wenn nur die Hardware-Einstufung interessiert.
3. **Score** 0–100 nach `scoring_weights`, normalisiert auf
   Single-Thread-Score, L3-Cache (Deckel 64 MB), Storage-Klasse und RAM
   (Deckel 128 GB).

Die CPU-Daten kommen ausschließlich aus `cpu_reference` — die APIs liefern
weder Kernzahl noch Boost-Takt noch L3. Unbekannte CPUs werden als
disqualifiziert geführt; `--unknown-cpus` gibt fertige JSON-Bausteine zum
Einfügen aus.

---

# Discord-Bot

Pollt beide Börsen im Hintergrund, schreibt jede Preisänderung in eine SQLite-Historie
und meldet Treffer pro Watchlist in den Kanal, in dem sie angelegt wurde.

## Commands

| Command | Wirkung |
|---|---|
| `/suche [tier] [max_preis] [min_ram] [storage] [min_sts] [anbieter]` | Abfrage, blätterbares Ergebnis mit Aktualisieren-Knopf |
| `/verlauf server_id` | Preisverlauf: Sparkline, Tief/Hoch, Trend, Tiefstand-Hinweis |
| `/watch add name [filter…] [erwähnen]` | Dauerüberwachung für diesen Kanal anlegen — `erwähnen` überschreibt `DEFAULT_MENTION` |
| `/watch edit id [felder…] [zurücksetzen]` | Filter, Name oder Ping einer Überwachung ändern |
| `/watch list` / `/watch remove id` | Überwachungen anzeigen / entfernen |
| `/alarm setzen id zielpreis [erwähnen]` | Ping, sobald **dieser** Server unter deinen Preis fällt |
| `/alarm liste` / `/alarm entfernen id` | Alarme anzeigen / abbrechen |
| `/warum server_id` | Wie Score und Tier zustande kommen |
| `/status` | Zustand der Poll-Schleife, letzter Poll, Anzahl Angebote, Datenbankgröße |
| `/poll` | Sofortigen Durchlauf auslösen |
| `/hilfe` | Kurzübersicht inkl. deiner Einstufungen aus `server_specs.json` |

**Watch oder Alarm?** Ein Watch ist ein *Filter* über alle Angebote („zeig mir alles
ab Tier recommended unter 90 €“). Ein Alarm gilt einem *bestimmten* Server, den du
dir vorgemerkt hast („sag Bescheid, wenn #3062623 unter 90 € fällt“). Bei einer
fallenden Auktion ist das der übliche Ablauf: mit `/suche` einen Kandidaten finden,
mit `/warum` prüfen, ob er taugt, mit `/alarm` die Schmerzgrenze setzen.

Ein Alarm endet von selbst — entweder er löst aus oder das Angebot verschwindet
(auch dann bekommst du eine Nachricht).

## Ping

Jede Treffermeldung kann jemanden anpingen. Zwei Ebenen, die feinere gewinnt:

| Wo | Wirkung |
|---|---|
| `DEFAULT_MENTION` in der `.env` | Gilt für **alle** Watches und Alarme, die nichts Eigenes gesetzt haben |
| `erwähnen:` bei `/watch add`, `/watch edit`, `/alarm setzen` | Gilt nur für diese eine Überwachung und schlägt den Standard |

`DEFAULT_MENTION` nimmt eine Benutzer-ID (`123456789`, Rechtsklick → ID kopieren),
`<@123456789>`, eine Rolle als `<@&123456789>` oder `@here`. Eine nackte Zahl wird
als Benutzer gelesen — Rollen müssen ausgeschrieben werden, weil sich beides sonst
nicht unterscheiden lässt. Steht dort etwas, das keine Erwähnung sein kann, startet
der Bot gar nicht erst: eine falsche Zeichenkette würde sonst monatelang stumm als
Text statt als Ping in den Kanal geschrieben.

Wer tatsächlich gepingt wird, steht bei `/watch list`, `/status` und in der
Bestätigung von `/watch add` bzw. `/alarm setzen`. Die Erwähnung geht bewusst in
den Nachrichtentext und nicht ins Embed — Erwähnungen in Embeds lösen keine
Benachrichtigung aus.

## Preisprognose

Aus der aufgezeichneten Preisreihe wird per Kleinste-Quadrate-Gerade die Senkungsrate
geschätzt und daraus, wann dein Zielpreis erreicht sein dürfte:

```
📉 4,00 €/Tag günstiger · 🎯 176,00 € etwa in 5 Tagen
```

Bewusst simpel gehalten: die Hetzner-Auktion senkt in unregelmäßigen Stufen, ein
aufwendigeres Modell würde eine Genauigkeit vortäuschen, die die Daten nicht hergeben.
Die Schätzung sagt „in welcher Größenordnung“, nicht „auf die Minute“. Sie kennzeichnet
sich selbst als *(grob)*, solange weniger als 5 Preispunkte über mindestens 12 Stunden
vorliegen, und schweigt ganz, wenn kein klarer Abwärtstrend erkennbar ist.

Und unabhängig davon: ein Auktionsserver kann jederzeit weggekauft werden. Eine
Prognose sagt nichts über die Verfügbarkeit.

## `/warum` — Bewertung nachvollziehen

Zeigt für ein Angebot drei Dinge: welche Ausschlusskriterien greifen, welche
Anforderungen der nächsthöheren Einstufung erfüllt sind (✅/❌ je Kriterium), und wie
sich der Score aus den vier gewichteten Bestandteilen zusammensetzt.

Die Tier-Bedingungen und die Score-Rechnung stehen dafür an **einer** Stelle in
`serversuche.py` (`tier_checks`, `score_components`) — `evaluate()` entscheidet damit,
`/warum` erklärt damit. Zwei getrennte Formulierungen derselben Regel würden früher
oder später auseinanderlaufen und die Erklärung zur Lüge machen.

## Darstellung

Jedes Angebot erscheint als Karte statt als Monospace-Tabelle — Tabellen brechen
auf schmalen Handy-Displays um und werden unlesbar:

```
🥇 AMD Ryzen 7 7700 — HZ 3062623
> 104,00 €/Monat · 123,76 € brutto · 64 GB RAM · 2 TB ⚡ NVMe · 📍 Helsinki · ⏳ in 5 Std.
> ███████░░░ 71 · 8C/16T · 5.3 GHz · 32 MB L3
```

- 🥇 🥈 🥉 zeigen die Einstufung, ▫️ heißt „erfüllt keinen Tier vollständig“
- Die ID ist ein Link direkt zum Angebot, der Balken ist der Score
- **Netto und brutto stehen beide da** (19 % USt.), eine Setup-Gebühr ebenso.
  `PRICE_MODE` bestimmt nur, welcher der beiden fett vorne steht — und mit
  welchem gerechnet wird: Preisfilter, Zielpreise der Alarme und Tier-Deckel
  beziehen sich auf diese Preisart. Umstellen ändert also die Bedeutung
  bestehender Alarme.
- Preise im deutschen Format, Zeitangaben als Discord-Zeitstempel (passen sich
  der Zeitzone jedes Lesers an)
- Bei **null Treffern** zeigt `/suche` die günstigsten Angebote knapp über deinem
  Limit — hilfreicher als ein blankes „nichts gefunden“
- Blättern darf nur, wer den Befehl abgesetzt hat; nach 5 Minuten werden die
  Knöpfe deaktiviert

Ein frisch angelegter Watch übernimmt den aktuellen Stand als „bekannt“ und meldet
danach nur noch Neuzugänge sowie Preisrutsche ab `ALERT_DROP_ABS` Euro **oder**
`ALERT_DROP_PCT` Prozent. Ohne diese Schwelle würden die Cent-Schritte der
Hetzner-Auktion den Kanal fluten.

`/watch edit` ändert nur die Felder, die du angibst — der Rest bleibt stehen. Ein
Kriterium wieder loszuwerden geht über `zurücksetzen`. Zwei Sicherungen dabei:

- Eine Änderung, die **kein Kriterium** übrig lässt, wird verworfen — sonst würde
  die Überwachung jedes neue Angebot der Börse melden.
- Nach jeder Änderung wird der Trefferstand **komplett neu gesetzt**. Sonst würden
  Angebote, die nur nicht mehr zum neuen Filter passen, beim nächsten Poll
  fälschlich als „nicht mehr verfügbar“ gemeldet.

## Ausfallverhalten

Die drei Stellen, an denen ein Poll früher stillschweigend Schaden anrichten konnte:

**Ein Anbieter antwortet nicht.** `fetch_all` lässt den anderen weiterlaufen, die
Trefferliste ist dann aber unvollständig. Früher galt jedes fehlende Angebot als
verschwunden — hunderte „👋 Nicht mehr verfügbar“, geschlossene Alarme, und beim
nächsten Poll alles wieder als „✨ Neu“. `record()` und `diff_watch()` bekommen
jetzt die Liste der Anbieter, die tatsächlich geantwortet haben; nur deren
Angebote können überhaupt verschwinden. Alarme auf den ausgefallenen Anbieter
werden übersprungen statt beendet.

**Der Poll-Task stirbt.** discord.py beendet einen `tasks.loop` endgültig, sobald
eine Ausnahme durchschlägt, die nicht in seiner Reconnect-Liste steht — ein
`sqlite3.OperationalError` etwa. Der Bot bliebe online und würde nie wieder
pollen. Der Durchlauf ist deshalb komplett gekapselt, es gibt einen
`@poll_loop.error`-Handler, und `/status` prüft nicht mehr nur, *ob* je ein Poll
lief, sondern ob die Schleife läuft und der letzte Durchlauf nicht überfällig ist.

**Die Meldung kommt nicht an.** `diff_watch()` schreibt nichts mehr; der neue
Trefferstand wird mit `commit_watch_diff()` erst festgeschrieben, wenn
`channel.send` durch ist. Scheitert das Senden, bleibt alles offen und der
nächste Poll versucht es erneut. Ist der Kanal dauerhaft weg (404) oder gesperrt
(403), wird die Überwachung abgeschaltet statt alle fünf Minuten weiter
anzuklopfen — bei allem anderen wird wiederholt.

> Wichtig für die Rutsch-Erkennung: Bei einer Änderung unterhalb der Schwelle
> bleibt der **alte** Preis als Bezugspunkt stehen. Nur so summieren sich die
> Cent-Schritte der Hetzner-Auktion irgendwann zu einem meldbaren Rutsch.

## Installation

Discord-Anwendung anlegen, Dateien auf den Server, venv, `.env`, systemd,
Sicherungen und Fehlersuche: **[INSTALL.md](INSTALL.md)**.

Zum Ausprobieren auf dem eigenen Rechner reicht:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.env .env      # DISCORD_TOKEN und DISCORD_GUILD_ID eintragen
.venv/bin/python bot.py
```

## Ressourcenverbrauch

Gemessen mit 296 Angeboten:

- Ein Poll: ~3 s HTTP (läuft in einem Thread, blockiert den Bot nicht) + 6–9 ms SQLite
- Datenbank nach dem ersten Poll: ~250 KB
- Wachstum: ein Preispunkt **nur bei tatsächlicher Änderung**, nicht pro Poll.
  Bei stündlichen Hetzner-Senkungen sind das grob 100–300 Zeilen/Tag, also wenige
  MB im Jahr. `PRUNE_AFTER_DAYS` räumt verschwundene Angebote zusätzlich ab.

## Grenzen

- `storage_gb` ist die **rohe** Summe aller Platten, ohne RAID. 2× 512 GB im
  RAID 1 zählen als 1024 GB.
- Beide APIs liefern Nettopreise; `--gross` bzw. die Brutto-Angabe des Bots
  rechnet pauschal mit 19 % USt. Kein Reverse-Charge, kein anderer Satz — wer
  im EU-Ausland oder mit USt-IdNr. kauft, zahlt etwas anderes.
- Der Hetzner-IPv4-Monatspreis ist separat ausgewiesen und wird nur mit
  `--include-ip` bzw. `INCLUDE_IP=1` eingerechnet.
- Der Bot kennt nur Angebote, die er **seit seinem Start** gesehen hat. `/verlauf`
  liefert für ältere Angebote nichts, weil es keine rückwirkende Datenquelle gibt.
- Die SQLite-Verbindung ist an den Thread gebunden, der sie erzeugt hat. Alle
  Store-Aufrufe laufen deshalb inline im Event-Loop (bei gemessenen 6–9 ms
  unproblematisch) — sie dürfen nicht in `asyncio.to_thread` gewickelt werden.
