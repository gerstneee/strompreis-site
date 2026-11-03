#!/usr/bin/env python3
"""
Strom-Uhr: erzeugt täglich eine statische, besonders leicht lesbare Website
mit den deutschen Day-Ahead-Strompreisen (Gebotszone DE-LU) und veröffentlicht sie
per git push (GitHub Pages oder Cloudflare Pages).

Datenquelle : Energy-Charts API (Fraunhofer ISE), Daten: Bundesnetzagentur | SMARD.de, CC BY 4.0
              -> Quellenangabe ist Pflicht (steht im Footer, siehe FOOTER_SOURCE).
Abhängigk.  : pip install requests jinja2 tzdata   (Python >= 3.11)
Start       : python main.py [--no-push]

Seiten, die erzeugt werden (Ordner docs/):
  index.html                24-h-Uhr mit Zeiger auf "jetzt" + Tagesplan heute/morgen + Geräte-Fahrplan
  tag/<datum>.html          Tagesseite mit allen Stunden
  geraete/*.html            "Wann soll ich ...?" je Gerät
  rechner.html              Kostenrechner (läuft nur im Browser, keine Datenübertragung)
  tarifvergleich.html       Fester vs. dynamischer Tarif inkl. Rechner
  statistik.html            Farbkalender, Tageszeit- und Wochentags-Statistik
  wissen/*.html             Erklärseiten in einfacher Sprache
  archiv.html, impressum.html, datenschutz.html, 404.html, sitemap.xml, robots.txt
  assets/style.css, assets/app.js

Aufbau dieser Datei:
  1. CONFIG            4. ANALYSE         7. TEMPLATES
  2. LOGGING / LOCK    5. HTML-HELFER     8. ERKLÄRSEITEN
  3. FETCH             6. CSS und JS      9. RENDER / PUBLISH / MAIN
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from jinja2 import DictLoader, Environment, select_autoescape
from markupsafe import Markup

# ----------------------------------------------------------------------------
# 1. CONFIG
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"          # Cache der Rohdaten (ein JSON pro Tag)
OUT_DIR = BASE_DIR / "docs"           # GitHub Pages liefert den Ordner /docs aus
LOG_DIR = BASE_DIR / "logs"
LEGAL_DIR = BASE_DIR / "legal"        # impressum.html / datenschutz.html (HTML-Fragmente)
LOCK_FILE = BASE_DIR / ".run.lock"

SITE_URL = "https://gerstneee.github.io/strompreis-site"   # ohne Slash am Ende
SITE_NAME = "Strom-Uhr"
TZ = ZoneInfo("Europe/Berlin")
API_URL = "https://api.energy-charts.info/price"
USER_AGENT = f"{SITE_NAME}/2.0 (+{SITE_URL})"

BACKFILL_DAYS = 62        # so viele zurückliegende Tage werden (einmalig) nachgeladen (für Monatsrückblick)
STATS_DAYS = 30           # Zeitraum für Ampel-Schwellen und Statistik
ARCHIVE_MAX = 120         # so viele Tage maximal im Archiv
MWST = 1.19
DEFAULT_AUFSCHLAG = 18.0  # Beispielwert in ct/kWh netto (Netzentgelte + Steuern + Umlagen + Anbieter)

# Schrift: Atkinson Hyperlegible (SIL Open Font License 1.1), entwickelt vom Braille Institute
# für gute Lesbarkeit. Wird beim ersten Lauf EINMALIG heruntergeladen und danach selbst gehostet
# (kein Abruf bei Google durch Besucher -> datenschutzfreundlich). Klappt der Download nicht,
# nutzt die Seite automatisch Verdana / Systemschrift.
FONT_FILES = {
    "atkinson-400.woff2": [
        "https://cdn.jsdelivr.net/fontsource/fonts/atkinson-hyperlegible@latest/latin-400-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-400-normal.woff2",
    ],
    "atkinson-700.woff2": [
        "https://cdn.jsdelivr.net/fontsource/fonts/atkinson-hyperlegible@latest/latin-700-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-700-normal.woff2",
    ],
}

FOOTER_SOURCE = ("Datenquelle: Bundesnetzagentur | SMARD.de (CC BY 4.0), "
                 "bereitgestellt über Energy-Charts (Fraunhofer ISE).")

# Affiliate-Links: erst eintragen, wenn du beim Partnerprogramm angenommen bist.
# Leere Strings = die Box wird nicht angezeigt.
AFFILIATE = {
    "dyn_tarif": {"label": "Dynamische Stromtarife ansehen", "url": ""},
    "smart_plug": {"label": "Steckdose mit Verbrauchsmessung ansehen", "url": ""},
    "smart_meter": {"label": "Energiemonitor für den Zähler ansehen", "url": ""},
}

# Geräte: kwh = Verbrauch pro Durchlauf, hours = Laufzeit in ganzen Stunden, per_week = Beispielnutzung
DEVICES = [
    {"slug": "waschmaschine", "name": "Waschmaschine", "frage": "die Waschmaschine laufen lassen",
     "kwh": 1.0, "hours": 2, "per_week": 3},
    {"slug": "geschirrspueler", "name": "Geschirrspüler", "frage": "den Geschirrspüler starten",
     "kwh": 0.9, "hours": 3, "per_week": 5},
    {"slug": "waeschetrockner", "name": "Wäschetrockner", "frage": "den Wäschetrockner laufen lassen",
     "kwh": 1.5, "hours": 2, "per_week": 2},
    {"slug": "warmwasser", "name": "Warmwasserspeicher", "frage": "das Warmwasser aufheizen",
     "kwh": 4.0, "hours": 2, "per_week": 7},
    {"slug": "e-auto-wallbox", "name": "E-Auto (Wallbox, 11 kW)", "frage": "mein Elektroauto laden (Wallbox)",
     "kwh": 20.0, "hours": 2, "per_week": 2},
    {"slug": "e-auto-ladebox", "name": "E-Auto (kleine Ladebox, ca. 3,7 kW)",
     "frage": "mein Elektroauto laden (kleine Ladebox)",
     "kwh": 20.0, "hours": 6, "per_week": 2},
]

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
WD_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember"]
LEVEL_WORD = {"g": "günstig", "y": "normal", "r": "teuer"}


def de1(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def de2(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def de_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day:02d}.{d.month:02d}.{d.year}"


def short_date(d: date) -> str:
    return f"{WD_SHORT[d.weekday()]} {d.day:02d}.{d.month:02d}."


# ----------------------------------------------------------------------------
# 2. LOGGING / LOCK
# ----------------------------------------------------------------------------
log = logging.getLogger("strompreis")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "run.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    if sys.stdout is not None:            # unter pythonw.exe (Aufgabenplanung ohne Fenster) gibt es keine Konsole
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def acquire_lock(max_age_s: int = 1800) -> bool:
    """Verhindert parallele Läufe. Veraltete Locks (>30 min) werden übernommen."""
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < max_age_s:
        return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# 3. FETCH
# ----------------------------------------------------------------------------
HARD_FAILS = 0   # Anzahl endgültig gescheiterter Abrufe in diesem Lauf (API down?)


def to_local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def fetch_day(d: date) -> list[list[float]] | None:
    """Liefert [[unix_seconds, EUR/MWh], ...] für den lokalen Tag d oder None.
    Vollständige Tage werden gecacht; unvollständige (z. B. morgen vor 13 Uhr) nicht."""
    global HARD_FAILS
    DATA_DIR.mkdir(exist_ok=True)
    cache = DATA_DIR / f"{d.isoformat()}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    params = {"bzn": "DE-LU", "start": d.isoformat(), "end": (d + timedelta(days=1)).isoformat()}
    for attempt in range(1, 6):
        try:
            r = requests.get(API_URL, params=params, timeout=30, headers={"User-Agent": USER_AGENT})
            if r.status_code == 404:      # Tag noch nicht veröffentlicht: kein Fehler, kein Retry
                log.info("Tag %s noch nicht veröffentlicht (404)", d)
                return None
            if r.status_code == 429:      # Rate-Limit: länger warten, dann neu versuchen
                ra = str(r.headers.get("Retry-After", ""))
                wait = int(ra) if ra.isdigit() else 30
                log.warning("Rate-Limit (429) bei %s – warte %ds", d, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            time.sleep(1)                 # höflicher Abstand zwischen Anfragen
            js = r.json()
            pts = [
                [t, p]
                for t, p in zip(js.get("unix_seconds") or [], js.get("price") or [])
                if p is not None and to_local(t).date() == d
            ]
            hours = {t // 3600 for t, _ in pts}
            if len(hours) < 23:          # Tag unvollständig / noch nicht veröffentlicht
                log.info("Tag %s noch unvollständig (%d Stunden) – kein Cache", d, len(hours))
                return None
            cache.write_text(json.dumps(pts), encoding="utf-8")
            log.info("Tag %s geladen (%d Datenpunkte)", d, len(pts))
            return pts
        except (requests.RequestException, ValueError) as exc:
            wait = 2 ** attempt
            log.warning("Abruf %s fehlgeschlagen (Versuch %d/5): %s – warte %ds", d, attempt, exc, wait)
            time.sleep(wait)
    log.error("Abruf %s endgültig fehlgeschlagen", d)
    HARD_FAILS += 1
    return None


# ----------------------------------------------------------------------------
# 4. ANALYSE
# ----------------------------------------------------------------------------
def hourly(points: list[list[float]]) -> list[dict]:
    """Aggregiert (auch 15-Minuten-)Werte zu Stundenmittelwerten in ct/kWh (netto, Börsenpreis).
    Schlüssel ist die UTC-Stunde, dadurch stimmen auch Tage mit Zeitumstellung (23/25 Stunden)."""
    buckets: dict[int, list[float]] = {}
    for t, p in points:
        buckets.setdefault(int(t) // 3600, []).append(p)
    out = []
    for h in sorted(buckets):
        start = to_local(h * 3600)
        out.append({"t": h * 3600, "start": start, "hour": start.hour,
                    "ct": statistics.mean(buckets[h]) / 10})
    return out


def compute_thresholds(day_hours: dict[date, list[dict]], today: date) -> dict:
    """Ampel-Schwellen: untere Drittel = günstig, obere Drittel = teuer, bezogen auf die
    Stundenpreise der letzten STATS_DAYS Tage (Zukunft zählt nicht mit)."""
    vals = [h["ct"] for d, hs in day_hours.items()
            if today - timedelta(days=STATS_DAYS - 1) <= d <= today for h in hs]
    if len(vals) < 48:
        vals = [h["ct"] for hs in day_hours.values() for h in hs]
    vals.sort()
    n = len(vals)
    return {"low": vals[int(n * 0.33)], "high": vals[min(n - 1, int(n * 0.67))], "n": n}


def level_of(ct: float, thr: dict) -> str:
    if ct <= thr["low"]:
        return "g"
    if ct >= thr["high"]:
        return "r"
    return "y"


def hour_to(last: dict) -> int:
    """Endstunde (1-24) eines Blocks, der mit der Stunde `last` endet (23 Uhr -> 24 Uhr)."""
    return last["hour"] + 1


def make_blocks(hrs: list[dict]) -> list[dict]:
    groups: list[list[dict]] = []
    for h in hrs:
        if groups and groups[-1][0]["lvl"] == h["lvl"]:
            groups[-1].append(h)
        else:
            groups.append([h])
    out = []
    for k, g in enumerate(groups):
        # Ende = Beginn des nächsten Blocks (so stimmt es auch an Tagen mit Zeitumstellung)
        h_to = groups[k + 1][0]["hour"] if k + 1 < len(groups) else hour_to(g[-1])
        out.append({"lvl": g[0]["lvl"], "word": LEVEL_WORD[g[0]["lvl"]], "h_from": g[0]["hour"],
                    "h_to": h_to, "avg": statistics.mean(x["ct"] for x in g), "n": len(g)})
    return out


def best_window(hrs: list[dict], length: int, worst: bool = False) -> dict | None:
    if len(hrs) < length:
        return None
    pick = max if worst else min
    i = pick(range(len(hrs) - length + 1), key=lambda k: sum(x["ct"] for x in hrs[k:k + length]))
    win = hrs[i:i + length]
    h_to = hrs[i + length]["hour"] if i + length < len(hrs) else hour_to(win[-1])
    return {"h_from": win[0]["hour"], "h_to": h_to,
            "avg": statistics.mean(x["ct"] for x in win), "idx": (i, i + length)}


WARTEN_STARK = 10.0    # ct/kWh brutto Abstand Tagesschnitt -> günstigste 3 Stunden (E-Auto 20 kWh: >= 2 EUR)
WARTEN_ETWAS = 4.0     # kalibriert mit echten Preisen Sep/Okt 2026 (Median des Abstands: 8,7 ct)
WARTEN = {
    "stark": {"kurz": "Ja, Warten lohnt sich {wann} deutlich.", "lvl": "g",
              "text": "Die günstigen Stunden sind klar billiger als der Rest des Tages."},
    "etwas": {"kurz": "Warten lohnt sich {wann} etwas.", "lvl": "y",
              "text": "Bei großen Verbrauchern wie E-Auto oder Warmwasser macht es sich bemerkbar. Bei der Waschmaschine sind es nur Cent."},
    "kaum": {"kurz": "Warten lohnt sich {wann} kaum.", "lvl": "n",
             "text": "Die Preise liegen den ganzen Tag nah beieinander. Schalten Sie Ihre Geräte ein, wann es Ihnen passt."},
}


def warten_of(avg: float, best3: float) -> dict:
    """Bewertet, ob sich das Verschieben an einem Tag lohnt: Abstand zwischen Tagesschnitt
    und den günstigsten drei Stunden, in Cent pro kWh inklusive Mehrwertsteuer."""
    diff = max(0.0, (avg - best3) * MWST)
    key = "stark" if diff >= WARTEN_STARK else ("etwas" if diff >= WARTEN_ETWAS else "kaum")
    return {"key": key, "diff": diff, "eauto": 20.0 * diff / 100, **WARTEN[key]}


def day_levels(hrs: list[dict]) -> None:
    """Grün/Gelb/Rot innerhalb des Tages: günstigstes Drittel, mittleres Drittel, teuerstes Drittel.
    So zeigt die Uhr auch an insgesamt teuren Tagen, wann es heute am günstigsten ist.
    Liegen alle Stunden fast gleich, wird der ganze Tag gelb (keine künstlichen Unterschiede).
    Negative Preise sind immer grün."""
    cts = sorted(h["ct"] for h in hrs)
    n = len(cts)
    flat = (cts[-1] - cts[0]) * MWST < WARTEN_ETWAS
    low, high = cts[max(0, n // 3 - 1)], cts[n - n // 3]
    for h in hrs:
        if h["ct"] < 0:
            h["lvl"] = "g"
        elif flat:
            h["lvl"] = "y"
        else:
            h["lvl"] = "g" if h["ct"] <= low else ("r" if h["ct"] >= high else "y")


def niveau_of(avg: float, ref: float, n_days: int) -> dict:
    """Preisniveau eines Tages im Vergleich zum Schnitt der letzten Tage (Börsenpreis netto)."""
    if ref > 0.5 and avg > ref * 1.15:
        word = "teurer als"
    elif ref > 0.5 and avg < ref * 0.85:
        word = "günstiger als"
    else:
        word = "etwa so teuer wie"
    return {"word": word, "avg": avg, "ref": ref, "n": n_days}


def analyse_day(d: date, hrs: list[dict], thr: dict) -> dict:
    day_levels(hrs)
    avg = statistics.mean(h["ct"] for h in hrs)
    green = sum(1 for h in hrs if h["lvl"] == "g")
    devices = []
    for dev in DEVICES:
        best = best_window(hrs, dev["hours"])
        worst = best_window(hrs, dev["hours"], worst=True)
        if not best or not worst:
            continue
        save_avg = dev["kwh"] * (avg - best["avg"]) * MWST / 100          # EUR je Durchlauf, inkl. MwSt.
        save_worst = dev["kwh"] * (worst["avg"] - best["avg"]) * MWST / 100
        devices.append({**dev, "best": best, "worst": worst, "save_avg": save_avg,
                        "dial": dial_svg(hrs, f'{best["h_from"]} bis {best["h_to"]} Uhr', mark=best["idx"]),
                        "save_worst": save_worst, "save_year": save_avg * dev["per_week"] * 52})
    blocks = make_blocks(hrs)
    w3 = best_window(hrs, 3)
    return {
        "warten": warten_of(avg, w3["avg"]),
        "date": d, "iso": d.isoformat(), "pretty": de_date(d), "short": short_date(d),
        "hours": hrs, "avg": avg, "n": len(hrs),
        "cheap": min(hrs, key=lambda h: h["ct"]), "pricey": max(hrs, key=lambda h: h["ct"]),
        "negative_hours": sum(1 for h in hrs if h["ct"] < 0),
        "green_share": green / len(hrs),
        "windows": {k: best_window(hrs, k) for k in (1, 2, 3, 4)},
        "devices": devices, "blocks": blocks,
        "band": band_html(hrs), "band_small": band_html(hrs, small=True),
        "blocks_html": blocks_html(blocks), "chart": chart_html(hrs),
        "dial": dial_svg(hrs, f"Ø {de1(avg)} ct"),
        "dial_live": dial_svg(hrs, "", live=True),
        "table": hours_table_html(hrs),
    }


def compute_stats(analysed: list[dict], thr: dict, today: date) -> dict:
    window = [a for a in analysed if today - timedelta(days=STATS_DAYS - 1) <= a["date"] <= today] or analysed
    all_ct = [h["ct"] for a in window for h in a["hours"]]
    by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    by_wd: dict[int, dict[str, list[float]]] = {w: {"avg": [], "green": []} for w in range(7)}
    for a in window:
        for h in a["hours"]:
            by_hour[h["hour"]].append(h["ct"])
        by_wd[a["date"].weekday()]["avg"].append(a["avg"])
        by_wd[a["date"].weekday()]["green"].append(a["warten"]["diff"])
    profile = []
    for hr, vals in by_hour.items():
        if vals:
            m = statistics.mean(vals)
            profile.append({"hour": hr, "ct": m, "lvl": level_of(m, thr), "t": None})
    weekdays = []
    for w in range(7):
        v = by_wd[w]
        if v["avg"]:
            weekdays.append({"name": WEEKDAYS[w], "avg": statistics.mean(v["avg"]),
                             "green": statistics.mean(v["green"]), "n": len(v["avg"])})
    cheapest_day = min(window, key=lambda a: a["cheap"]["ct"])
    priciest_day = max(window, key=lambda a: a["pricey"]["ct"])
    best4 = [a["windows"][4]["avg"] for a in window if a["windows"].get(4)]
    return {
        "n_days": len(window), "avg": statistics.mean(all_ct),
        "best4": statistics.mean(best4) if best4 else statistics.mean(all_ct),
        "profile": profile, "profile_chart": chart_html(profile) if profile else Markup(""),
        "profile_best3": best_window(profile, 3) if len(profile) >= 3 else None,
        "weekdays": weekdays,
        "best_weekday": min(weekdays, key=lambda x: x["avg"]) if weekdays else None,
        "negative_hours": sum(a["negative_hours"] for a in window),
        "cheapest": {"ct": cheapest_day["cheap"]["ct"], "date": cheapest_day["pretty"],
                     "hour": cheapest_day["cheap"]["hour"]},
        "priciest": {"ct": priciest_day["pricey"]["ct"], "date": priciest_day["pretty"],
                     "hour": priciest_day["pricey"]["hour"]},
    }


def compute_months(analysed: list[dict], today: date) -> list[dict]:
    """Monatsrückblicke aus allen vergangenen Tagen (heute und morgen zählen noch nicht mit).
    Abgeschlossene Monate brauchen mindestens 20 Tage Daten, der laufende Monat mindestens 5."""
    by_month: dict[str, list[dict]] = {}
    for a in analysed:
        if a["date"] < today:
            by_month.setdefault(a["date"].strftime("%Y-%m"), []).append(a)
    current = today.strftime("%Y-%m")
    months = []
    for key in sorted(by_month):
        days = sorted(by_month[key], key=lambda a: a["date"])
        complete = key < current
        if len(days) < (20 if complete else 5):
            continue
        first = days[0]["date"]
        name = f"{MONTHS[first.month - 1]} {first.year}"
        devices = []
        for dev in DEVICES:
            per_run = [x["save_avg"] for a in days for x in a["devices"] if x["slug"] == dev["slug"]]
            if not per_run:
                continue
            runs = dev["per_week"] * len(days) / 7
            devices.append({**dev, "per_run": statistics.mean(per_run), "runs": runs,
                            "total": statistics.mean(per_run) * runs})
        wd: dict[int, list[float]] = {}
        for a in days:
            wd.setdefault(a["date"].weekday(), []).append(a["avg"])
        best_wd = min(wd, key=lambda w: statistics.mean(wd[w]))
        counts = {k: sum(1 for a in days if a["warten"]["key"] == k) for k in ("stark", "etwas", "kaum")}
        haushalt = sum(d["total"] for d in devices if d["slug"] in ("waschmaschine", "geschirrspueler", "e-auto-wallbox"))
        months.append({
            "key": key, "name": name, "complete": complete, "days": days, "n": len(days),
            "avg": statistics.mean(a["avg"] for a in days),
            "best3": statistics.mean(a["windows"][3]["avg"] for a in days),
            "spread": statistics.mean(a["warten"]["diff"] for a in days),
            "counts": counts, "devices": devices, "haushalt": haushalt,
            "negative_hours": sum(a["negative_hours"] for a in days),
            "cheapest_day": min(days, key=lambda a: a["avg"]),
            "priciest_day": max(days, key=lambda a: a["avg"]),
            "cheapest_hour": min(((a, a["cheap"]) for a in days), key=lambda x: x[1]["ct"]),
            "best_weekday": WEEKDAYS[best_wd],
            "calendar": calendar_html(days, root="../"),
        })
    for i, m in enumerate(months):
        m["prev"] = months[i - 1] if i > 0 else None
        m["next"] = months[i + 1] if i + 1 < len(months) else None
    return months


# ----------------------------------------------------------------------------
# 5. HTML-HELFER (Farbband, Säulen, Blockliste, Tabellen)
# ----------------------------------------------------------------------------
def band_html(hrs: list[dict], small: bool = False) -> Markup:
    cells = []
    for h in hrs:
        lab = str(h["hour"]) if (h["hour"] % 3 == 0 and not small) else ""
        tip = f'{h["hour"]:02d} Uhr: {LEVEL_WORD[h["lvl"]]}, {de1(h["ct"])} ct/kWh'
        cells.append(f'<div class="bz {h["lvl"]}" data-t="{h["t"]}" title="{tip}"><span>{lab}</span></div>')
    cls = "band klein" if small else "band"
    label = ("Farbband des Tages: grün günstig, gelb normal, rot teuer"
             if not small else "Farbband dieses Tages")
    return Markup(f'<div class="{cls}" role="img" aria-label="{label}">{"".join(cells)}</div>')


def chart_html(items: list[dict]) -> Markup:
    """Säulendiagramm in HTML/CSS (Schrift bleibt mit der Seite gleich groß, auch am Handy)."""
    vals = [x["ct"] for x in items]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    cols = []
    for x in items:
        height = 1.0 + 8.0 * (x["ct"] - lo) / span      # 1 bis 9 rem
        lab = str(x["hour"]) if x["hour"] % 3 == 0 else ""
        t = f' data-t="{x["t"]}"' if x.get("t") is not None else ""
        tip = f'{x["hour"]:02d} Uhr: {de1(x["ct"])} ct/kWh'
        cols.append(f'<div class="col"><div class="bar {x["lvl"]}"{t} style="height:{height:.2f}rem" '
                    f'title="{tip}"></div><span class="lab">{lab}</span></div>')
    return Markup('<div class="bars" role="img" aria-label="Säulendiagramm je Stunde: '
                  f'höhere Säule bedeutet höherer Preis">{"".join(cols)}</div>')


def blocks_html(blocks: list[dict]) -> Markup:
    rows = []
    for b in blocks:
        rows.append(f'<li class="{b["lvl"]}"><span><span class="zl-zeit">{b["h_from"]} bis {b["h_to"]} Uhr</span> '
                    f'<span class="zl-wort">{b["word"]}</span></span>'
                    f'<span class="zl-preis">Ø {de1(b["avg"])} ct</span></li>')
    return Markup("".join(rows))


def hours_table_html(hrs: list[dict]) -> Markup:
    rows = []
    prev = None
    for h in hrs:
        extra = " (Zeitumstellung: diese Stunde gibt es heute zweimal)" if h["hour"] == prev else ""
        prev = h["hour"]
        rows.append(f'<tr><td>{h["hour"]:02d}:00 Uhr{extra}</td><td>{de1(h["ct"])} ct/kWh</td>'
                    f'<td><span class="marker {h["lvl"]}" aria-hidden="true"></span>{LEVEL_WORD[h["lvl"]]}</td></tr>')
    return Markup("".join(rows))


def dial_svg(hrs: list[dict], center: str, live: bool = False, mark: tuple[int, int] | None = None) -> Markup:
    """24-Stunden-Zifferblatt wie bei einer Zeitschaltuhr: 0 Uhr oben, im Uhrzeigersinn.
    Jedes Segment ist eine Stunde in Ampelfarbe. Der Zeiger wird im Browser auf 'jetzt' gedreht."""
    n = len(hrs)
    cx = cy = 130.0
    r_out, r_in, r_lab = 104.0, 70.0, 125.0
    parts = []
    for i, h in enumerate(hrs):
        a0 = math.radians(i * 360 / n - 90 + 0.7)
        a1 = math.radians((i + 1) * 360 / n - 90 - 0.7)
        p = (f"M{cx + r_out * math.cos(a0):.2f} {cy + r_out * math.sin(a0):.2f}"
             f"A{r_out:.0f} {r_out:.0f} 0 0 1 {cx + r_out * math.cos(a1):.2f} {cy + r_out * math.sin(a1):.2f}"
             f"L{cx + r_in * math.cos(a1):.2f} {cy + r_in * math.sin(a1):.2f}"
             f"A{r_in:.0f} {r_in:.0f} 0 0 0 {cx + r_in * math.cos(a0):.2f} {cy + r_in * math.sin(a0):.2f}Z")
        tip = f'{h["hour"]} Uhr: {LEVEL_WORD[h["lvl"]]}, {de1(h["ct"])} ct/kWh'
        parts.append(f'<path class="seg {h["lvl"]}" data-t="{h["t"]}" d="{p}"><title>{tip}</title></path>')
    if mark:
        # wie die heruntergedrückten Stifte einer Zeitschaltuhr: Bogen außen um das beste Zeitfenster
        r_m = 111.0
        a0 = math.radians(mark[0] * 360 / n - 90 + 0.7)
        a1 = math.radians(mark[1] * 360 / n - 90 - 0.7)
        large = 1 if (mark[1] - mark[0]) * 360 / n > 180 else 0
        parts.append(f'<path class="stifte" d="M{cx + r_m * math.cos(a0):.2f} {cy + r_m * math.sin(a0):.2f}'
                     f'A{r_m:.0f} {r_m:.0f} 0 {large} 1 {cx + r_m * math.cos(a1):.2f} {cy + r_m * math.sin(a1):.2f}"/>')
    seen = set()
    for i, h in enumerate(hrs):
        if h["hour"] % 3 or h["hour"] in seen:
            continue
        seen.add(h["hour"])
        a = math.radians(i * 360 / n - 90)
        parts.append(f'<text class="ziffer" x="{cx + r_lab * math.cos(a):.1f}" y="{cy + r_lab * math.sin(a):.1f}">'
                     f'{h["hour"]}</text>')
    if live:
        parts.append(f'<g class="zeiger"><line x1="{cx}" y1="{cy - 50}" x2="{cx}" y2="{cy - r_out - 7}"/>'
                     f'<circle cx="{cx}" cy="{cy - r_out - 7}" r="3.5"/></g>')
    parts.append(f'<text class="mitte" x="{cx}" y="{cy}">{center}</text>')
    attr = ' data-live="1"' if live else ""
    return Markup(f'<svg class="uhr" viewBox="-8 -8 276 276"{attr} data-n="{n}" role="img" '
                  f'aria-label="24-Stunden-Uhr: Jede Stunde ist grün (günstig), gelb (normal) oder rot (teuer).">'
                  f'{"".join(parts)}</svg>')


def calendar_html(days_desc: list[dict], root: str = "") -> Markup:
    rows = []
    for a in days_desc:
        rows.append(f'<a class="kal-zeile" href="{root}tag/{a["iso"]}.html">'
                    f'<span class="kal-tag">{a["short"]}</span>{a["band_small"]}'
                    f'<span class="kal-ct">Ø {de1(a["avg"])}</span></a>')
    return Markup("".join(rows))


# ----------------------------------------------------------------------------
# 6. CSS und JAVASCRIPT (werden nach docs/assets/ geschrieben)
# ----------------------------------------------------------------------------
CSS = r'''
@font-face{font-family:"Atkinson Hyperlegible";font-style:normal;font-weight:400;font-display:swap;src:url("fonts/atkinson-400.woff2") format("woff2")}
@font-face{font-family:"Atkinson Hyperlegible";font-style:normal;font-weight:700;font-display:swap;src:url("fonts/atkinson-700.woff2") format("woff2")}
:root{
 --papier:#ffffff;--tinte:#1c2b39;--grau:#55636f;--linie:#d3dae0;--flaeche:#eef2f4;--blau:#1f5a8a;
 --gruen:#2e8540;--gruen-hell:#e4f2e7;--gruen-text:#1d5c2b;
 --gelb:#e3a615;--gelb-hell:#fbf1d4;--gelb-text:#6b4a00;
 --rot:#c73e32;--rot-hell:#f9e3e0;--rot-text:#8f2219;
 color-scheme:light}
*{box-sizing:border-box}
html{font-size:125%;-webkit-text-size-adjust:100%;background:var(--papier)}
body{margin:0;background:var(--papier);color:var(--tinte);line-height:1.55;
font-family:"Atkinson Hyperlegible",Verdana,"Segoe UI",system-ui,sans-serif;
overflow-wrap:break-word;-webkit-hyphens:auto;hyphens:auto}
.wrap{max-width:52rem;margin:0 auto;padding:0 clamp(.75rem,4vw,1.25rem)}
h1{font-size:1.75rem;line-height:1.2;margin:0 0 .5rem}
h2{font-size:1.35rem;line-height:1.25;margin:2.4rem 0 .6rem}
h3{font-size:1.1rem;margin:1.5rem 0 .4rem}
p,li{max-width:40rem}
a{color:var(--blau);text-decoration-thickness:.08em;text-underline-offset:.18em}
a:hover{text-decoration-thickness:.15em}
:focus-visible{outline:3px solid var(--blau);outline-offset:3px}
.skip{position:absolute;left:-999px;top:0}
.skip:focus{left:1rem;top:1rem;background:var(--papier);padding:.5rem 1rem;z-index:20;border:2px solid var(--tinte)}
.leise{color:var(--grau);font-size:.85rem}

/* Kopf: Wortmarke und Textnavigation */
.kopf{border-bottom:1px solid var(--linie)}
.kopf .wrap{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:.3rem 1.5rem;padding-top:.9rem;padding-bottom:0}
.marke{font-weight:700;font-size:1.2rem;color:var(--tinte);text-decoration:none;display:inline-flex;align-items:center;gap:.5rem;padding-bottom:.7rem}
.marke svg{width:1.6rem;height:1.6rem;flex:none}
.menue{display:flex;flex-wrap:wrap;gap:0 1.3rem}
.menue a{display:inline-block;padding:.35rem 0 .6rem;color:var(--tinte);text-decoration:none;border-bottom:4px solid transparent;font-size:.95rem}
.menue a:hover{border-bottom-color:var(--linie)}
.menue a[aria-current=page]{border-bottom-color:var(--blau);font-weight:700}

/* Die Lage jetzt: Zifferblatt und Satz */
.frage{padding-top:1.6rem}
.lage{background:var(--flaeche);border-top:1px solid var(--linie);border-bottom:1px solid var(--linie);margin:1rem 0 0}
.lage[data-lvl=g]{background:var(--gruen-hell)}
.lage[data-lvl=y]{background:var(--gelb-hell)}
.lage[data-lvl=r]{background:var(--rot-hell)}
.lage-innen{display:grid;grid-template-columns:minmax(0,17rem) minmax(0,1fr);gap:2rem;align-items:center;padding-top:1.6rem;padding-bottom:1.6rem}
.satz{font-size:2.1rem;font-weight:700;line-height:1.15;margin:0 0 .7rem}
.lage[data-lvl=g] .satz{color:var(--gruen-text)}
.lage[data-lvl=y] .satz{color:var(--gelb-text)}
.lage[data-lvl=r] .satz{color:var(--rot-text)}
.lage-text p{margin:.35rem 0}
.lage-text .bis{font-size:1.15rem;font-weight:700}

/* Zifferblatt */
.uhr{display:block;width:100%;height:auto;max-width:17rem}
.uhr .seg{stroke:var(--papier);stroke-width:1}
.uhr .seg.g{fill:var(--gruen)}.uhr .seg.y{fill:var(--gelb)}.uhr .seg.r{fill:var(--rot)}
.uhr .seg.jetzt{stroke:var(--tinte);stroke-width:3}
.uhr .ziffer{font-size:13px;font-weight:700;fill:var(--tinte);text-anchor:middle;dominant-baseline:central}
.uhr .mitte{font-size:17px;font-weight:700;fill:var(--tinte);text-anchor:middle;dominant-baseline:central}
.uhr .mitte.g{fill:var(--gruen-text)}.uhr .mitte.y{fill:var(--gelb-text)}.uhr .mitte.r{fill:var(--rot-text)}
.uhr .stifte{fill:none;stroke:var(--tinte);stroke-width:6;stroke-linecap:butt}
.uhr .zeiger{display:none}
.uhr .zeiger.an{display:inline}
.uhr .zeiger line{stroke:var(--tinte);stroke-width:5;stroke-linecap:round}
.uhr .zeiger circle{fill:var(--tinte)}
.uhr-klein{max-width:13rem}

/* Tagesplan als Zeitleiste */
.tage{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(19rem,100%),1fr));gap:0 2.5rem}
.tag h2{margin-top:2rem}
ol.zeitleiste{list-style:none;padding:0;margin:.4rem 0 .8rem;border-top:1px solid var(--linie)}
ol.zeitleiste li{display:grid;grid-template-columns:.9rem minmax(0,1fr) auto;gap:.2rem .7rem;align-items:baseline;padding:.5rem 0;border-bottom:1px solid var(--linie);max-width:none}
ol.zeitleiste li::before{content:"";width:.9rem;height:.9rem;border-radius:2px;align-self:center}
ol.zeitleiste li.g::before{background:var(--gruen)}
ol.zeitleiste li.y::before{background:var(--gelb)}
ol.zeitleiste li.r::before{background:var(--rot)}
.zl-zeit{font-weight:700}
.zl-wort{font-weight:400}
.zl-preis{color:var(--grau);font-size:.85rem;text-align:right;white-space:nowrap}
.beste{margin:.6rem 0}
.hinweis{border-left:5px solid var(--gelb);padding:.3rem 0 .3rem 1rem;margin:1.2rem 0}

/* Fahrplan der Geräte */
table.fahrplan{width:100%;border-collapse:collapse;margin:.6rem 0}
.fahrplan th{font-size:.85rem;font-weight:400;color:var(--grau);text-align:left;padding:.4rem .5rem .4rem 0;border-bottom:2px solid var(--tinte)}
.fahrplan td{padding:.65rem .5rem .65rem 0;border-bottom:1px solid var(--linie);vertical-align:baseline}
.fahrplan td:first-child a{font-weight:700}
.fahrplan .zahl{text-align:right;white-space:nowrap}
.fahrplan th.zahl{text-align:right}

/* Allgemeine Tabellen */
.scroll{overflow-x:auto}
table.tabelle{width:100%;border-collapse:collapse;margin:.6rem 0}
.tabelle th,.tabelle td{padding:.5rem .6rem .5rem 0;border-bottom:1px solid var(--linie);text-align:left;vertical-align:top}
.tabelle th{font-size:.85rem;font-weight:400;color:var(--grau);border-bottom:2px solid var(--tinte)}
.marker{display:inline-block;width:.8rem;height:.8rem;border-radius:2px;margin-right:.4rem;vertical-align:-.05rem}
.marker.g{background:var(--gruen)}.marker.y{background:var(--gelb)}.marker.r{background:var(--rot)}

/* Farbband und Säulen */
.band{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);gap:1px}
.bz{height:1.3rem;min-width:0}
.bz.g{background:var(--gruen)}.bz.y{background:var(--gelb)}.bz.r{background:var(--rot)}
.bz span{display:none}
.bz.jetzt{outline:2px solid var(--tinte);outline-offset:0;position:relative;z-index:1}
.bars{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);gap:2px;align-items:end;margin:1rem 0 .2rem;border-bottom:2px solid var(--tinte)}
.col{display:flex;flex-direction:column;justify-content:flex-end;min-width:0}
.bar{min-height:2px}
.bar.g{background:var(--gruen)}.bar.y{background:var(--gelb)}.bar.r{background:var(--rot)}
.bar.jetzt{outline:2px solid var(--tinte)}
.lab{display:block;height:1.2rem;overflow:hidden;font-size:clamp(9px,.7rem,13px);text-align:center;color:var(--grau);order:2}

/* Kalender */
.kalender{border-top:1px solid var(--linie)}
.kal-zeile{display:grid;grid-template-columns:5.6em minmax(2rem,1fr) 3.9em;gap:.6em;align-items:center;padding:.22rem 0;text-decoration:none;color:var(--tinte);border-bottom:1px solid var(--flaeche);font-size:clamp(11px,.85rem,16px)}
.kal-zeile:hover .kal-tag{text-decoration:underline}
.kal-tag{font-weight:700;white-space:nowrap}
.kal-ct{color:var(--grau);white-space:nowrap;text-align:right}

/* Kennzahlen und Rechner-Ergebnisse */
.werte{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(12rem,100%),1fr));gap:1rem 1.5rem;margin:1rem 0}
.wert{border-left:5px solid var(--linie);padding:.1rem 0 .1rem .8rem;display:flex;flex-direction:column}
.wert.gut{border-left-color:var(--gruen)}
.wert.schlecht{border-left-color:var(--rot)}
.wert .titel{color:var(--grau);font-size:.85rem}
.wert .zahl{font-size:1.45rem;font-weight:700;line-height:1.25}

/* Formulare */
form.rechner{background:var(--flaeche);padding:1.2rem clamp(.9rem,3vw,1.4rem);margin:1.2rem 0;border-radius:6px}
.feld{margin:0 0 1.1rem}
.feld:last-child{margin-bottom:0}
.feld label{display:block;font-weight:700;margin-bottom:.3rem}
.feld input,.feld select{width:100%;max-width:26rem;font:inherit;padding:.6rem .7rem;border:2px solid #9aa7b1;border-radius:6px;background:var(--papier);color:var(--tinte)}
.feld input[type=range]{padding:0;height:2.4rem;border:0;background:transparent;accent-color:var(--blau)}
.hilfe{display:block;color:var(--grau);font-size:.85rem;margin-top:.25rem;max-width:36rem}
.ergebnis{margin:1.4rem 0}
.knopf{display:inline-block;font:inherit;font-weight:700;padding:.65rem 1.2rem;border:2px solid var(--blau);border-radius:6px;background:var(--blau);color:#fff;text-decoration:none;cursor:pointer}
.knopf:hover{background:#174a73}
.textknopf{font:inherit;color:var(--blau);background:none;border:0;padding:0;text-decoration:underline;text-underline-offset:.18em;cursor:pointer}

/* Lohnt sich Warten? */
.warten{border-left:6px solid #9aa7b1;padding:.1rem 0 .1rem 1rem;margin:2rem 0 .8rem}
.warten.g{border-left-color:var(--gruen)}
.warten.y{border-left-color:var(--gelb)}
.warten h2{margin:0 0 .2rem}
.warten-satz{font-size:1.45rem;font-weight:700;line-height:1.2;margin:.1rem 0 .5rem}
.warten-morgen{margin:.3rem 0 1rem}

/* Wechsel-Check */
fieldset.frage-block{border:0;margin:0 0 1.4rem;padding:0}
fieldset.frage-block legend{font-weight:700;font-size:1.05rem;padding:0;margin-bottom:.5rem}
.auswahl{display:flex;flex-wrap:wrap;gap:.5rem}
.auswahl label{display:flex;align-items:center;gap:.5rem;min-height:2.8rem;padding:.4rem .9rem;border:2px solid #9aa7b1;border-radius:6px;background:var(--papier);cursor:pointer;font-weight:400}
.auswahl input{width:1.15rem;height:1.15rem;margin:0;accent-color:var(--blau)}
.auswahl label:has(input:checked){border-color:var(--blau);box-shadow:inset 0 0 0 1px var(--blau);font-weight:700}
.urteil{font-size:1.6rem;font-weight:700;line-height:1.2;margin:.3rem 0 .8rem}
.urteil.ja{color:var(--gruen-text)}
.urteil.vielleicht{color:var(--gelb-text)}
.urteil.nein{color:var(--tinte)}
ul.gruende li{margin:.3rem 0}

/* Listen von Erklärseiten */
ul.verweise{list-style:none;padding:0;margin:1rem 0;border-top:1px solid var(--linie)}
ul.verweise li{padding:.8rem 0;border-bottom:1px solid var(--linie);max-width:none}
ul.verweise a{font-weight:700;font-size:1.1rem}
ul.verweise span{display:block;color:var(--grau);font-size:.9rem}

.weiter{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin:1.6rem 0}
.werbung{border-top:1px solid var(--linie);margin-top:2.5rem;padding-top:.8rem}
main{padding-bottom:2.5rem}
footer{background:var(--flaeche);border-top:1px solid var(--linie);padding:1.2rem 0 2rem;font-size:.85rem;color:var(--grau)}
footer p{max-width:46rem}
.fuss-nav{display:flex;flex-wrap:wrap;gap:.4rem 1.3rem;margin-top:.7rem}
.quellen{font-size:.9rem;color:var(--grau)}

@media (max-width:40rem){
 html{font-size:112.5%}
 .lage-innen{grid-template-columns:1fr;gap:1rem;justify-items:center}
 .lage-text{justify-self:stretch}
 .uhr{max-width:15rem}
 .satz{font-size:1.75rem}
 h1{font-size:1.5rem}
 .menue{gap:0 1rem}
}
@media print{
 .kopf .menue,.fuss-nav,.noprint,form,.skip,.werbung{display:none!important}
 html{font-size:100%}
 body{font-size:12pt}
 .lage{background:#fff!important;border:1px solid #999}
 a{color:#000}
}
'''

JS = r'''
(function () {
  'use strict';

  var DATA = null;
  try {
    var el = document.getElementById('preisdaten');
    if (el) { DATA = JSON.parse(el.textContent); }
  } catch (e) { DATA = null; }

  var WORD = { g: 'günstig', y: 'normal', r: 'teuer' };
  var SATZ = { g: 'Jetzt ist Strom günstig.', y: 'Jetzt ist Strom normal teuer.', r: 'Jetzt ist Strom teuer.' };
  var HINT = {
    g: 'Ein guter Zeitpunkt für Waschmaschine, Geschirrspüler und andere große Geräte.',
    y: 'Geräte dürfen laufen. Wer warten kann, findet aber günstigere Stunden.',
    r: 'Große Geräte wenn möglich später einschalten.'
  };

  function $(id) { return document.getElementById(id); }
  function fmt(n, d) { return n.toFixed(d).replace('.', ','); }
  function num(el, def) {
    var v = parseFloat(String(el.value).replace(',', '.'));
    return isFinite(v) ? v : def;
  }

  var tf = new Intl.DateTimeFormat('de-DE', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'Europe/Berlin' });
  var ymd = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: 'Europe/Berlin' });
  var wd = new Intl.DateTimeFormat('de-DE', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'Europe/Berlin' });

  function uhr(t) {
    var s = tf.format(new Date(t * 1000)).replace(/:00$/, '').replace(/^0(\d)/, '$1');
    if (s === '0') { s = '24'; }
    return s + ' Uhr';
  }
  function dayLabel(t) {
    var d = ymd.format(new Date(t * 1000));
    var now = Date.now();
    if (d === ymd.format(new Date(now))) { return 'heute'; }
    if (d === ymd.format(new Date(now + 86400000))) { return 'morgen'; }
    return 'am ' + wd.format(new Date(t * 1000));
  }
  /* Endzeit: Mitternacht gehört zum Vortag ("bis 24 Uhr") */
  function endLabel(t) { return uhr(t); }
  function endDay(t) { return dayLabel(t - 1); }

  /* ---- Preisdaten sortiert und ohne Dopplungen ---- */
  var all = [];
  if (DATA && DATA.days) {
    var seen = {};
    Object.keys(DATA.days).forEach(function (k) {
      DATA.days[k].forEach(function (h) {
        if (!seen[h.t]) { seen[h.t] = 1; all.push(h); }
      });
    });
    all.sort(function (a, b) { return a.t - b.t; });
  }
  var nowT = Math.floor(Date.now() / 1000);
  var idx = -1;
  for (var i = 0; i < all.length; i++) {
    if (all[i].t <= nowT && nowT < all[i].t + 3600) { idx = i; break; }
  }

  function runEnd(i) {
    var j = i;
    while (j + 1 < all.length && all[j + 1].t === all[j].t + 3600 && all[j + 1].l === all[i].l) { j++; }
    return j;
  }
  function allWindows(start, len) {
    var res = [];
    for (var i = start; i + len <= all.length; i++) {
      if (all[i + len - 1].t - all[i].t !== (len - 1) * 3600) { continue; }
      var s = 0;
      for (var k = 0; k < len; k++) { s += all[i + k].c; }
      res.push({ i: i, avg: s / len });
    }
    return res;
  }
  function pickBest(wins, worst) {
    var best = null;
    wins.forEach(function (w) {
      if (!best || (worst ? w.avg > best.avg : w.avg < best.avg)) { best = w; }
    });
    return best;
  }
  function winLabel(w, len) {
    var t0 = all[w.i].t;
    return dayLabel(t0) + ' von ' + uhr(t0) + ' bis ' + endLabel(t0 + len * 3600);
  }

  /* ---- aktuelle Stunde markieren (Zifferblatt, Band, Säulen) ---- */
  function markNow() {
    var els = document.querySelectorAll('[data-t]');
    for (var i = 0; i < els.length; i++) {
      var t = parseInt(els[i].getAttribute('data-t'), 10);
      if (t <= nowT && nowT < t + 3600) { els[i].classList.add('jetzt'); }
    }
  }

  /* ---- Zeiger der Zeitschaltuhr auf "jetzt" drehen ---- */
  function setHand(svg, lvl) {
    var segs = svg.querySelectorAll('.seg');
    var n = segs.length;
    for (var i = 0; i < n; i++) {
      var t = parseInt(segs[i].getAttribute('data-t'), 10);
      if (t <= nowT && nowT < t + 3600) {
        var angle = (i + (nowT - t) / 3600) / n * 360;
        var hand = svg.querySelector('.zeiger');
        if (hand) {
          hand.setAttribute('transform', 'rotate(' + angle.toFixed(2) + ' 130 130)');
          hand.classList.add('an');
        }
        var mid = svg.querySelector('.mitte');
        if (mid && lvl) {
          mid.textContent = WORD[lvl];
          mid.setAttribute('class', 'mitte ' + lvl);
        }
        return true;
      }
    }
    return false;
  }

  /* ---- Die Lage jetzt (Startseite) ---- */
  function initHero() {
    var box = $('jetzt');
    if (!box) { return; }
    var satz = $('jetzt-satz'), bis = $('jetzt-bis'), preis = $('jetzt-preis'), hint = $('jetzt-hint'), next = $('jetzt-next'), beste = $('jetzt-beste');
    if (!DATA || idx < 0) {
      satz.textContent = 'Die aktuellen Preise fehlen gerade.';
      hint.textContent = 'Unten sehen Sie den zuletzt bekannten Tagesplan. Neue Preise kommen mit der nächsten Aktualisierung.';
      return;
    }
    var cur = all[idx];
    box.setAttribute('data-lvl', cur.l);
    satz.textContent = SATZ[cur.l];
    var j = runEnd(idx);
    var endT = all[j].t + 3600;
    if (j === all.length - 1) {
      bis.textContent = 'Das bleibt mindestens so bis ' + endLabel(endT) + '.';
    } else {
      bis.textContent = 'Das bleibt so bis ' + endLabel(endT) + (endDay(endT) === 'heute' ? '' : ' (' + endDay(endT) + ')') + '.';
    }
    preis.textContent = 'Börsenpreis in dieser Stunde: ' + fmt(cur.c, 1) + ' Cent pro kWh.';
    hint.textContent = HINT[cur.l];
    if (cur.l !== 'g') {
      var found = -1;
      for (var i = idx + 1; i < all.length; i++) { if (all[i].l === 'g') { found = i; break; } }
      if (found >= 0) {
        var e = runEnd(found);
        next.textContent = 'Die nächste günstige Zeit: ' + dayLabel(all[found].t) + ' von ' + uhr(all[found].t) + ' bis ' + endLabel(all[e].t + 3600) + '.';
      } else {
        next.textContent = 'Für die nächsten Stunden ist noch keine günstige Zeit bekannt.';
      }
    }
    var b = pickBest(allWindows(idx, 3), false);
    if (b && beste) {
      beste.textContent = 'Die günstigsten drei Stunden am Stück: ' + winLabel(b, 3) + '.';
    }
    var live = document.querySelector('svg.uhr[data-live]');
    if (live) { setHand(live, cur.l); }
  }

  /* ---- Kostenrechner ---- */
  function initRechner() {
    var f = $('rechner');
    if (!f) { return; }
    var out = $('ergebnis');
    f.addEventListener('submit', function (e) { e.preventDefault(); });
    if (!DATA) { out.textContent = 'Der Rechner braucht die Preisdaten. Bitte laden Sie die Seite neu.'; return; }

    var sel = f.elements.geraet;
    DATA.devices.forEach(function (d) {
      var o = document.createElement('option');
      o.value = d.slug; o.textContent = d.name;
      sel.appendChild(o);
    });
    var own = document.createElement('option');
    own.value = 'eigen'; own.textContent = 'Anderes Gerät (Werte selbst eintragen)';
    sel.appendChild(own);
    f.elements.aufschlag.value = String(DATA.aufschlag).replace('.', ',');

    function apply() {
      var d = null;
      DATA.devices.forEach(function (x) { if (x.slug === sel.value) { d = x; } });
      if (d) {
        f.elements.kwh.value = String(d.kwh).replace('.', ',');
        f.elements.dauer.value = String(d.hours);
        f.elements.woche.value = String(d.perWeek);
      }
    }
    function calc() {
      var k = num(f.elements.kwh, 0);
      var h = Math.max(1, Math.round(num(f.elements.dauer, 1)));
      var a = num(f.elements.aufschlag, 0);
      var w = Math.max(0, num(f.elements.woche, 0));
      var M = DATA.mwst;
      if (k <= 0) { out.innerHTML = '<p>Tragen Sie ein, wie viele Kilowattstunden das Gerät pro Durchlauf braucht.</p>'; return; }
      if (idx < 0) { out.innerHTML = '<p>Die aktuellen Preise fehlen gerade. Der Rechner funktioniert wieder, sobald neue Preise da sind.</p>'; return; }
      var wins = allWindows(idx, h);
      if (!wins.length) { out.innerHTML = '<p>Für ' + h + ' Stunden Laufzeit sind noch nicht genug Preise bekannt. Wählen Sie eine kürzere Laufzeit oder schauen Sie nach 14 Uhr wieder vorbei.</p>'; return; }
      function cost(avg) { return k * (avg + a) * M / 100; }
      var best = pickBest(wins, false), worst = pickBest(wins, true), now = null, sum = 0;
      wins.forEach(function (x) { sum += x.avg; if (x.i === idx) { now = x; } });
      var typ = sum / wins.length;
      var saveNow = now ? cost(now.avg) - cost(best.avg) : 0;
      var year = (cost(typ) - cost(best.avg)) * w * 52;
      var priceNow = (all[idx].c + a) * M;
      var html = '<h2>Ergebnis</h2><div class="werte">';
      html += '<div class="wert gut"><span class="titel">Am günstigsten</span><span class="zahl">' + fmt(cost(best.avg), 2) + ' €</span><span>' + winLabel(best, h) + '</span></div>';
      if (now) {
        html += '<div class="wert"><span class="titel">Wenn Sie jetzt starten</span><span class="zahl">' + fmt(cost(now.avg), 2) + ' €</span><span>' + (saveNow > 0.004 ? 'Warten spart ca. ' + fmt(saveNow, 2) + ' €.' : 'Jetzt ist bereits die beste Zeit.') + '</span></div>';
      }
      html += '<div class="wert schlecht"><span class="titel">Am teuersten</span><span class="zahl">' + fmt(cost(worst.avg), 2) + ' €</span><span>' + winLabel(worst, h) + '</span></div>';
      html += '</div>';
      html += '<p>Strom kostet Sie in dieser Stunde ca. <strong>' + fmt(priceNow, 1) + ' Cent pro kWh</strong> (Börsenpreis plus Aufschlag plus Mehrwertsteuer).</p>';
      if (w > 0) {
        html += '<p>Wer das Gerät ' + String(w).replace('.', ',') + '-mal pro Woche zur günstigsten statt zu einer beliebigen Zeit laufen lässt, spart grob geschätzt <strong>' + fmt(Math.max(year, 0), 2) + ' € im Jahr</strong>. Die Preise sind jeden Tag anders.</p>';
      }
      html += '<p class="leise">Diese Ersparnis gibt es nur mit einem dynamischen Stromtarif. Mit einem Festpreis kostet die Kilowattstunde zu jeder Uhrzeit gleich viel.</p>';
      out.innerHTML = html;
    }
    sel.addEventListener('change', function () { apply(); calc(); });
    f.addEventListener('input', calc);

    var q = null;
    try { q = new URLSearchParams(window.location.search).get('geraet'); } catch (e) { q = null; }
    if (q) {
      for (var i = 0; i < sel.options.length; i++) { if (sel.options[i].value === q) { sel.selectedIndex = i; } }
    }
    apply();
    calc();
  }

  /* ---- Tarifrechner: fest gegen dynamisch ---- */
  function initTarif() {
    var f = $('tarifrechner');
    if (!f) { return; }
    var out = $('tarif-ergebnis');
    f.addEventListener('submit', function (e) { e.preventDefault(); });
    if (!DATA) { out.textContent = 'Der Rechner braucht die Preisdaten. Bitte laden Sie die Seite neu.'; return; }
    f.elements.dyn_aufschlag.value = String(DATA.aufschlag).replace('.', ',');

    function calc() {
      var V = num(f.elements.verbrauch, 0);
      var fest = num(f.elements.fest_preis, 0), festG = num(f.elements.fest_grund, 0);
      var a = num(f.elements.dyn_aufschlag, 0), dynG = num(f.elements.dyn_grund, 0);
      var s = Math.min(100, Math.max(0, num(f.elements.anteil, 0))) / 100;
      var M = DATA.mwst, avg = DATA.stats.avg, best = DATA.stats.best4;
      $('anteil-wert').textContent = fmt(s * 100, 0) + ' %';
      if (V <= 0) { out.innerHTML = '<p>Tragen Sie Ihren Jahresverbrauch ein. Er steht auf Ihrer letzten Stromrechnung.</p>'; return; }
      var dynNetto = (1 - s) * avg + s * best;
      var dynBrutto = (dynNetto + a) * M;
      var cFest = V * fest / 100 + 12 * festG;
      var cDyn = V * dynBrutto / 100 + 12 * dynG;
      var diff = cFest - cDyn;
      var html = '<h2>Ergebnis</h2><div class="werte">';
      html += '<div class="wert"><span class="titel">Fester Tarif</span><span class="zahl">' + fmt(cFest, 0) + ' € im Jahr</span></div>';
      html += '<div class="wert"><span class="titel">Dynamischer Tarif</span><span class="zahl">' + fmt(cDyn, 0) + ' € im Jahr</span><span>im Mittel ca. ' + fmt(dynBrutto, 1) + ' Cent pro kWh</span></div>';
      html += '<div class="wert ' + (diff > 0 ? 'gut' : 'schlecht') + '"><span class="titel">Unterschied</span><span class="zahl">' + fmt(Math.abs(diff), 0) + ' €</span><span>' + (diff > 0 ? 'zugunsten des dynamischen Tarifs' : 'zugunsten des festen Tarifs') + '</span></div>';
      html += '</div>';
      var need = (cFest - 12 * dynG) * 100 / V;
      var sNeed = (avg - (need / M - a)) / (avg - best);
      if (avg - best > 0.01) {
        if (sNeed <= 0) {
          html += '<p>Mit diesen Zahlen wäre der dynamische Tarif <strong>auch ohne Verschieben</strong> günstiger.</p>';
        } else if (sNeed > 1) {
          html += '<p>Mit diesen Zahlen wäre der dynamische Tarif <strong>selbst dann nicht günstiger</strong>, wenn Sie Ihren ganzen Verbrauch in die günstigsten Stunden legen könnten.</p>';
        } else {
          html += '<p>Mit diesen Zahlen lohnt sich der dynamische Tarif, wenn Sie mindestens <strong>' + fmt(sNeed * 100, 0) + ' % Ihres Verbrauchs</strong> in günstige Stunden legen.</p>';
        }
      }
      html += '<p class="leise">Gerechnet mit dem durchschnittlichen Börsenpreis der letzten ' + DATA.stats.n + ' Tage (' + fmt(avg, 1) + ' Cent pro kWh netto) und dem Durchschnitt der günstigsten 4 Stunden je Tag (' + fmt(best, 1) + ' Cent). Die Preise ändern sich über das Jahr. Das Ergebnis ist eine Beispielrechnung, keine Vorhersage.</p>';
      out.innerHTML = html;
    }
    f.addEventListener('input', calc);
    calc();
  }

  /* ---- Wechsel-Check ---- */
  function initCheck() {
    var f = $('check');
    if (!f) { return; }
    var out = $('check-ergebnis');
    var NAMES = ['auto', 'heizung', 'speicher', 'flexibel', 'zaehler', 'planbar'];
    function val(n) {
      var r = f.querySelector('input[name="' + n + '"]:checked');
      return r ? r.value : null;
    }
    function calc() {
      var a = {}, offen = 0;
      NAMES.forEach(function (n) { a[n] = val(n); if (!a[n]) { offen++; } });
      if (offen) {
        out.innerHTML = '<p class="leise">' + (offen === 6 ? 'Beantworten Sie die Fragen, dann erscheint hier Ihr Ergebnis.' : 'Noch ' + offen + (offen === 1 ? ' Frage' : ' Fragen') + ' offen.') + '</p>';
        return;
      }
      if (!DATA) { out.textContent = 'Für die Schätzung fehlen die Preisdaten. Bitte laden Sie die Seite neu.'; return; }
      var gross = a.auto === 'ja' || a.heizung === 'ja' || a.speicher === 'ja';
      /* Verschiebbare Strommenge pro Jahr (grobe Durchschnittswerte) */
      var kwh = 2500 * ({ oft: 0.2, manchmal: 0.1, kaum: 0.03 })[a.flexibel];
      if (a.auto === 'ja') { kwh += 2500 * 0.8; }
      if (a.heizung === 'ja') { kwh += 4000 * 0.2; }
      if (a.speicher === 'ja') { kwh += 1000 * 0.6; }
      var spread = Math.max(0, DATA.stats.avg - DATA.stats.best4) * DATA.mwst / 100;
      var mitte = kwh * spread;
      var lo = Math.round(mitte * 0.6 / 5) * 5, hi = Math.round(mitte * 1.3 / 5) * 5;

      var urteil, klasse, gruende = [];
      if (!gross && (a.flexibel === 'kaum' || a.planbar === 'sehr' || mitte < 40)) {
        urteil = 'Eher nein. Ein fester Tarif passt wahrscheinlich besser zu Ihnen.'; klasse = 'nein';
      } else if (gross && a.planbar !== 'sehr') {
        urteil = 'Ja, ein Vergleich lohnt sich für Sie.'; klasse = 'ja';
      } else {
        urteil = 'Vielleicht. Prüfen Sie die Angebote genau.'; klasse = 'vielleicht';
      }
      if (a.auto === 'ja') { gruende.push('Ein E-Auto ist der größte Hebel: Die Ladung lässt sich meist problemlos in günstige Stunden legen.'); }
      if (a.heizung === 'ja') { gruende.push('Eine Wärmepumpe braucht viel Strom. Ein Teil davon lässt sich verschieben, weil das Haus die Wärme eine Weile hält.'); }
      if (a.speicher === 'ja') { gruende.push('Ein Speicher kann in günstigen Stunden laden und später abgeben.'); }
      if (!gross) { gruende.push('Ohne große Verbraucher bleibt beim Verschieben meist nur wenig übrig. Waschmaschine und Geschirrspüler bringen jeweils nur Cent pro Durchlauf.'); }
      if (a.flexibel === 'kaum') { gruende.push('Wenn Sie Geräte kaum zeitversetzt starten können, zahlen Sie oft den Durchschnittspreis oder mehr.'); }
      if (a.planbar === 'sehr') { gruende.push('Bei einem dynamischen Tarif schwanken die Monatskosten. Wer Planbarkeit schätzt, ist mit einem Festpreis entspannter.'); }

      var html = '<h2>Ihr Ergebnis</h2><p class="urteil ' + klasse + '">' + urteil + '</p>';
      html += '<ul class="gruende">' + gruende.map(function (g) { return '<li>' + g + '</li>'; }).join('') + '</ul>';
      html += '<div class="werte"><div class="wert ' + (klasse === 'ja' ? 'gut' : '') + '"><span class="titel">Möglicher Vorteil durch Verschieben</span><span class="zahl">' + (hi < 5 ? 'unter 5 €' : lo + ' bis ' + hi + ' €') + '</span><span>pro Jahr, grob geschätzt</span></div>';
      html += '<div class="wert"><span class="titel">Davon verschiebbar</span><span class="zahl">ca. ' + fmt(Math.round(kwh / 50) * 50, 0) + ' kWh</span><span>pro Jahr</span></div></div>';
      html += '<p class="leise">Geschätzt mit dem Preisabstand der letzten ' + DATA.stats.n + ' Tage: Die günstigsten 4 Stunden waren im Schnitt ' + fmt(spread * 100, 1) + ' Cent pro kWh billiger als der Tagesschnitt (mit Mehrwertsteuer). Mehrkosten für Grundpreis und Messstelle sind nicht abgezogen.</p>';

      html += '<h2>Ihre nächsten Schritte</h2><ol>';
      if (a.zaehler === 'ja') {
        html += '<li>Sie haben bereits einen Smart Meter. Damit fehlt nur noch der passende Tarif.</li>';
      } else if (a.zaehler === 'nein') {
        html += '<li>Fragen Sie Ihren Messstellenbetreiber nach dem Einbau eines intelligenten Messsystems. Wer das ist, steht auf Ihrer Stromrechnung.</li>';
      } else {
        html += '<li>Klären Sie, welchen Zähler Sie haben: Das steht auf Ihrer Stromrechnung, oder Sie fragen Ihren Messstellenbetreiber.</li>';
      }
      if (klasse !== 'nein') {
        html += '<li>Vergleichen Sie Angebote: Arbeitspreis-Aufschlag, Grundpreis und Kosten der Messstelle. Im <a href="tarifvergleich.html">Tarifvergleich</a> rechnen Sie mit Ihren Zahlen nach.</li>';
      } else {
        html += '<li>Prüfen Sie lieber, ob Ihr fester Tarif noch günstig ist. Ein Anbieterwechsel bringt oft mehr als die Uhrzeit.</li>';
      }
      if (a.auto === 'ja' || a.heizung === 'ja' || a.speicher === 'ja') {
        html += '<li>Fragen Sie Ihren Netzbetreiber nach günstigeren Netzentgelten nach <a href="wissen/paragraf-14a-netzentgelte.html">§ 14a</a>. Das geht auch ohne dynamischen Tarif.</li>';
      }
      html += '<li>Bei Unsicherheit berät die Verbraucherzentrale unabhängig.</li></ol>';
      if (klasse !== 'nein' && DATA.affiliate && DATA.affiliate.url) {
        html += '<p class="werbung"><span class="leise">Werbung: Bei diesem Link erhalten wir eventuell eine Provision. Für Sie ändert sich der Preis nicht.</span><br><a href="' + DATA.affiliate.url + '" rel="sponsored nofollow noopener" target="_blank">' + DATA.affiliate.label + '</a></p>';
      }
      out.innerHTML = html;
    }
    f.addEventListener('change', calc);
    f.addEventListener('submit', function (e) { e.preventDefault(); });
    calc();
  }

  function initLiveDevices() {
    var cells = document.querySelectorAll('[data-live-hours]');
    if (!cells.length || idx < 0) { return; }
    for (var i = 0; i < cells.length; i++) {
      var h = parseInt(cells[i].getAttribute('data-live-hours'), 10);
      var b = pickBest(allWindows(idx, h), false);
      if (b) { cells[i].textContent = winLabel(b, h); }
    }
  }

  function initPrint() {
    var pr = document.querySelectorAll('[data-print]');
    for (var j = 0; j < pr.length; j++) { pr[j].addEventListener('click', function () { window.print(); }); }
  }

  [markNow, initHero, initLiveDevices, initRechner, initTarif, initCheck, initPrint].forEach(function (fn) {
    try { fn(); } catch (e) { if (window.console) { console.error(e); } }
  });
})();
'''

# ----------------------------------------------------------------------------
# 7. TEMPLATES
# ----------------------------------------------------------------------------
TEMPLATES = {
    "base.html": """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} | {{ site_name }}</title>
<meta name="description" content="{{ description }}">
<meta name="theme-color" content="#ffffff">
<meta name="color-scheme" content="light">
<link rel="canonical" href="{{ site_url }}/{{ canon }}">
<meta property="og:type" content="website">
<meta property="og:title" content="{{ title }}">
<meta property="og:description" content="{{ description }}">
<meta property="og:url" content="{{ site_url }}/{{ canon }}">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='14' fill='%23c73e32'/%3E%3Cpath d='M16 2a14 14 0 0 1 14 14H16z' fill='%232e8540'/%3E%3Cpath d='M30 16a14 14 0 0 1-14 14V16z' fill='%23e3a615'/%3E%3Ccircle cx='16' cy='16' r='8' fill='%23fff'/%3E%3C/svg%3E">
<link rel="preload" href="{{ root }}assets/fonts/atkinson-400.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{{ root }}assets/style.css">
</head>
<body>
<a class="skip" href="#inhalt">Zum Inhalt springen</a>
<header class="kopf">
 <div class="wrap">
  <a class="marke" href="{{ root }}index.html"><svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="14" fill="#c73e32"/><path d="M16 2a14 14 0 0 1 14 14H16z" fill="#2e8540"/><path d="M30 16a14 14 0 0 1-14 14V16z" fill="#e3a615"/><circle cx="16" cy="16" r="8" fill="#fff"/><path d="M16 16V7" stroke="#1c2b39" stroke-width="2.5" stroke-linecap="round"/></svg>{{ site_name }}</a>
  <nav class="menue" aria-label="Hauptmenü">
  {% set items = [('heute','index.html','Heute'),('geraete','geraete/index.html','Geräte'),('rechner','rechner.html','Kostenrechner'),('tarife','wechsel-check.html','Wechsel-Check'),('statistik','statistik.html','Statistik'),('wissen','wissen/index.html','Wissen')] %}
  {% for key, href, label in items %}<a href="{{ root }}{{ href }}"{% if nav == key %} aria-current="page"{% endif %}>{{ label }}</a>{% endfor %}
  </nav>
 </div>
</header>
{% block hero %}{% endblock %}
<main id="inhalt" class="wrap">
{% block body %}{% endblock %}
{% if show_affiliate and affiliate %}
<aside class="werbung" aria-label="Werbung"><p class="leise">Werbung: Bei diesen Links erhalten wir eventuell eine Provision. Für Sie ändert sich der Preis nicht.</p>
<ul>{% for a in affiliate %}<li><a href="{{ a.url }}" rel="sponsored nofollow noopener" target="_blank">{{ a.label }}</a></li>{% endfor %}</ul></aside>
{% endif %}
</main>
<footer>
 <div class="wrap">
  <p>{{ source }}</p>
  <p>Alle Preise sind Börsenpreise (Day-Ahead) ohne Netzentgelte, Steuern, Umlagen und Aufschläge der Anbieter. Das ist keine Tarif- oder Anlageberatung. Angaben ohne Gewähr. {{ built_text }}</p>
  <nav class="fuss-nav" aria-label="Weitere Seiten"><a href="{{ root }}monat/index.html">Monatsrückblicke</a><a href="{{ root }}tarifvergleich.html">Tarifvergleich</a><a href="{{ root }}archiv.html">Archiv</a><a href="{{ root }}impressum.html">Impressum</a><a href="{{ root }}datenschutz.html">Datenschutz</a></nav>
 </div>
</footer>
{% if data_json %}<script id="preisdaten" type="application/json">{{ data_json }}</script>{% endif %}
<script src="{{ root }}assets/app.js" defer></script>
</body></html>""",

    "macros.html": """{% macro warten(d, root, titel) %}
<section class="warten {{ d.warten.lvl }}" aria-labelledby="w-{{ d.iso }}">
 <h2 id="w-{{ d.iso }}">Lohnt sich Warten {{ titel }}?</h2>
 <p class="warten-satz">{{ d.warten.kurz.replace("{wann}", titel) }}</p>
 <p>{{ d.warten.text }} Wer große Verbraucher in die günstigsten drei Stunden legt, zahlt rund <strong>{{ d.warten.diff|de1 }} Cent pro kWh</strong> weniger als im Tagesschnitt. Beim Laden eines E-Autos mit 20 kWh sind das etwa <strong>{{ d.warten.eauto|de2 }} €</strong>.</p>
</section>
{% endmacro %}
{% macro plan(d, root, titel) %}
<section class="tag" aria-labelledby="plan-{{ d.iso }}">
 <h2 id="plan-{{ d.iso }}">{{ titel }}</h2>
 <ol class="zeitleiste">{{ d.blocks_html }}</ol>
 <p class="beste">Am günstigsten drei Stunden am Stück: <strong>{{ d.windows[3].h_from }} bis {{ d.windows[3].h_to }} Uhr</strong>.</p>
 <p><a href="{{ root }}tag/{{ d.iso }}.html">Alle Stunden mit Preisen</a></p>
</section>
{% endmacro %}""",

    "index.html": """{% extends "base.html" %}
{% from "macros.html" import plan, warten %}
{% block hero %}
<div class="wrap frage"><h1>Wann ist Strom heute günstig?</h1></div>
<section id="jetzt" class="lage" data-lvl="" aria-live="polite" aria-label="Strompreis jetzt">
 <div class="wrap lage-innen">
  {{ today_dial }}
  <div class="lage-text">
   <p class="satz" id="jetzt-satz">Die Preise werden geladen.</p>
   <p class="bis" id="jetzt-bis"></p>
   <p id="jetzt-hint"></p>
   <p id="jetzt-next"></p>
   <p id="jetzt-beste"></p>
   <p class="leise" id="jetzt-preis"></p>
   <p class="leise">Insgesamt ist Strom {{ "heute" if ref_warten_titel == "heute" else ref_warten_titel }} {{ ref_day.niveau.word }} im Schnitt der letzten {{ ref_day.niveau.n }} Tage: {{ ref_day.niveau.avg|de1 }} statt {{ ref_day.niveau.ref|de1 }} Cent pro kWh an der Börse. Die Farben der Uhr vergleichen die Stunden dieses Tages.</p>
  </div>
 </div>
</section>
{% endblock %}
{% block body %}
<noscript><p class="hinweis">Für den Zeiger und den Satz „jetzt“ braucht Ihr Browser JavaScript. Den Tagesplan sehen Sie auch ohne.</p></noscript>
<p class="leise noprint" style="margin-top:1rem">So lesen Sie die Uhr: Sie zeigt den ganzen Tag, 0 Uhr ist oben, 12 Uhr unten. Grün heißt günstig, gelb normal, rot teuer. Der Zeiger steht auf der aktuellen Uhrzeit.</p>
{{ warten(ref_day, root, ref_warten_titel) }}
{% if tomorrow_day and tomorrow_day.iso != ref_day.iso %}<p class="warten-morgen"><strong>Und morgen?</strong> {{ tomorrow_day.warten.kurz.replace("{wann}", "morgen") }} Unterschied: {{ tomorrow_day.warten.diff|de1 }} Cent pro kWh.</p>{% endif %}
<p class="leise">Günstige Stunden sind oft Stunden mit viel Wind- und Sonnenstrom. Verschieben hilft deshalb auch dem Klima, selbst mit festem Tarif. <a href="{{ root }}wissen/guenstiger-strom-gruener-strom.html">Warum das so ist</a></p>
<div class="tage">
{% for d, titel in shown %}{{ plan(d, root, titel) }}{% endfor %}
</div>
{% if not has_today %}<p class="hinweis">Die neuesten Preise konnten noch nicht geladen werden. Angezeigt wird der letzte bekannte Tag.</p>
{% elif not has_tomorrow %}<p class="hinweis">Die Preise für morgen stehen jeden Tag ab etwa 14 Uhr fest. Danach erscheint hier der Plan für morgen.</p>{% endif %}
<p class="noprint"><button type="button" class="textknopf" data-print>Tagesplan drucken</button></p>

<h2>Wann soll ich welches Gerät einschalten?</h2>
<div class="scroll"><table class="fahrplan">
<tr><th>Gerät</th><th>Beste Startzeit</th><th class="zahl">Ersparnis</th></tr>
{% for v in ref_devices %}<tr><td><a href="{{ root }}geraete/{{ v.slug }}.html">{{ v.name }}</a></td><td data-live-hours="{{ v.hours }}">{{ ref_label }} {{ v.best.h_from }} bis {{ v.best.h_to }} Uhr</td><td class="zahl">{{ v.save_avg|de2 }} €</td></tr>{% endfor %}
</table></div>
<p class="leise">Die Startzeit wird ab der aktuellen Stunde berechnet. Ersparnis je Durchlauf gegenüber einem Start zu einer durchschnittlichen Zeit, mit Mehrwertsteuer. Das gilt nur mit dynamischem Stromtarif.</p>

<h2>Habe ich überhaupt etwas davon?</h2>
<p>Nur mit einem dynamischen Stromtarif zahlen Sie zu günstigen Zeiten weniger. Ob sich so ein Tarif für Sie lohnt, finden Sie im <a href="{{ root }}wechsel-check.html">Wechsel-Check</a> mit sechs kurzen Fragen heraus. Was Verschieben in den letzten Wochen gebracht hätte, zeigt der <a href="{{ root }}monat/index.html">Monatsrückblick</a>.</p>
<p>Was ein bestimmtes Gerät zu welcher Uhrzeit kostet, rechnet der <a href="{{ root }}rechner.html">Kostenrechner</a> aus. Begriffe erklären wir unter <a href="{{ root }}wissen/index.html">Wissen</a>.</p>
{% endblock %}""",

    "day.html": """{% extends "base.html" %}
{% from "macros.html" import warten %}
{% block body %}
<h1 style="margin-top:1.6rem">Strompreis am {{ d.pretty }}</h1>
<div class="lage-innen" style="padding:0">
 {{ d.dial }}
 <div>
  <ol class="zeitleiste">{{ d.blocks_html }}</ol>
  <p class="beste">Am günstigsten drei Stunden am Stück: <strong>{{ d.windows[3].h_from }} bis {{ d.windows[3].h_to }} Uhr</strong>.</p>
 </div>
</div>
<div class="werte">
 <div class="wert"><span class="titel">Durchschnitt</span><span class="zahl">{{ d.avg|de1 }} ct/kWh</span></div>
 <div class="wert gut"><span class="titel">Günstigste Stunde</span><span class="zahl">{{ d.cheap.hour }} Uhr</span><span>{{ d.cheap.ct|de1 }} ct/kWh</span></div>
 <div class="wert schlecht"><span class="titel">Teuerste Stunde</span><span class="zahl">{{ d.pricey.hour }} Uhr</span><span>{{ d.pricey.ct|de1 }} ct/kWh</span></div>
</div>
{{ warten(d, root, "an diesem Tag") }}
<p class="leise">Insgesamt war Strom an diesem Tag {{ d.niveau.word }} im Schnitt der letzten {{ d.niveau.n }} Tage ({{ d.niveau.avg|de1 }} statt {{ d.niveau.ref|de1 }} Cent pro kWh). Die Farben vergleichen die Stunden innerhalb des Tages.</p>
{% if d.negative_hours %}<p class="hinweis">An diesem Tag war der Börsenpreis {{ d.negative_hours }} Stunde(n) lang negativ. <a href="{{ root }}wissen/negative-strompreise.html">Was das bedeutet</a></p>{% endif %}
<h2>Verlauf über den Tag</h2>
{{ d.chart }}
<p class="leise">Je höher die Säule, desto teurer. Die Zahlen darunter sind Uhrzeiten.</p>
<h2>Wann lohnt sich welches Gerät?</h2>
<div class="scroll"><table class="fahrplan"><tr><th>Gerät</th><th>Beste Zeit</th><th class="zahl">Ersparnis</th></tr>
{% for v in d.devices %}<tr><td><a href="{{ root }}geraete/{{ v.slug }}.html">{{ v.name }}</a></td><td>{{ v.best.h_from }} bis {{ v.best.h_to }} Uhr</td><td class="zahl">{{ v.save_avg|de2 }} €</td></tr>{% endfor %}</table></div>
<p class="leise">Ersparnis je Durchlauf gegenüber einer durchschnittlichen Startzeit, mit Mehrwertsteuer. Nur mit dynamischem Tarif.</p>
<h2>Alle Stunden</h2>
<div class="scroll"><table class="tabelle"><tr><th>Uhrzeit</th><th>Börsenpreis</th><th>Einstufung</th></tr>{{ d.table }}</table></div>
<nav class="weiter" aria-label="Weitere Tage">
 <span>{% if prev_day %}<a href="{{ prev_day.iso }}.html">Vorheriger Tag ({{ prev_day.short }})</a>{% endif %}</span>
 <span><a href="{{ root }}archiv.html">Alle Tage</a></span>
 <span>{% if next_day %}<a href="{{ next_day.iso }}.html">Nächster Tag ({{ next_day.short }})</a>{% endif %}</span>
</nav>
{% endblock %}""",

    "devices_index.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Wann soll ich welches Gerät einschalten?</h1>
<p>Klicken Sie auf Ihr Gerät. Sie sehen dann die günstigste Uhrzeit für heute und, ab etwa 14 Uhr, auch für morgen.</p>
<div class="scroll"><table class="fahrplan">
<tr><th>Gerät</th><th>Beste Zeit {{ ref_label }}</th><th class="zahl">Verbrauch</th></tr>
{% for v in ref_devices %}<tr><td><a href="{{ root }}geraete/{{ v.slug }}.html">{{ v.name }}</a></td><td>{{ v.best.h_from }} bis {{ v.best.h_to }} Uhr</td><td class="zahl">{{ v.kwh|de1 }} kWh</td></tr>{% endfor %}
</table></div>
<p class="hinweis">Die Uhrzeit macht nur mit einem <a href="{{ root }}wissen/dynamischer-stromtarif.html">dynamischen Stromtarif</a> einen Unterschied auf Ihrer Rechnung. Mit einem Festpreis kostet Strom immer gleich viel.</p>
{% endblock %}""",

    "device.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Wann soll ich {{ dev.frage }}?</h1>
<p>Gerechnet mit {{ dev.kwh|de1 }} kWh Stromverbrauch und etwa {{ dev.hours }} Stunden Laufzeit pro Durchlauf.</p>
{% for e in entries %}
<section>
 <h2>{{ e.titel }}</h2>
 <p class="satz" style="font-size:1.6rem">Am besten von {{ e.dev.best.h_from }} bis {{ e.dev.best.h_to }} Uhr.</p>
 <div class="lage-innen" style="padding:0;grid-template-columns:minmax(0,13rem) minmax(0,1fr)">
  {{ e.dev.dial }}
  <div>
   <p>Der dunkle Bogen am Rand der Uhr zeigt die beste Zeit.</p>
   <p>Am teuersten wäre es von {{ e.dev.worst.h_from }} bis {{ e.dev.worst.h_to }} Uhr.</p>
   <p>Gegenüber einem Start zu einer durchschnittlichen Zeit sparen Sie ca. <strong>{{ e.dev.save_avg|de2 }} €</strong> je Durchlauf (mit dynamischem Tarif, inklusive Mehrwertsteuer).</p>
  </div>
 </div>
</section>
{% endfor %}
{% if not has_tomorrow %}<p class="hinweis">Die Zeiten für morgen stehen jeden Tag ab etwa 14 Uhr fest.</p>{% endif %}
<h2>Lohnt sich das?</h2>
<p>Bei {{ dev.per_week }} Durchläufen pro Woche sind das grob geschätzt <strong>{{ ref.save_year|de2 }} € im Jahr</strong>. {% if ref.save_year < 10 %}Bei kleinen Geräten bleibt es bei wenigen Euro. Spürbar wird es bei Geräten mit hohem Verbrauch wie E-Auto, Warmwasserspeicher oder Wärmepumpe.{% else %}Bei diesem Gerät kann sich das Verschieben bemerkbar machen.{% endif %}</p>
<p class="leise">Die Schätzung beruht auf den Preisen dieses Tages. Jeder Tag ist anders.</p>
<p><a class="knopf" href="{{ root }}rechner.html?geraet={{ dev.slug }}">Mit meinen Werten rechnen</a></p>
<p class="noprint"><a href="{{ root }}geraete/index.html">Alle Geräte</a></p>
{% endblock %}""",

    "rechner.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Was kostet mein Gerät zu welcher Uhrzeit?</h1>
<p>Wählen Sie Ihr Gerät. Der Rechner zeigt, was ein Durchlauf jetzt, zur günstigsten und zur teuersten Zeit kostet.</p>
<form id="rechner" class="rechner" novalidate autocomplete="off">
 <div class="feld"><label for="geraet">Gerät</label><select id="geraet" name="geraet"></select></div>
 <div class="feld"><label for="kwh">Stromverbrauch pro Durchlauf in kWh</label><input id="kwh" name="kwh" type="text" inputmode="decimal"><span class="hilfe">Steht meist in der Bedienungsanleitung oder auf dem Energielabel.</span></div>
 <div class="feld"><label for="dauer">Laufzeit in ganzen Stunden</label><input id="dauer" name="dauer" type="text" inputmode="numeric"></div>
 <div class="feld"><label for="woche">Durchläufe pro Woche</label><input id="woche" name="woche" type="text" inputmode="numeric"></div>
 <div class="feld"><label for="aufschlag">Ihr Aufschlag auf den Börsenpreis in Cent pro kWh, ohne Mehrwertsteuer</label><input id="aufschlag" name="aufschlag" type="text" inputmode="decimal"><span class="hilfe">Voreingestellt ist ein Beispielwert. Gemeint sind Netzentgelte, Steuern, Umlagen und der Aufschlag Ihres Anbieters. Den genauen Wert finden Sie im Preisblatt Ihres Tarifs. <a href="{{ root }}wissen/boersenpreis-und-endpreis.html">Mehr dazu</a></span></div>
</form>
<section id="ergebnis" class="ergebnis" aria-live="polite"><noscript>Der Rechner braucht JavaScript.</noscript></section>
<p class="leise">Der Rechner läuft nur in Ihrem Browser. Ihre Eingaben werden weder übertragen noch gespeichert.</p>
{% endblock %}""",

    "tarife.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Fester oder dynamischer Stromtarif?</h1>
<p>Beim festen Tarif kostet die Kilowattstunde immer gleich viel. Beim dynamischen Tarif ändert sich der Preis jede Stunde mit dem Börsenpreis.</p>
<div class="scroll"><table class="tabelle">
<tr><th></th><th>Fester Tarif</th><th>Dynamischer Tarif</th></tr>
<tr><td><strong>Preis</strong></td><td>bleibt während der Vertragslaufzeit gleich</td><td>ändert sich jede Stunde</td></tr>
<tr><td><strong>Planbarkeit</strong></td><td>Sie wissen vorher, was Strom kostet</td><td>die Monatskosten schwanken</td></tr>
<tr><td><strong>Technik</strong></td><td>normaler Zähler genügt</td><td>in der Regel ein Smart Meter</td></tr>
<tr><td><strong>Passt zu</strong></td><td>allen, die feste Kosten wollen</td><td>Haushalten, die Verbrauch verschieben können, zum Beispiel mit E-Auto, Wärmepumpe oder Speicher</td></tr>
<tr><td><strong>Risiko</strong></td><td>der Preis kann bei Vertragsende steigen</td><td>teure Stunden schlagen voll durch</td></tr>
</table></div>
<h2>Rechnen Sie mit Ihren Zahlen</h2>
<p>Die voreingestellten Werte sind Beispiele. Ersetzen Sie sie durch Ihre eigenen.</p>
<form id="tarifrechner" class="rechner" novalidate autocomplete="off">
 <div class="feld"><label for="verbrauch">Stromverbrauch pro Jahr in kWh</label><input id="verbrauch" name="verbrauch" type="text" inputmode="numeric" value="3000"><span class="hilfe">Steht auf Ihrer Jahresabrechnung.</span></div>
 <h3>Ihr jetziger Tarif</h3>
 <div class="feld"><label for="fest_preis">Arbeitspreis in Cent pro kWh, mit Mehrwertsteuer</label><input id="fest_preis" name="fest_preis" type="text" inputmode="decimal" value="32"></div>
 <div class="feld"><label for="fest_grund">Grundpreis in Euro pro Monat, mit Mehrwertsteuer</label><input id="fest_grund" name="fest_grund" type="text" inputmode="decimal" value="12"></div>
 <h3>Dynamischer Tarif</h3>
 <div class="feld"><label for="dyn_aufschlag">Aufschlag auf den Börsenpreis in Cent pro kWh, ohne Mehrwertsteuer</label><input id="dyn_aufschlag" name="dyn_aufschlag" type="text" inputmode="decimal"><span class="hilfe">Netzentgelte, Steuern, Umlagen und Anbieteraufschlag. Steht im Preisblatt des Angebots.</span></div>
 <div class="feld"><label for="dyn_grund">Grundpreis in Euro pro Monat, mit Mehrwertsteuer und Messstelle</label><input id="dyn_grund" name="dyn_grund" type="text" inputmode="decimal" value="15"></div>
 <div class="feld"><label for="anteil">Anteil Ihres Verbrauchs in günstigen Stunden: <span id="anteil-wert"></span></label><input id="anteil" name="anteil" type="range" min="0" max="80" step="5" value="20"><span class="hilfe">0 % heißt: Sie ändern nichts. 20 % heißt zum Beispiel: Waschmaschine und E-Auto laufen meist in günstigen Stunden.</span></div>
</form>
<section id="tarif-ergebnis" class="ergebnis" aria-live="polite"><noscript>Der Rechner braucht JavaScript.</noscript></section>
<h2>Vor einem Wechsel klären</h2>
<ol>
<li>Gibt es bei mir einen Smart Meter, oder kann einer eingebaut werden? Das weiß Ihr Messstellenbetreiber.</li>
<li>Was kosten Grundpreis und Messstelle im Jahr?</li>
<li>Kann ich wirklich Verbrauch in günstige Stunden legen?</li>
<li>Wie lange läuft der Vertrag, und wie schnell kann ich kündigen?</li>
<li>Komme ich mit schwankenden Monatskosten zurecht?</li>
</ol>
<p>Mehr dazu: <a href="{{ root }}wissen/lohnt-sich-dynamischer-tarif.html">Lohnt sich ein dynamischer Tarif für mich?</a> Unabhängig beraten auch die Verbraucherzentralen.</p>
{% endblock %}""",

    "check.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Wechsel-Check: Lohnt sich ein dynamischer Stromtarif für mich?</h1>
<p>Sechs kurze Fragen, eine ehrliche Antwort. Der Check läuft nur in Ihrem Browser, Ihre Antworten werden nicht übertragen.</p>
<form id="check" class="rechner" novalidate autocomplete="off">
 <fieldset class="frage-block"><legend>1. Laden Sie ein Elektroauto zu Hause?</legend>
  <div class="auswahl"><label><input type="radio" name="auto" value="ja">Ja</label><label><input type="radio" name="auto" value="nein">Nein</label></div></fieldset>
 <fieldset class="frage-block"><legend>2. Heizen Sie mit Strom, zum Beispiel mit einer Wärmepumpe?</legend>
  <div class="auswahl"><label><input type="radio" name="heizung" value="ja">Ja</label><label><input type="radio" name="heizung" value="nein">Nein</label></div></fieldset>
 <fieldset class="frage-block"><legend>3. Haben Sie einen Batteriespeicher oder einen Warmwasserspeicher mit Heizstab?</legend>
  <div class="auswahl"><label><input type="radio" name="speicher" value="ja">Ja</label><label><input type="radio" name="speicher" value="nein">Nein</label></div></fieldset>
 <fieldset class="frage-block"><legend>4. Wie oft können Sie Geräte zeitversetzt starten?</legend>
  <span class="hilfe" style="margin:-.3rem 0 .5rem">Zum Beispiel mit der Startzeitvorwahl der Waschmaschine, einer Zeitschaltuhr oder per App.</span>
  <div class="auswahl"><label><input type="radio" name="flexibel" value="oft">Oft</label><label><input type="radio" name="flexibel" value="manchmal">Manchmal</label><label><input type="radio" name="flexibel" value="kaum">Kaum</label></div></fieldset>
 <fieldset class="frage-block"><legend>5. Haben Sie schon einen Smart Meter (intelligentes Messsystem)?</legend>
  <div class="auswahl"><label><input type="radio" name="zaehler" value="ja">Ja</label><label><input type="radio" name="zaehler" value="nein">Nein</label><label><input type="radio" name="zaehler" value="unklar">Weiß ich nicht</label></div></fieldset>
 <fieldset class="frage-block"><legend>6. Wie wichtig sind Ihnen feste, planbare Stromkosten?</legend>
  <div class="auswahl"><label><input type="radio" name="planbar" value="sehr">Sehr wichtig</label><label><input type="radio" name="planbar" value="egal">Nicht so wichtig</label></div></fieldset>
</form>
<section id="check-ergebnis" class="ergebnis" aria-live="polite"><noscript>Der Check braucht JavaScript.</noscript></section>
<h2>Gut zu wissen</h2>
<ul>
<li>Die Schätzung zeigt nur den Vorteil durch Verschieben. Grundpreis und Kosten der Messstelle unterscheiden sich je nach Angebot und kommen dazu.</li>
<li>Mit Wärmepumpe oder Wallbox gibt es zusätzlich günstigere Netzentgelte nach <a href="{{ root }}wissen/paragraf-14a-netzentgelte.html">§ 14a</a>.</li>
<li>Mit konkreten Angeboten rechnen Sie im <a href="{{ root }}tarifvergleich.html">Tarifvergleich</a> genauer nach.</li>
</ul>
{% endblock %}""",

    "month.html": """{% extends "base.html" %}
{% block body %}
<p style="margin-top:1.4rem"><a href="index.html">Alle Monatsrückblicke</a></p>
<h1>Strompreis im {{ m.name }}: der Rückblick</h1>
{% if not m.complete %}<p class="hinweis">Dieser Monat läuft noch. Ausgewertet sind die ersten {{ m.n }} Tage bis gestern.</p>{% endif %}
<p class="satz" style="font-size:1.5rem">{% if m.spread >= warten_stark %}Verschieben hat sich im {{ m.name.split(' ')[0] }} deutlich gelohnt.{% elif m.spread >= warten_etwas %}Verschieben hat sich im {{ m.name.split(' ')[0] }} etwas gelohnt.{% else %}Verschieben hat sich im {{ m.name.split(' ')[0] }} kaum gelohnt.{% endif %}</p>
<p>Im Schnitt waren die günstigsten drei Stunden eines Tages <strong>{{ m.spread|de1 }} Cent pro kWh</strong> billiger als der Tagesdurchschnitt (mit Mehrwertsteuer). An {{ m.counts.stark }} {{ 'Tag' if m.counts.stark == 1 else 'Tagen' }} hat sich Warten deutlich gelohnt, an {{ m.counts.etwas }} {{ 'Tag' if m.counts.etwas == 1 else 'Tagen' }} etwas und an {{ m.counts.kaum }} {{ 'Tag' if m.counts.kaum == 1 else 'Tagen' }} kaum.</p>
<h2>Was hätte ein Haushalt gespart?</h2>
<p>Angenommen, ein Haushalt mit dynamischem Tarif hätte seine Geräte immer zur günstigsten Zeit gestartet statt zu einer durchschnittlichen Zeit:</p>
<div class="scroll"><table class="fahrplan">
<tr><th>Gerät</th><th class="zahl">Durchläufe</th><th class="zahl">je Durchlauf</th><th class="zahl">im Monat</th></tr>
{% for d in m.devices %}<tr><td><a href="{{ root }}geraete/{{ d.slug }}.html">{{ d.name }}</a></td><td class="zahl">{{ d.runs|round|int }}</td><td class="zahl">{{ d.per_run|de2 }} €</td><td class="zahl">{{ d.total|de2 }} €</td></tr>{% endfor %}
</table></div>
<p>Ein Haushalt mit E-Auto an der Wallbox, Waschmaschine und Geschirrspüler hätte damit etwa <strong>{{ m.haushalt|de2 }} €</strong> gespart. Ohne E-Auto sind es meist nur wenige Euro.</p>
<p class="leise">Durchläufe pro Woche wie im <a href="{{ root }}rechner.html">Kostenrechner</a> voreingestellt. Ersparnis gegenüber einem Start zur durchschnittlichen Tageszeit, mit Mehrwertsteuer. Mehrkosten für Grundpreis und Messstelle sind nicht abgezogen.</p>
<h2>Der Monat in Zahlen</h2>
<div class="werte">
 <div class="wert"><span class="titel">Durchschnitt Börsenpreis</span><span class="zahl">{{ m.avg|de1 }} ct/kWh</span>{% if m.prev %}<span>{% if (m.avg - m.prev.avg)|abs < 0.05 %}etwa gleich wie im {{ m.prev.name }}{% elif m.avg < m.prev.avg %}{{ (m.prev.avg - m.avg)|de1 }} ct weniger als im {{ m.prev.name }}{% else %}{{ (m.avg - m.prev.avg)|de1 }} ct mehr als im {{ m.prev.name }}{% endif %}</span>{% endif %}</div>
 <div class="wert gut"><span class="titel">Günstigster Tag</span><span class="zahl">{{ m.cheapest_day.short }}</span><span>Ø {{ m.cheapest_day.avg|de1 }} ct/kWh</span></div>
 <div class="wert schlecht"><span class="titel">Teuerster Tag</span><span class="zahl">{{ m.priciest_day.short }}</span><span>Ø {{ m.priciest_day.avg|de1 }} ct/kWh</span></div>
 <div class="wert"><span class="titel">Günstigster Wochentag</span><span class="zahl">{{ m.best_weekday }}</span></div>
 <div class="wert"><span class="titel">Stunden mit negativem Preis</span><span class="zahl">{{ m.negative_hours }}</span></div>
 <div class="wert gut"><span class="titel">Günstigste Stunde</span><span class="zahl">{{ m.cheapest_hour[1].ct|de1 }} ct/kWh</span><span>{{ m.cheapest_hour[0].short }}, {{ m.cheapest_hour[1].hour }} Uhr</span></div>
</div>
<h2>Jeder Tag im {{ m.name.split(' ')[0] }}</h2>
<p>Jede Zeile ist ein Tag, oben der Monatsanfang. Links beginnt der Tag um Mitternacht, rechts endet er. Grün heißt günstig, gelb normal, rot teuer.</p>
<div class="kalender">{{ m.calendar }}</div>
<nav class="weiter" aria-label="Weitere Monate">
 <span>{% if m.prev %}<a href="{{ m.prev.key }}.html">{{ m.prev.name }}</a>{% endif %}</span>
 <span>{% if m.next %}<a href="{{ m.next.key }}.html">{{ m.next.name }}</a>{% endif %}</span>
</nav>
<p>Lohnt sich ein dynamischer Tarif für Sie? Das zeigt der <a href="{{ root }}wechsel-check.html">Wechsel-Check</a>.</p>
{% endblock %}""",

    "months_index.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Monatsrückblicke</h1>
<p>Was hätte Verschieben in den vergangenen Monaten gebracht? Jeden Monat rechnen wir es mit den echten Börsenpreisen nach.</p>
{% if months %}
<div class="scroll"><table class="fahrplan">
<tr><th>Monat</th><th class="zahl">Ø Börsenpreis</th><th class="zahl">Vorteil günstige Stunden</th><th class="zahl">Haushalt mit E-Auto</th></tr>
{% for m in months|reverse %}<tr><td><a href="{{ m.key }}.html">{{ m.name }}</a>{% if not m.complete %} (läuft noch){% endif %}</td><td class="zahl">{{ m.avg|de1 }} ct</td><td class="zahl">{{ m.spread|de1 }} ct/kWh</td><td class="zahl">{{ m.haushalt|de2 }} €</td></tr>{% endfor %}
</table></div>
<p class="leise">„Vorteil günstige Stunden“: Um so viel waren die günstigsten drei Stunden eines Tages im Schnitt billiger als der Tagesdurchschnitt, mit Mehrwertsteuer.</p>
{% else %}<p>Der erste Rückblick erscheint, sobald genug Tage gesammelt sind.</p>{% endif %}
{% endblock %}""",

    "stats.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Wann ist Strom meistens günstig?</h1>
<p>Ausgewertet sind die letzten {{ stats.n_days }} Tage.{% if stats.n_days < 14 %} Das sind noch wenige, die Werte sind deshalb nur ein erster Eindruck.{% endif %} Einzelne Monate finden Sie in den <a href="{{ root }}monat/index.html">Monatsrückblicken</a>.</p>
{% if stats.profile_best3 %}
<h2>Die beste Uhrzeit</h2>
<p class="satz" style="font-size:1.6rem">Im Schnitt zwischen {{ stats.profile_best3.h_from }} und {{ stats.profile_best3.h_to }} Uhr.</p>
{{ stats.profile_chart }}
<p class="leise">Durchschnittlicher Börsenpreis je Uhrzeit. Je höher die Säule, desto teurer.</p>
{% endif %}
<h2>Der günstigste Wochentag</h2>
<div class="scroll"><table class="tabelle"><tr><th>Wochentag</th><th>Durchschnitt</th><th>Günstige Stunden billiger um</th></tr>
{% for w in stats.weekdays %}<tr><td>{% if stats.best_weekday and w.name == stats.best_weekday.name %}<strong>{{ w.name }}</strong>{% else %}{{ w.name }}{% endif %}</td><td>{{ w.avg|de1 }} ct/kWh</td><td>{{ w.green|de1 }} ct/kWh</td></tr>{% endfor %}
</table></div>
{% if stats.best_weekday %}<p>Am günstigsten war im Schnitt der <strong>{{ stats.best_weekday.name }}</strong>. Die rechte Spalte zeigt, um wie viel die günstigsten drei Stunden billiger waren als der Tagesschnitt, mit Mehrwertsteuer. Je größer der Wert, desto mehr lohnt sich Verschieben an diesem Wochentag.</p>{% endif %}
<h2>In Zahlen</h2>
<div class="werte">
 <div class="wert"><span class="titel">Durchschnitt</span><span class="zahl">{{ stats.avg|de1 }} ct/kWh</span></div>
 <div class="wert gut"><span class="titel">Günstigste Stunde</span><span class="zahl">{{ stats.cheapest.ct|de1 }} ct/kWh</span><span>{{ stats.cheapest.date }}, {{ stats.cheapest.hour }} Uhr</span></div>
 <div class="wert schlecht"><span class="titel">Teuerste Stunde</span><span class="zahl">{{ stats.priciest.ct|de1 }} ct/kWh</span><span>{{ stats.priciest.date }}, {{ stats.priciest.hour }} Uhr</span></div>
 <div class="wert"><span class="titel">Stunden mit negativem Preis</span><span class="zahl">{{ stats.negative_hours }}</span></div>
</div>
<h2>Der Strom-Kalender</h2>
<p>Jede Zeile ist ein Tag. Links beginnt der Tag um Mitternacht, rechts endet er. Grün heißt günstig, gelb normal, rot teuer. Ein Klick öffnet den Tag.</p>
<div class="kalender">{{ calendar }}</div>
<p class="leise">Die Farben vergleichen die Stunden innerhalb des jeweiligen Tages. <a href="{{ root }}wissen/so-liest-man-die-strom-uhr.html">So entsteht die Einstufung</a></p>
{% endblock %}""",

    "archive.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Alle Tage</h1>
<div class="scroll"><table class="tabelle"><tr><th>Tag</th><th>Durchschnitt</th><th>Günstigste drei Stunden</th></tr>
{% for d in days %}<tr><td><a href="{{ root }}tag/{{ d.iso }}.html">{{ d.pretty }}</a></td><td>{{ d.avg|de1 }} ct/kWh</td><td>{{ d.windows[3].h_from }} bis {{ d.windows[3].h_to }} Uhr</td></tr>{% endfor %}
</table></div>
{% endblock %}""",

    "wissen_index.html": """{% extends "base.html" %}
{% block body %}
<h1 style="margin-top:1.6rem">Strom einfach erklärt</h1>
<p>Ohne Fachsprache: was hinter der Uhr, dem Börsenpreis und dynamischen Tarifen steckt.</p>
<ul class="verweise">
{% for w in wissen %}<li><a href="{{ w.slug }}.html">{{ w.title }}</a><span>{{ w.desc }}</span></li>{% endfor %}
</ul>
{% endblock %}""",

    "wissen_page.html": """{% extends "base.html" %}
{% block body %}
<p style="margin-top:1.4rem"><a href="index.html">Alle Erklärungen</a></p>
<h1>{{ w.title }}</h1>
{{ body }}
<nav class="weiter" aria-label="Weiter"><a href="{{ root }}index.html">Zum Strompreis heute</a><a href="index.html">Weitere Erklärungen</a></nav>
{% endblock %}""",

    "legal.html": """{% extends "base.html" %}
{% block body %}<h1 style="margin-top:1.6rem">{{ title }}</h1>{{ content }}{% endblock %}""",

    "notfound.html": """{% extends "base.html" %}
{% block body %}<h1 style="margin-top:1.6rem">Diese Seite gibt es nicht</h1>
<p>Vielleicht hat sich die Adresse geändert. Zur <a href="{{ site_url }}/index.html">Startseite</a>.</p>{% endblock %}""",
}

ENV = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(["html"], default_for_string=True))
ENV.filters["de1"] = de1
ENV.filters["de2"] = de2

# ----------------------------------------------------------------------------
# 8. ERKLÄRSEITEN (einfache Sprache; Variablen: root, thr, stats)
# ----------------------------------------------------------------------------
WISSEN = [
    {"slug": "dynamischer-stromtarif", "title": "Was ist ein dynamischer Stromtarif?",
     "desc": "Wie der Preis stündlich schwankt und was Sie dafür brauchen.",
     "body": """
<p>Kurz gesagt: Bei einem dynamischen Stromtarif ändert sich der Preis für eine Kilowattstunde jede Stunde. Bei einem normalen Tarif bleibt er über Monate gleich.</p>
<h2>So funktioniert es</h2>
<p>Strom wird an einer Börse gehandelt, ähnlich wie Obst auf dem Großmarkt. Scheint viel Sonne oder weht viel Wind, gibt es reichlich Strom und der Preis fällt. Wenn abends viele kochen und das Licht anschalten, ist Strom oft knapper und teurer. Ein dynamischer Tarif gibt diese Schwankungen an Sie weiter.</p>
<p>Die Preise für den nächsten Tag stehen ab etwa 13 bis 14 Uhr fest. Genau diese Preise zeigt die <a href="{{ root }}index.html">Strom-Uhr</a>.</p>
<h2>Was heißt das für mich?</h2>
<ul>
<li>Zu günstigen Zeiten kostet Strom weniger, zu teuren Zeiten mehr.</li>
<li>Wer Waschmaschine, Geschirrspüler, Warmwasser oder E-Auto in günstige Stunden legt, kann sparen.</li>
<li>Wer immer zur gleichen Zeit viel Strom braucht, spart meist wenig.</li>
</ul>
<h2>Was brauche ich dafür?</h2>
<p>Damit Ihr Verbrauch stundengenau abgerechnet werden kann, ist in der Regel ein <strong>Smart Meter</strong> (intelligentes Messsystem) nötig. So sieht es auch das Gesetz vor: Nach § 41a des Energiewirtschaftsgesetzes richtet sich das Angebot dynamischer Tarife an Kunden mit einem solchen Messsystem. Alle Stromlieferanten müssen seit dem 1. Januar 2025 einen dynamischen Tarif anbieten. Ob und wann bei Ihnen ein Smart Meter eingebaut werden kann, klärt Ihr Messstellenbetreiber.</p>
<p>Smart Meter sind noch selten. Nach Zahlen der Bundesnetzagentur hatten Mitte 2026 knapp 6 von 100 Netzanschlüssen in Deutschland ein solches Messsystem.</p>
<h2>Muss ich wechseln?</h2>
<p>Nein. Ob sich ein Wechsel für Sie lohnt, finden Sie mit dem <a href="{{ root }}wechsel-check.html">Wechsel-Check</a> in wenigen Fragen heraus.</p>
<h2 class="quellen">Quellen</h2>
<ul class="quellen"><li><a href="https://www.gesetze-im-internet.de/enwg_2005/__41a.html" rel="noopener">§ 41a Energiewirtschaftsgesetz (EnWG)</a></li>
<li><a href="https://www.cleanthinking.de/smart-meter-deutschland-2026/" rel="noopener">Smart-Meter-Rollout 2026, nach Zahlen der Bundesnetzagentur</a></li></ul>
"""},
    {"slug": "boersenpreis-und-endpreis", "title": "Börsenpreis und Strompreis: Was zahle ich wirklich?",
     "desc": "Warum der Börsenpreis nur ein Teil Ihrer Stromrechnung ist.",
     "body": """
<p>Der Preis, den Sie auf dieser Seite sehen, ist der Börsenpreis. Ihre Stromrechnung besteht aus mehr.</p>
<h2>Woraus der Strompreis besteht</h2>
<p>Nach der Auswertung des Bundesverbands der Energie- und Wasserwirtschaft (BDEW, Stand August 2026) kostet Strom für Haushalte im Durchschnitt 37,0 Cent pro Kilowattstunde. Das sind Neukundentarife bei 3.500 Kilowattstunden Jahresverbrauch.</p>
<div class="scroll"><table class="tabelle">
<tr><th>Bestandteil</th><th>Cent pro kWh</th><th>Anteil</th></tr>
<tr><td>Strombeschaffung und Vertrieb (hier steckt der Börsenpreis)</td><td>15,17</td><td>41 %</td></tr>
<tr><td>Netzentgelte (für die Stromleitungen)</td><td>9,24</td><td>25 %</td></tr>
<tr><td>Steuern, Abgaben und Umlagen (inklusive Mehrwertsteuer)</td><td>12,6</td><td>34 %</td></tr>
</table></div>
<h2>Was bedeutet das für die Strom-Uhr?</h2>
<p>Der Börsenpreis ist nur ein Teil der Beschaffungskosten. Netzentgelte, Steuern und Umlagen kommen dazu und sind in üblichen Haushaltstarifen meist unabhängig von der Uhrzeit. Deshalb schwankt Ihr Endpreis weniger stark als der Börsenpreis. Eine Ausnahme sind zeitvariable Netzentgelte für Wärmepumpen und Wallboxen, siehe <a href="{{ root }}wissen/paragraf-14a-netzentgelte.html">§ 14a einfach erklärt</a>.</p>
<h2>Ein Rechenbeispiel</h2>
<p>Kostet die Börse in einer Stunde 6 Cent und Ihr Aufschlag beträgt 18 Cent (Netzentgelte, Steuern, Umlagen und Anbieter, ohne Mehrwertsteuer), dann rechnet man: (6 + 18) × 1,19 = rund 28,6 Cent pro Kilowattstunde. Der Wert 1,19 steht für die 19 Prozent Mehrwertsteuer.</p>
<p>Rechnet man aus den BDEW-Zahlen die Mehrwertsteuer heraus, bleiben für Netzentgelte, Steuern und Umlagen rund 16 Cent. Dazu kommt der Aufschlag Ihres Anbieters. Deshalb ist im <a href="{{ root }}rechner.html">Kostenrechner</a> ein Beispielwert von 18 Cent voreingestellt. Ihren echten Wert finden Sie im Preisblatt Ihres Tarifs.</p>
<h2 class="quellen">Quellen</h2>
<ul class="quellen"><li><a href="https://www.bdew.de/service/daten-und-grafiken/bdew-strompreisanalyse/" rel="noopener">BDEW-Strompreisanalyse (Stand August 2026)</a></li></ul>
"""},
    {"slug": "so-liest-man-die-strom-uhr", "title": "So liest man die Strom-Uhr",
     "desc": "Was Grün, Gelb und Rot bedeuten und wie wir sie berechnen.",
     "body": """
<p>Die Strom-Uhr sieht aus wie eine Zeitschaltuhr: Der Ring zeigt den ganzen Tag, 0 Uhr ist oben, 12 Uhr unten. Jedes Feld ist eine Stunde. Der Zeiger steht auf der aktuellen Uhrzeit.</p>
<h2>Was die Farben bedeuten</h2>
<p>Die Farben vergleichen die Stunden eines Tages miteinander:</p>
<ul>
<li><strong>Grün (günstig):</strong> die günstigsten acht Stunden des Tages.</li>
<li><strong>Gelb (normal):</strong> die mittleren Stunden.</li>
<li><strong>Rot (teuer):</strong> die teuersten acht Stunden des Tages.</li>
</ul>
<p>So zeigt die Uhr auch an einem insgesamt teuren Tag, wann es heute am günstigsten ist. Liegen alle Stunden fast gleich, ist der ganze Tag gelb. Negative Preise sind immer grün.</p>
<p>Ob Strom insgesamt teuer oder günstig ist, steht in einem eigenen Satz: Wir vergleichen den Tagesdurchschnitt mit dem Schnitt der letzten {{ stats.n_days }} Tage.</p>
<h2>Lohnt sich Warten heute?</h2>
<p>Wie viel das Verschieben bringt, hängt vom Abstand zwischen günstigen und teuren Stunden ab. Deshalb bewerten wir jeden Tag zusätzlich:</p>
<ul>
<li><strong>Lohnt sich deutlich:</strong> Die günstigsten drei Stunden sind mindestens {{ warten_stark|de1 }} Cent pro kWh billiger als der Tagesschnitt (mit Mehrwertsteuer). Beim Laden eines E-Autos sind das 2 Euro oder mehr.</li>
<li><strong>Lohnt sich etwas:</strong> {{ warten_etwas|de1 }} bis {{ warten_stark|de1 }} Cent Unterschied.</li>
<li><strong>Lohnt sich kaum:</strong> weniger als {{ warten_etwas|de1 }} Cent. Dann müssen Sie sich keine Mühe machen.</li>
</ul>
<h2>Was die Uhr nicht zeigt</h2>
<ul>
<li>Sie zeigt nur den Börsenpreis. Ihr persönlicher Preis hängt von Ihrem Tarif ab, siehe <a href="{{ root }}wissen/boersenpreis-und-endpreis.html">Börsenpreis und Strompreis</a>.</li>
<li>Bei einem Festpreistarif ändert die Uhrzeit nichts an Ihrer Rechnung.</li>
<li>Es ist keine Vorhersage. Die Preise für morgen stehen erst am Nachmittag fest.</li>
</ul>
<h2>Woher kommen die Zahlen?</h2>
<p>Die Preise stammen vom Day-Ahead-Markt der Strombörse für Deutschland und Luxemburg. Veröffentlicht werden sie von der Bundesnetzagentur (SMARD.de) und dem Fraunhofer ISE (Energy-Charts). Seit 2025 gibt es diese Preise im Viertelstundentakt. Wir bilden daraus Stundenmittelwerte.</p>
"""},
    {"slug": "guenstiger-strom-gruener-strom", "title": "Günstiger Strom ist oft grüner Strom",
     "desc": "Warum sich Verschieben auch mit Festpreis für das Klima lohnen kann.",
     "body": """
<p>Auch wer einen festen Stromtarif hat und durch die Uhrzeit nichts spart, kann mit der Strom-Uhr etwas Gutes tun.</p>
<h2>Der Zusammenhang</h2>
<p>Wind- und Solaranlagen haben kaum laufende Kosten. Wenn sie viel Strom liefern, drücken sie den Börsenpreis. Teure Stunden entstehen dagegen oft dann, wenn Gas- und Kohlekraftwerke einspringen müssen, zum Beispiel abends bei wenig Wind. Grüne Stunden auf der Strom-Uhr fallen deshalb häufig mit viel Wind- und Sonnenstrom zusammen.</p>
<p>Das ist eine Faustregel, keine Garantie. Der Preis hängt auch von der Nachfrage, vom Handel mit Nachbarländern und von Kraftwerksausfällen ab.</p>
<h2>Was bringt es?</h2>
<ul>
<li>Wer große Verbraucher in günstige Stunden legt, nutzt im Schnitt einen höheren Anteil an Ökostrom.</li>
<li>Es entlastet das Stromnetz in den Abendstunden, in denen viele gleichzeitig Strom brauchen.</li>
<li>Mit einem dynamischen Tarif sparen Sie zusätzlich Geld.</li>
</ul>
<h2>Wo sehe ich den Ökostrom-Anteil genau?</h2>
<p>Den aktuellen Anteil erneuerbarer Energien zeigt das Fraunhofer ISE auf Energy-Charts und in seiner Stromampel-App. Die Strom-Uhr konzentriert sich auf den Preis und darauf, wann sich welches Gerät lohnt.</p>
<h2 class="quellen">Quellen</h2>
<ul class="quellen"><li><a href="https://www.ise.fraunhofer.de/de/presse-und-medien/presseinformationen/2023/ampel-app-zeigt-an-so-gruen-ist-europas-strommix.html" rel="noopener">Fraunhofer ISE: Stromampel-App</a></li></ul>
"""},
    {"slug": "paragraf-14a-netzentgelte", "title": "§ 14a einfach erklärt: günstigere Netzentgelte für Wärmepumpe und Wallbox",
     "desc": "Was Modul 1, 2 und 3 bedeuten und für wen sie gelten.",
     "body": """
<p>Wer eine Wärmepumpe, eine Wallbox fürs E-Auto oder einen Batteriespeicher hat, kann bei den Netzentgelten sparen. Im Gegenzug darf der Netzbetreiber diese Geräte im Notfall kurz drosseln. Geregelt ist das in § 14a des Energiewirtschaftsgesetzes.</p>
<h2>Für welche Geräte gilt das?</h2>
<p>Laut Bundesnetzagentur gilt die Regel für „steuerbare Verbrauchseinrichtungen“ mit mehr als 4,2 Kilowatt Anschlussleistung, die seit dem 1. Januar 2024 in Betrieb gegangen sind. Dazu gehören:</p>
<ul>
<li>private Wallboxen</li>
<li>Wärmepumpen, auch Heizstäbe als Zusatzheizung, und Klimaanlagen</li>
<li>Batteriespeicher, soweit sie Strom aus dem Netz beziehen</li>
</ul>
<p>Ältere Geräte ohne bisherige Steuerungsvereinbarung bleiben dauerhaft ausgenommen. Sie können aber freiwillig wechseln.</p>
<h2>Was darf der Netzbetreiber?</h2>
<p>Nur wenn eine konkrete Überlastung des Netzes droht, darf er den Strombezug des Geräts verringern, bei üblichen Haushaltsgeräten bis auf 4,2 Kilowatt. Ganz abschalten darf er nicht. Ein E-Auto lädt dann langsamer, eine Wärmepumpe läuft weiter.</p>
<h2>Die drei Module</h2>
<div class="scroll"><table class="tabelle">
<tr><th>Modul</th><th>Was Sie bekommen</th><th>Gut zu wissen</th></tr>
<tr><td><strong>Modul 1</strong></td><td>einen festen Rabatt pro Jahr auf das Netzentgelt</td><td>Gilt unabhängig davon, wann Sie Strom verbrauchen. Die Höhe steht im Preisblatt Ihres Netzbetreibers.</td></tr>
<tr><td><strong>Modul 2</strong></td><td>ein um 60 Prozent niedrigeres Netzentgelt pro kWh</td><td>Meist mit eigenem Zähler für das Gerät. Nicht mit Modul 1 oder 3 kombinierbar.</td></tr>
<tr><td><strong>Modul 3</strong></td><td>Netzentgelte, die sich nach der Tageszeit richten</td><td>Nur zusätzlich zu Modul 1. Seit 1. April 2025 von allen Netzbetreibern anzubieten. Braucht einen Smart Meter.</td></tr>
</table></div>
<h2>Modul 3 und die Strom-Uhr</h2>
<p>Bei Modul 3 gibt es drei Preisstufen beim Netzentgelt: teurer in Hochlastzeiten, günstiger in Niedriglastzeiten, dazwischen Standard. Hochlast liegt typischerweise am frühen Abend, Niedriglast eher nachts. Die genauen Zeiten legt jeder Netzbetreiber selbst fest.</p>
<p>Das ist ein zweites Zeitsignal neben dem Börsenpreis. Beide zeigen oft in die gleiche Richtung: Abends ist meist beides teuer. Wer einen dynamischen Tarif und Modul 3 kombiniert, sollte beide Zeiten im Blick haben. Die Strom-Uhr zeigt nur den Börsenpreis. Die Zeitfenster Ihres Netzbetreibers finden Sie in dessen Preisblatt.</p>
<h2>Was ist für mich das Richtige?</h2>
<p>Das hängt von Ihrem Verbrauch und Ihrem Netzbetreiber ab. Für eine erste Einschätzung zum dynamischen Tarif hilft der <a href="{{ root }}wechsel-check.html">Wechsel-Check</a>. Bei der Wahl des Moduls beraten Netzbetreiber, Installateur oder Verbraucherzentrale.</p>
<h2 class="quellen">Quellen</h2>
<ul class="quellen"><li><a href="https://www.bundesnetzagentur.de/DE/Vportal/Energie/SteuerbareVBE/BetroffeneAnlagen_table.html" rel="noopener">Bundesnetzagentur: Betroffene Anlagen und Dimm-Regelungen</a></li>
<li><a href="https://elektronik-zeit.de/14a-enwg/netzentgelte-modul-1-2-3/" rel="noopener">§ 14a Netzentgelte 2026: Modul 1, 2 und 3 im Vergleich</a></li>
<li><a href="https://www.zfk.de/energie/strom/14a-enwg-bundesnetzagentur-zeitvariable-netzentgelte" rel="noopener">ZfK: Zeitvariable Netzentgelte nach § 14a</a></li></ul>
"""},
    {"slug": "negative-strompreise", "title": "Negative Strompreise einfach erklärt",
     "desc": "Was es bedeutet, wenn Strom an der Börse „weniger als nichts“ kostet.",
     "body": """
<p>Manchmal ist der Börsenpreis für Strom kleiner als null. Das klingt seltsam, hat aber einen einfachen Grund.</p>
<h2>Warum gibt es das?</h2>
<p>Strom lässt sich nur begrenzt speichern. Wenn viel Sonne scheint und viel Wind weht, aber wenig Strom gebraucht wird, gibt es mehr Strom als Abnehmer. Kraftwerke zahlen dann sogar etwas dafür, dass jemand den Strom nimmt. Das kommt vor allem an sonnigen, windigen Wochenenden und Feiertagen vor.</p>
<h2>Bekomme ich dann Geld?</h2>
<p>Meist nicht. Zum Börsenpreis kommen bei Haushalten Netzentgelte, Steuern und Umlagen hinzu. Ein dynamischer Tarif wird in solchen Stunden deshalb sehr günstig, aber in der Regel nicht negativ. Genaues steht in Ihrem Vertrag.</p>
<h2>Wie oft kommt das vor?</h2>
<p>In den letzten {{ stats.n_days }} Tagen gab es {{ stats.negative_hours }} Stunde(n) mit negativem Börsenpreis. Die Strom-Uhr zeigt diese Stunden immer grün. Im <a href="{{ root }}statistik.html">Strom-Kalender</a> sehen Sie, wann günstige Zeiten meist liegen.</p>
"""},
    {"slug": "lohnt-sich-dynamischer-tarif", "title": "Lohnt sich ein dynamischer Tarif für mich?",
     "desc": "Eine ehrliche Entscheidungshilfe ohne Verkaufsdruck.",
     "body": """
<p>Ein dynamischer Tarif ist nicht für jeden besser. Ob er sich lohnt, hängt davon ab, wie viel Strom Sie verschieben können. Am schnellsten finden Sie es mit dem <a href="{{ root }}wechsel-check.html">Wechsel-Check</a> heraus.</p>
<h2>Eher geeignet, wenn …</h2>
<ul>
<li>Sie ein E-Auto zu Hause laden, das nicht sofort voll sein muss.</li>
<li>Sie eine Wärmepumpe, einen Warmwasserspeicher mit Heizstab oder einen Batteriespeicher haben.</li>
<li>Sie große Verbraucher gern per Zeitvorwahl, Zeitschaltuhr oder Smart Home starten.</li>
<li>Sie mit schwankenden Monatskosten gut zurechtkommen.</li>
</ul>
<h2>Eher nicht geeignet, wenn …</h2>
<ul>
<li>Sie Strom vor allem morgens und abends brauchen und kaum verschieben können.</li>
<li>Ihnen feste, planbare Kosten wichtiger sind als eine mögliche Ersparnis.</li>
<li>Bei Ihnen kein Smart Meter eingebaut werden kann.</li>
</ul>
<h2>Was ist, wenn Strom insgesamt teuer ist?</h2>
<p>Dann lohnt sich ein dynamischer Tarif nicht automatisch weniger. Entscheidend ist der Abstand zwischen günstigen und teuren Stunden, nicht das allgemeine Preisniveau. Wie groß dieser Abstand gerade ist, zeigt die Strom-Uhr jeden Tag unter „Lohnt sich Warten heute?“, und der <a href="{{ root }}monat/index.html">Monatsrückblick</a> rechnet nach, was Verschieben zuletzt gebracht hätte.</p>
<h2>So prüfen Sie es</h2>
<ol>
<li>Machen Sie den <a href="{{ root }}wechsel-check.html">Wechsel-Check</a>.</li>
<li>Rechnen Sie im <a href="{{ root }}tarifvergleich.html">Tarifvergleich</a> mit den Preisen konkreter Angebote nach.</li>
<li>Vergleichen Sie auch Grundpreis und Kosten der Messstelle.</li>
<li>Fragen Sie bei Unsicherheit Ihre Verbraucherzentrale.</li>
</ol>
"""},
]


# ----------------------------------------------------------------------------
# 9. RENDER / PUBLISH / MAIN
# ----------------------------------------------------------------------------
def ensure_fonts() -> None:
    """Lädt die Schriftdateien einmalig nach data/fonts/ und kopiert sie nach docs/assets/fonts/."""
    store = DATA_DIR / "fonts"
    store.mkdir(parents=True, exist_ok=True)
    for name, urls in FONT_FILES.items():
        target = store / name
        if not (target.exists() and target.stat().st_size > 1000):
            for url in urls:
                try:
                    r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
                    if r.ok and r.content[:4] == b"wOF2":
                        target.write_bytes(r.content)
                        log.info("Schrift %s geladen", name)
                        break
                except requests.RequestException as exc:
                    log.warning("Schrift %s nicht ladbar (%s)", name, exc)
            else:
                log.warning("Schrift %s nicht verfügbar – Seite nutzt Ersatzschrift", name)
                continue
        out = OUT_DIR / "assets" / "fonts" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, out)


def affiliate_list() -> list[dict]:
    return [a for a in AFFILIATE.values() if a["url"]]


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_data_json(analysed: list[dict], thr: dict, stats: dict, today: date, built: datetime) -> Markup:
    days = {}
    for a in analysed:
        if a["date"] >= today - timedelta(days=1):
            days[a["iso"]] = [{"t": h["t"], "c": round(h["ct"], 3), "l": h["lvl"]} for h in a["hours"]]
    data = {
        "built": built.isoformat(timespec="minutes"),
        "mwst": MWST, "aufschlag": DEFAULT_AUFSCHLAG,
        "thr": {"low": round(thr["low"], 3), "high": round(thr["high"], 3)},
        "days": days,
        "stats": {"avg": round(stats["avg"], 3), "best4": round(stats["best4"], 3), "n": stats["n_days"]},
        "affiliate": {"url": AFFILIATE["dyn_tarif"]["url"], "label": AFFILIATE["dyn_tarif"]["label"]}
                     if AFFILIATE["dyn_tarif"]["url"] else None,
        "devices": [{"slug": d["slug"], "name": d["name"], "kwh": d["kwh"], "hours": d["hours"],
                     "perWeek": d["per_week"]} for d in DEVICES],
    }
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return Markup(raw.replace("</", "<\\/"))


def render_site(analysed: list[dict], thr: dict, stats: dict, built: datetime) -> int:
    today = built.date()
    days = sorted(analysed, key=lambda a: a["date"])
    by_date = {a["date"]: a for a in days}
    today_a = by_date.get(today)
    tomorrow_a = by_date.get(today + timedelta(days=1))
    shown = [a for a in (today_a, tomorrow_a) if a] or [days[-1]]
    has_tomorrow = tomorrow_a is not None
    ref_day = shown[0]

    common = dict(
        site_name=SITE_NAME, site_url=SITE_URL, source=FOOTER_SOURCE, affiliate=affiliate_list(),
        built_text=f"Stand: {built:%d.%m.%Y, %H:%M} Uhr.", data_json=None, show_affiliate=False,
        nav="", canon="", title="", description="", root="", thr=thr, stats=stats,
        warten_stark=WARTEN_STARK, warten_etwas=WARTEN_ETWAS,
    )
    pages: list[str] = []

    def emit(rel: str, template: str, **ctx) -> None:
        html = ENV.get_template(template).render(**{**common, **ctx})
        write(OUT_DIR / rel, html)
        pages.append(rel)

    def day_title(a: dict) -> str:
        rel = {today: "Heute", today + timedelta(days=1): "Morgen"}.get(a["date"])
        long = f'{WEEKDAYS[a["date"].weekday()]}, {a["date"].day}. {MONTHS[a["date"].month - 1]}'
        return f"{rel}, {long}" if rel else long

    ref_label = "heute" if ref_day["date"] == today else f'am {ref_day["short"]}'
    data_json = build_data_json(days, thr, stats, today, built)

    # Startseite
    emit("index.html", "index.html", shown=[(a, day_title(a)) for a in shown], has_tomorrow=has_tomorrow,
         has_today=today_a is not None, today_dial=ref_day["dial_live"],
         ref_day=ref_day, tomorrow_day=tomorrow_a,
         ref_warten_titel="heute" if ref_day["date"] == today else f'am {ref_day["short"]}',

         ref_devices=ref_day["devices"], ref_label=ref_label,
         data_json=data_json, nav="heute", canon="index.html",
         title="Strompreis heute: Wann ist Strom günstig?",
         description="Die Strom-Uhr zeigt, ob Strom jetzt günstig, normal oder teuer ist, und wann Waschmaschine, "
                     "Geschirrspüler und E-Auto am besten laufen.")

    # Tagesseiten
    for i, a in enumerate(days):
        emit(f"tag/{a['iso']}.html", "day.html", d=a, root="../", nav="heute", canon=f"tag/{a['iso']}.html",
             prev_day=days[i - 1] if i > 0 else None, next_day=days[i + 1] if i + 1 < len(days) else None,
             title=f"Strompreis {a['pretty']}: günstigste Stunden",
             description=f"Börsenstrompreis am {a['pretty']}: Durchschnitt {de1(a['avg'])} ct/kWh, "
                         f"günstigste Stunde {a['cheap']['hour']} Uhr.")

    # Geräte
    emit("geraete/index.html", "devices_index.html", ref_devices=ref_day["devices"], ref_label=ref_label,
         root="../", nav="geraete",
         canon="geraete/index.html", title="Wann soll ich Waschmaschine, Auto und Co. einschalten?",
         description="Für jedes Gerät die günstigste Uhrzeit heute und morgen, einfach erklärt.")
    for dev in DEVICES:
        entries = []
        for a in shown:
            dv = next((x for x in a["devices"] if x["slug"] == dev["slug"]), None)
            if dv:
                entries.append({"day": a, "dev": dv, "titel": day_title(a)})
        if not entries:
            continue
        emit(f"geraete/{dev['slug']}.html", "device.html", dev=dev, entries=entries, ref=entries[0]["dev"],
             has_tomorrow=has_tomorrow, root="../", nav="geraete", canon=f"geraete/{dev['slug']}.html",
             show_affiliate=True,
             title=f"Wann soll ich {dev['frage']}? Günstigste Uhrzeit",
             description=f"Die günstigste Uhrzeit für {dev['name']} heute und morgen, mit Ersparnis in Euro.")

    # Rechner, Tarife, Statistik, Archiv
    emit("rechner.html", "rechner.html", data_json=data_json, nav="rechner", canon="rechner.html",
         show_affiliate=True, title="Stromkosten-Rechner: Was kostet mein Gerät wann?",
         description="Rechnen Sie nach, was Waschmaschine, Geschirrspüler oder E-Auto zu welcher Uhrzeit kosten.")
    emit("tarifvergleich.html", "tarife.html", data_json=data_json, nav="tarife", canon="tarifvergleich.html",
         show_affiliate=True, title="Fester oder dynamischer Stromtarif? Vergleich und Rechner",
         description="Fester gegen dynamischen Stromtarif: Unterschiede einfach erklärt, mit Rechner für Ihren Haushalt.")
    emit("wechsel-check.html", "check.html", data_json=data_json, nav="tarife", canon="wechsel-check.html",
         title="Wechsel-Check: Lohnt sich ein dynamischer Stromtarif für mich?",
         description="Sechs kurze Fragen, eine ehrliche Antwort: Lohnt sich ein dynamischer Stromtarif "
                     "für Ihren Haushalt? Mit Schätzung in Euro pro Jahr.")
    months = compute_months(days, today)
    emit("monat/index.html", "months_index.html", months=months, root="../", nav="statistik",
         canon="monat/index.html", title="Monatsrückblicke: Was hätte Verschieben gebracht?",
         description="Jeden Monat nachgerechnet: Wie viel billiger waren die günstigen Stunden, "
                     "und was hätte ein Haushalt mit E-Auto gespart?")
    for m in months:
        emit(f"monat/{m['key']}.html", "month.html", m=m, root="../", nav="statistik", canon=f"monat/{m['key']}.html",
             show_affiliate=True,
             title=f"Strompreis im {m['name']}: Rückblick und Spar-Bilanz",
             description=f"Strompreis im {m['name']}: Durchschnitt {de1(m['avg'])} ct/kWh, günstige Stunden "
                         f"{de1(m['spread'])} ct billiger. Was Verschieben gebracht hätte.")
    emit("statistik.html", "stats.html", calendar=calendar_html(list(reversed(days))[:STATS_DAYS]),
         nav="statistik", canon="statistik.html", title="Strom-Statistik: Beste Uhrzeit und bester Wochentag",
         description="Wann ist Strom meistens günstig? Strom-Kalender, beste Uhrzeit und Wochentags-Statistik.")
    emit("archiv.html", "archive.html", days=list(reversed(days))[:ARCHIVE_MAX], nav="heute",
         canon="archiv.html", title="Archiv: Strompreise der letzten Tage",
         description="Alle ausgewerteten Tage mit Durchschnittspreis und günstigsten Stunden.")

    # Wissen
    emit("wissen/index.html", "wissen_index.html", wissen=WISSEN, root="../", nav="wissen",
         canon="wissen/index.html", title="Strom einfach erklärt",
         description="Dynamischer Tarif, Börsenpreis, § 14a, Strom-Uhr und negative Strompreise in einfacher Sprache.")
    for w in WISSEN:
        body = Markup(ENV.from_string(w["body"]).render(root="../", thr=thr, stats=stats,
                                                       warten_stark=WARTEN_STARK, warten_etwas=WARTEN_ETWAS))
        emit(f"wissen/{w['slug']}.html", "wissen_page.html", w=w, body=body, root="../", nav="wissen",
             canon=f"wissen/{w['slug']}.html", show_affiliate=w["slug"] in ("dynamischer-stromtarif", "lohnt-sich-dynamischer-tarif"),
             title=w["title"], description=w["desc"])

    # Rechtstexte: werden aus legal/*.html übernommen (HTML-Fragmente)
    for name, title in (("impressum", "Impressum"), ("datenschutz", "Datenschutzerklärung")):
        src = LEGAL_DIR / f"{name}.html"
        content = Markup(src.read_text(encoding="utf-8")) if src.exists() else Markup("<p>TODO: Text einfügen</p>")
        if not src.exists():
            log.warning("legal/%s.html fehlt – Platzhaltertext wird verwendet!", name)
        emit(f"{name}.html", "legal.html", title=title, content=content, canon=f"{name}.html",
             description=f"{title} von {SITE_NAME}")
    pages_for_sitemap = list(pages)

    # 404-Seite (absolute Links, weil sie unter jedem Pfad ausgeliefert wird)
    emit("404.html", "notfound.html", root=f"{SITE_URL}/", title="Seite nicht gefunden", canon="404.html", description="Seite nicht gefunden")

    # Assets, robots, sitemap
    try:
        ensure_fonts()
    except Exception as exc:  # noqa: BLE001 – Schrift ist optional
        log.warning("Schriften konnten nicht vorbereitet werden: %s", exc)
    write(OUT_DIR / "assets" / "style.css", CSS.strip() + "\n")
    write(OUT_DIR / "assets" / "app.js", JS.strip() + "\n")
    now_s = today.isoformat()
    urls = "".join(f"<url><loc>{SITE_URL}/{'' if p == 'index.html' else p}</loc><lastmod>{now_s}</lastmod></url>"
                   for p in pages_for_sitemap)
    write(OUT_DIR / "sitemap.xml",
          f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>')
    write(OUT_DIR / "robots.txt", f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
    (OUT_DIR / ".nojekyll").touch()
    # Alte Seiten aus früheren Versionen entfernen (sonst bleiben sie online und bei Google)
    keep = set(pages)
    removed = 0
    for f in OUT_DIR.rglob("*.html"):
        if f.relative_to(OUT_DIR).as_posix() not in keep:
            f.unlink()
            removed += 1
    if removed:
        log.info("%d veraltete Seite(n) entfernt", removed)
    log.info("Site gerendert: %d Tage, %d Seiten", len(days), len(pages))
    return len(pages)


def git(*args: str) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # Windows: kein aufblitzendes Konsolenfenster
    return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, timeout=120,
                          creationflags=flags)


def publish() -> None:
    if not shutil.which("git"):
        raise RuntimeError("git nicht gefunden")
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        log.info("Keine Änderungen – kein Push")
        return
    msg = f"Update {datetime.now(TZ):%Y-%m-%d %H:%M}"
    for step in (("commit", "-m", msg), ("pull", "--rebase", "--autostash"), ("push",)):
        res = git(*step)
        if res.returncode != 0:
            raise RuntimeError(f"git {step[0]} fehlgeschlagen: {res.stderr.strip()}")
    log.info("Veröffentlicht: %s", msg)


def run(push: bool) -> int:
    built = datetime.now(TZ)
    today = built.date()
    wanted = {today + timedelta(days=1), today} | {today - timedelta(days=i) for i in range(1, BACKFILL_DAYS + 1)}
    cached = {date.fromisoformat(p.stem) for p in DATA_DIR.glob("*.json")} if DATA_DIR.exists() else set()

    day_hours: dict[date, list[dict]] = {}
    for d in sorted(cached | wanted):
        if d not in cached and HARD_FAILS >= 2:
            continue                       # API scheint down zu sein: nur noch Cache verwenden
        pts = fetch_day(d)
        if pts:
            day_hours[d] = hourly(pts)
    if not day_hours:
        log.error("Keine Daten – Abbruch ohne Veröffentlichung (alter Stand bleibt online)")
        return 2

    thr = compute_thresholds(day_hours, today)
    analysed = [analyse_day(d, hrs, thr) for d, hrs in sorted(day_hours.items())]
    stats = compute_stats(analysed, thr, today)
    for a in analysed:
        a["niveau"] = niveau_of(a["avg"], stats["avg"], stats["n_days"])
    render_site(analysed, thr, stats, built)
    if push:
        publish()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="nur lokal erzeugen")
    args = ap.parse_args()
    setup_logging()
    if not acquire_lock():
        log.warning("Anderer Lauf aktiv – beende")
        return 0
    try:
        return run(push=not args.no_push)
    except Exception:  # noqa: BLE001 – letzte Verteidigungslinie, alles ins Log
        log.exception("Unerwarteter Fehler")
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
