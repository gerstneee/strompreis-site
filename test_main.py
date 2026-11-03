"""Kleine Selbstprüfung der wichtigsten Rechnungen in main.py.
Start: .venv\\Scripts\\python test_main.py   (meldet "Alle Prüfungen bestanden" oder bricht mit Fehler ab)"""
from datetime import date, datetime, timedelta

import main as m


def stunden(preise: list[float], start: date = date(2026, 11, 2)) -> list[dict]:
    """Baut Stundenwerte (ct/kWh) wie hourly() sie liefert, ab 0 Uhr deutscher Zeit."""
    t0 = int(datetime(start.year, start.month, start.day, tzinfo=m.TZ).timestamp())
    return m.hourly([[t0 + i * 3600, p * 10] for i, p in enumerate(preise)])


# Zeitumstellung: 25 Stunden im Oktober, 23 Stunden im März, 15-Minuten-Werte werden zu Stunden
for tag, anzahl in ((date(2026, 10, 25), 25), (date(2026, 3, 29), 23)):
    t0 = int(datetime(tag.year, tag.month, tag.day, tzinfo=m.TZ).timestamp())
    viertel = [[t0 + i * 900, 100.0] for i in range(anzahl * 4)]
    hrs = m.hourly(viertel)
    assert len(hrs) == anzahl, (tag, len(hrs))
    assert m.split_days({"unix_seconds": [t for t, _ in viertel], "price": [p for _, p in viertel]}).keys() == {tag}
    assert abs(hrs[0]["ct"] - 10.0) < 1e-9

# Ampel innerhalb des Tages: je ein Drittel grün, gelb, rot
hrs = stunden([float(i) for i in range(24)])
m.day_levels(hrs)
assert [h["lvl"] for h in hrs] == ["g"] * 8 + ["y"] * 8 + ["r"] * 8
# fast gleiche Preise: alles gelb, negative Preise immer grün
hrs = stunden([1.0] * 12 + [2.0] * 11 + [-0.5])
m.day_levels(hrs)
assert [h["lvl"] for h in hrs] == ["y"] * 23 + ["g"]

# günstigstes Zeitfenster, auch am Tagesende (23 Uhr -> 24 Uhr)
hrs = stunden([20.0] * 21 + [1.0, 1.0, 1.0])
w = m.best_window(hrs, 3)
assert (w["h_from"], w["h_to"], w["avg"]) == (21, 24, 1.0)
assert m.best_window(hrs, 3, worst=True)["h_from"] == 0

# Warten lohnt sich: Grenzen 4 und 10 ct inkl. MwSt.
assert m.warten_of(20.0, 20.0)["key"] == "kaum"
assert m.warten_of(20.0, 20.0 - 3.9 / m.MWST)["key"] == "kaum"
assert m.warten_of(20.0, 20.0 - 4.1 / m.MWST)["key"] == "etwas"
assert m.warten_of(20.0, 20.0 - 10.1 / m.MWST)["key"] == "stark"

# Preisniveau: jeder Tag wird mit den 30 Tagen davor verglichen, nicht mit den letzten 30 Tagen ab heute
start = date(2026, 1, 1)
analysed = [{"date": start + timedelta(days=i), "avg": 10.0 if i < 40 else 30.0} for i in range(80)]
m.add_niveau(analysed, {"avg": 30.0, "n_days": 30})
assert analysed[35]["niveau"]["ref"] == 10.0 and analysed[35]["niveau"]["n"] == 30
assert analysed[35]["niveau"]["word"] == "etwa so teuer wie"
assert analysed[45]["niveau"]["word"] == "teurer als"
assert analysed[0]["niveau"]["ref"] == 30.0          # erster Tag ohne Vorgeschichte: Schnitt der letzten Tage

print("Alle Prüfungen bestanden")
