# Installation auf einem Linux-Server

Schritt für Schritt vom leeren Server bis zum laufenden Bot. Getestet gegen
Debian 12/13 und Ubuntu 22.04/24.04; auf anderen Distributionen ändern sich nur
die Paketnamen in Schritt 2.

Wenn du nur wissen willst, *was* der Bot tut, steht das in der
[README](README.md). Hier geht es allein ums Aufsetzen.

---

## Was du brauchst

| | |
|---|---|
| Server | Beliebiger VPS oder Root-Server. 256 MB freier RAM reichen — die systemd-Unit deckelt den Bot genau darauf. |
| Netz | Nur **ausgehend** HTTPS (443). Der Bot öffnet keinen Port, es sind **keine** Firewall-Regeln nötig. |
| Python | 3.11 oder neuer. Debian 12 bringt 3.11 mit, Ubuntu 24.04 bringt 3.12 mit. Entwickelt und getestet wurde auf 3.13. |
| Zugang | Ein Konto mit `sudo` auf dem Server. |
| Discord | Ein Server, auf dem du „Server verwalten“ darfst. |

Der Bot braucht **keine** Datenbank, keinen Webserver und keinen Reverse Proxy.
Die gesamte Historie liegt in einer SQLite-Datei neben dem Skript.

---

## 1. Discord-Anwendung anlegen

Das machst du im Browser, nicht auf dem Server.

1. <https://discord.com/developers/applications> öffnen → **New Application**,
   Name vergeben (der taucht später als Bot-Name auf).
2. Links **Bot** → **Reset Token** → Token kopieren. **Diesen Wert siehst du nur
   einmal.** Er kommt gleich in die `.env`. Wer ihn hat, steuert deinen Bot —
   also nicht in Chats posten und nicht ins Git.
3. Auf derselben Seite die drei *Privileged Gateway Intents* **aus**lassen. Der
   Bot liest keine Nachrichten, er reagiert nur auf Slash-Commands.
4. Links **OAuth2** → **URL Generator**:
   - Scopes: `bot` und `applications.commands`
   - Bot Permissions: `Send Messages` und `Embed Links`
   - Die erzeugte URL unten öffnen und den Bot auf deinen Server einladen.

Zum Schluss noch die **Server-ID**: In Discord unter *Einstellungen →
Erweitert → Entwicklermodus* einschalten, dann Rechtsklick auf deinen Server →
**Server-ID kopieren**. Ohne diese ID registriert Discord die Slash-Commands
global, und das dauert bis zu einer Stunde. Mit ID sind sie sofort da.

Genauso holst du dir per Rechtsklick auf dich selbst deine **Benutzer-ID** —
die brauchst du für `DEFAULT_MENTION`, damit der Bot dich bei Treffern anpingt.

> Prüf am Ende noch, ob der Bot im Zielkanal überhaupt schreiben darf. Ein
> Kanal mit eingeschränkten Rechten überstimmt die Rechte aus der Einladung.

---

## 2. Pakete installieren

```bash
sudo apt update
sudo apt install -y python3 python3-venv rsync sqlite3
```

`sqlite3` ist nicht zum Betrieb nötig — Python bringt SQLite mit. Du brauchst
das Kommandozeilenwerkzeug nur für die Sicherungen in Schritt 9.

Kurz gegenprüfen:

```bash
python3 --version        # muss 3.11 oder höher sein
```

---

## 3. Dateien auf den Server bringen

### Variante A: vom Windows-Rechner

`tar` und `scp` sind in Windows 10/11 enthalten, du brauchst nichts zu
installieren. In PowerShell:

```powershell
cd C:\Users\schne\Desktop
tar -czf serversuche.tar.gz --exclude=.venv --exclude=__pycache__ --exclude="*.db*" --exclude=.env Serversuche
scp serversuche.tar.gz benutzer@dein-server:/tmp/
```

Ausgeschlossen sind mit Absicht:

- `.venv` — die virtuelle Umgebung ist an Windows gebunden und wird auf dem
  Server neu gebaut.
- `*.db*` — die Historie gehört dem Server; die lokale Datei würde ihn nur mit
  veralteten Ständen füttern.
- `.env` — das Token trägst du gleich direkt auf dem Server ein, statt es
  zusätzlich über die Leitung zu schicken.

### Variante B: aus einem Git-Repository

```bash
sudo apt install -y git
git clone <deine-repo-url> /tmp/Serversuche
```

---

## 4. Dienstnutzer und Verzeichnis anlegen

Der Bot läuft unter einem eigenen Konto ohne Login-Shell. Fällt er einem
Angreifer in die Hände, ist der Schaden auf sein eigenes Verzeichnis begrenzt.

```bash
sudo useradd --system --home /opt/serversuche --shell /usr/sbin/nologin serversuche
sudo mkdir -p /opt/serversuche
```

Dateien an ihren Platz (bei Variante A vorher auspacken):

```bash
tar -xzf /tmp/serversuche.tar.gz -C /tmp
sudo rsync -a /tmp/Serversuche/ /opt/serversuche/
```

---

## 5. Virtuelle Umgebung und Abhängigkeit

```bash
cd /opt/serversuche
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt
```

Installiert wird genau ein Paket: `discord.py`. `serversuche.py` selbst kommt
mit der Standardbibliothek aus.

---

## 6. Konfiguration eintragen

```bash
sudo cp /opt/serversuche/config.example.env /opt/serversuche/.env
sudo nano /opt/serversuche/.env
```

Mindestens diese drei Zeilen ausfüllen:

```ini
DISCORD_TOKEN=das_token_aus_schritt_1
DISCORD_GUILD_ID=deine_server_id
DEFAULT_MENTION=deine_benutzer_id
```

`DEFAULT_MENTION` darf leer bleiben — dann pingt der Bot nur, wo du beim
Anlegen einer Überwachung ausdrücklich jemanden über `erwähnen:` gesetzt hast.
Eine reine Zahl liest der Bot als Benutzer-ID; eine Rolle musst du als
`<@&123456789>` ausschreiben. Steht dort etwas, das keine Erwähnung sein kann,
startet der Bot gar nicht erst und sagt dir das — besser als monatelang stumm
Text statt eines Pings zu schreiben.

Alles Weitere (Poll-Intervall, Alert-Schwellen, `PRICE_MODE`) ist in der Datei
kommentiert und hat brauchbare Voreinstellungen.

Jetzt Rechte setzen — **wichtig**, die Datei enthält das Token:

```bash
sudo chown -R serversuche:serversuche /opt/serversuche
sudo chmod 600 /opt/serversuche/.env
```

---

## 7. Testlauf im Vordergrund

Erst von Hand starten, bevor systemd ins Spiel kommt. Fehler siehst du so
direkt statt im Journal.

```bash
sudo -u serversuche /opt/serversuche/.venv/bin/python /opt/serversuche/bot.py
```

Erwartete Ausgabe, sinngemäß:

```
INFO    serversuche.bot: Angemeldet als Serversuche#1234
INFO    serversuche.bot: Poll: 296 Angebote, 296 neu, 0 Preisänderungen, 0 verschwunden
```

Wenn das steht: mit `Strg+C` beenden. Läuft es nicht, hilft
[Schritt 10](#10-fehlersuche).

---

## 8. Als Dienst einrichten

```bash
sudo cp /opt/serversuche/serversuche-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now serversuche-bot
```

Zustand ansehen:

```bash
systemctl status serversuche-bot
journalctl -u serversuche-bot -f          # Logs mitlesen, Strg+C zum Beenden
```

Die mitgelieferte Unit ist abgesichert: `ProtectSystem=strict` macht das
gesamte Dateisystem schreibgeschützt bis auf `/opt/serversuche`,
`ProtectHome=true` blendet `/home` aus, `RestrictAddressFamilies` lässt nur
IPv4/IPv6 zu, und `MemoryMax=256M` deckelt den Speicher. Bei einem Absturz
startet der Dienst nach 30 Sekunden neu — aber höchstens fünfmal in fünf
Minuten, damit eine kaputte Konfiguration nicht in einer Neustartschleife endet.

### Funktionsprüfung in Discord

Im Zielkanal nacheinander:

| Befehl | Was du sehen solltest |
|---|---|
| `/hilfe` | Übersicht mit deinen Einstufungen aus `server_specs.json` |
| `/status` | „🟢 Alles läuft“, Anzahl Angebote, Standard-Ping |
| `/suche` | Eine blätterbare Trefferliste mit Netto- und Bruttopreis |

Tauchen die Befehle nicht auf, ist meist `DISCORD_GUILD_ID` leer oder falsch.

---

## 9. Betrieb

### Alltagsbefehle

```bash
sudo systemctl restart serversuche-bot     # nach einer Änderung
sudo systemctl stop serversuche-bot        # anhalten
journalctl -u serversuche-bot -n 100       # letzte 100 Zeilen
journalctl -u serversuche-bot --since today
```

### Aktualisieren

```bash
sudo systemctl stop serversuche-bot
sudo rsync -a /tmp/Serversuche/ /opt/serversuche/ \
     --exclude .venv --exclude '*.db*' --exclude .env
sudo chown -R serversuche:serversuche /opt/serversuche
sudo .venv/bin/pip install -r /opt/serversuche/requirements.txt
sudo systemctl start serversuche-bot
```

Die `--exclude`-Angaben sind der ganze Trick: Datenbank und `.env` bleiben
stehen, nur der Code wird ersetzt.

### Sicherung

Die Datenbank läuft im WAL-Modus. Ein simples `cp` der `.db`-Datei kann deshalb
einen halben Schreibvorgang erwischen und unbrauchbar sein — die aktuellen
Daten stecken teilweise noch in `serversuche.db-wal`. Nimm stattdessen den
`.backup`-Befehl von SQLite, der ist dafür gemacht und läuft bei laufendem Bot:

```bash
sudo -u serversuche mkdir -p /opt/serversuche/backup
sudo -u serversuche sqlite3 /opt/serversuche/serversuche.db \
     ".backup '/opt/serversuche/backup/serversuche-$(date +%F).db'"
```

Als tägliche Aufgabe:

```bash
sudo crontab -e
```

```cron
30 4 * * * sudo -u serversuche sqlite3 /opt/serversuche/serversuche.db ".backup '/opt/serversuche/backup/serversuche-$(date +\%F).db'" && find /opt/serversuche/backup -name '*.db' -mtime +14 -delete
```

Das `%` muss in einer Crontab escaped werden (`\%`), sonst schneidet cron die
Zeile dort ab. Die zweite Hälfte räumt Sicherungen älter als 14 Tage weg.

### Platzbedarf

Nach dem ersten Poll etwa 250 KB. Ein Preispunkt entsteht nur bei tatsächlicher
Änderung, nicht pro Poll — bei stündlichen Hetzner-Senkungen sind das grob
100–300 Zeilen am Tag, also wenige MB im Jahr. `PRUNE_AFTER_DAYS` in der `.env`
räumt verschwundene Angebote zusätzlich ab.

---

## 10. Fehlersuche

| Symptom | Ursache und Abhilfe |
|---|---|
| `DISCORD_TOKEN ist nicht gesetzt` | `.env` fehlt, liegt nicht in `/opt/serversuche/` oder gehört nicht dem Dienstnutzer. `sudo ls -l /opt/serversuche/.env` |
| `discord.py fehlt` | Der Dienst startet ein anderes Python. `ExecStart` muss auf `/opt/serversuche/.venv/bin/python` zeigen. |
| `LoginFailure: Improper token` | Token abgeschnitten oder mit Anführungszeichen eingefügt. In den Developer-Einstellungen neu erzeugen. |
| Slash-Commands fehlen | `DISCORD_GUILD_ID` leer oder falsch → Registrierung erfolgt global und dauert bis zu einer Stunde. Oder der Scope `applications.commands` fehlte bei der Einladung: dann Bot rauswerfen und mit korrekter URL neu einladen. |
| Bot ist online, meldet aber nichts | `/status` prüfen. Steht dort „🔴 Poll-Schleife steht“, sagt `journalctl` warum. „🟠 Poll überfällig“ heißt meist, dass beide APIs nicht erreichbar sind. |
| `Forbidden (403)` im Journal | Der Bot darf im Kanal nicht schreiben. Kanalrechte prüfen. Bleibt der Kanal gesperrt oder gelöscht, schaltet der Bot die betroffene Überwachung von selbst ab und schreibt das ins Log. |
| Dienst startet dauernd neu | `journalctl -u serversuche-bot -n 50`. Nach fünf Fehlstarts in fünf Minuten bleibt er absichtlich stehen; nach der Korrektur mit `sudo systemctl reset-failed serversuche-bot` wieder freigeben. |
| `Unknown key name ... in section 'Service'` | Alte Fassung der Unit-Datei. `StartLimitBurst` und `StartLimitIntervalSec` gehören seit systemd 229/230 in den Abschnitt `[Unit]`; in `[Service]` werden sie ignoriert. Datei aus dem Projekt neu kopieren. |
| Prozess wird beendet, Log endet abrupt | Speicherdeckel erreicht. `MemoryMax=256M` in der Unit hochsetzen, `daemon-reload`, neu starten. |
| Umlaute im Log kaputt | `Environment=PYTHONIOENCODING=utf-8` in der Unit ergänzen. Betrifft nur die Anzeige, nicht die Daten. |

Für mehr Details im Log in der `.env`:

```ini
LOG_LEVEL=DEBUG
```

---

## 11. Deinstallation

```bash
sudo systemctl disable --now serversuche-bot
sudo rm /etc/systemd/system/serversuche-bot.service
sudo systemctl daemon-reload
sudo rm -rf /opt/serversuche
sudo userdel serversuche
```

Die Discord-Anwendung selbst löschst du unter
<https://discord.com/developers/applications> → *Delete Application*.
