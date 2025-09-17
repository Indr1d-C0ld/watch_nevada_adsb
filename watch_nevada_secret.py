#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch_nevada_secret.py — Monitor aerei con poligoni precisi (point-in-polygon).
Aggiunge:
 - supporto a poligoni (GeoJSON o JSON semplice)
 - algoritmo ray-casting (no dipendenze)
 - gestione rate-limit globale tramite lockfile
 - parsing sicuro dei tipi numerici (altitudine, velocità, coordinate)
 - notifiche Telegram con link diretti (HEX / FLT / REG / POS)
"""

import argparse
import csv
import datetime as dt
import fnmatch
import json
import os
import sys
import time
import fcntl   # 🔹 gestione lockfile per rate limit
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import requests

# ---------------------------
# Tiles / API config
# ---------------------------
TILES = [
    (37.246, -115.800, 40),
    (37.790, -116.780, 45),
    (37.100, -115.850, 45),
    (37.600, -115.200, 45),
    (36.800, -115.200, 40),
    (36.300, -115.030, 30),
]

API_TEMPLATE = "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{rng}"
API_MIL = "https://opendata.adsb.fi/api/v2/mil"

# thresholds
MAX_GS_KT = 650
MIN_GS_KT = 35
MIN_ALT_FT = 500
MAX_ALT_FT = 60000
MAX_VS_FPM = 8000
MAX_DGS_KTS = 250

HTTP_TIMEOUT = 15
HTTP_RETRIES = 2
HTTP_BACKOFF = 2.0

# ---------------------------
# Dataclasses
# ---------------------------
@dataclass
class Aircraft:
    hex: str
    flight: str
    lat: Optional[float]
    lon: Optional[float]
    alt_baro: Optional[int]
    gs: Optional[float]
    ts: Optional[float]
    reg: Optional[str] = None  # nuova proprietà REG
    model_t: Optional[str] = None      # 🔹 sigla modello (abbreviata)
    model_desc: Optional[str] = None   # 🔹 descrizione estesa
    is_mil: bool = False   # 🔹 nuovo campo

# ---------------------------
# Rate limiting con lockfile
# ---------------------------
def api_rate_guard():
    """
    Garantisce che solo una richiesta API al secondo venga fatta
    da tutte le istanze in esecuzione.
    Usa un lockfile in /tmp per coordinare i processi.
    """
    lockfile = "/tmp/adsbfi_api.lock"
    with open(lockfile, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)   # lock esclusivo
        f.seek(0)
        try:
            last = float(f.read().strip())
        except Exception:
            last = 0.0

        now = time.time()
        delta = now - last
        if delta < 1.05:  # se <1s dall’ultima chiamata, aspetta
            time.sleep(1.05 - delta)

        # aggiorna il timestamp
        f.seek(0)
        f.truncate()
        f.write(str(time.time()))
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)

# ---------------------------
# Point-in-polygon (ray-casting)
# ---------------------------
def point_in_ring(point: Tuple[float, float], ring: List[Tuple[float, float]]) -> bool:
    x, y = point[1], point[0]  # (lon, lat) -> (x, y)
    inside = False
    n = len(ring)
    for i in range(n):
        yi, xi = ring[i][0], ring[i][1]
        yj, xj = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
    return inside

def point_in_polygon(point: Tuple[float, float], polygon: List[List[Tuple[float, float]]]) -> bool:
    if not polygon:
        return False
    exterior = polygon[0]
    if not point_in_ring(point, exterior):
        return False
    for hole in polygon[1:]:
        if point_in_ring(point, hole):
            return False
    return True

def in_any_polygon(lat: Optional[float], lon: Optional[float], polygons: Iterable[List[List[Tuple[float, float]]]]) -> bool:
    if lat is None or lon is None:
        return False
    pt = (lat, lon)
    for poly in polygons:
        if point_in_polygon(pt, poly):
            return True
    return False

# ---------------------------
# Utility: load polygons
# ---------------------------
def load_polygons_from_geojson(path: str) -> List[List[List[Tuple[float, float]]]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    polys = []
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        for feat in data.get("features", []):
            geom = feat.get("geometry", {})
            gtype = geom.get("type")
            coords = geom.get("coordinates", [])
            if gtype == "Polygon":
                rings = []
                for ring in coords:
                    rings.append([(float(pt[1]), float(pt[0])) for pt in ring])
                polys.append(rings)
            elif gtype == "MultiPolygon":
                for polycoords in coords:
                    rings = []
                    for ring in polycoords:
                        rings.append([(float(pt[1]), float(pt[0])) for pt in ring])
                    polys.append(rings)
    elif isinstance(data, dict) and "polygons" in data:
        for poly in data["polygons"]:
            rings = []
            for ring in poly:
                rings.append([(float(pt[0]), float(pt[1])) for pt in ring])
            polys.append(rings)
    else:
        raise ValueError("Formato GeoJSON/JSON non riconosciuto")
    return polys

def sample_approx_polygons() -> List[List[List[Tuple[float, float]]]]:
    boxes = [
        (37.05, 37.55, -116.15, -115.30),
        (37.55, 38.10, -117.20, -116.30),
        (36.80, 38.30, -116.60, -115.00),
        (36.50, 37.05, -116.40, -115.20),
    ]
    polys = []
    for (min_lat, max_lat, min_lon, max_lon) in boxes:
        ring = [
            (min_lat, min_lon),
            (min_lat, max_lon),
            (max_lat, max_lon),
            (max_lat, min_lon),
            (min_lat, min_lon)
        ]
        polys.append([ring])
    return polys

# ---------------------------
# Fetching / parsing aircraft
# ---------------------------
def fetch_tile(lat: float, lon: float, rng_nm: int) -> List[dict]:
    api_rate_guard()   # 🔹 qui la guardia globale
    url = API_TEMPLATE.format(lat=lat, lon=lon, rng=rng_nm)
    last_exc = None
    # 1 tentativo iniziale + HTTP_RETRIES retry
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            j = r.json()
            return j.get("aircraft", []) or []
        except Exception as e:
            last_exc = e
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_BACKOFF * (attempt + 1))
    print(f"[WARN] Fetch fallito {url} — {last_exc}", file=sys.stderr)
    return []

def fetch_all_tiles() -> List[dict]:
    seen = set()
    merged: List[dict] = []
    for (lat, lon, rng) in TILES:
        acs = fetch_tile(lat, lon, rng)
        for ac in acs:
            hx = (ac.get("hex") or "").lower()
            if hx and hx not in seen:
                seen.add(hx)
                merged.append(ac)
    return merged

def to_aircraft(ac: dict) -> Aircraft:
    def safe_int(val):
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def safe_float(val):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return Aircraft(
        hex=(ac.get("hex") or "").lower(),
        flight=(ac.get("flight") or "").strip(),
        lat=safe_float(ac.get("lat")),
        lon=safe_float(ac.get("lon")),
        alt_baro=safe_int(ac.get("alt_baro")),
        gs=safe_float(ac.get("gs")),
        ts=safe_float(ac.get("seen_pos_timestamp") or ac.get("seen_timestamp")),
        reg=(ac.get("r") or ac.get("reg") or "").strip() or None,
        model_t=(ac.get("t") or "").strip() or None,        # 🔹 sigla
        model_desc=(ac.get("desc") or "").strip() or None ,  # 🔹 esteso
        is_mil=bool(
            ac.get("force_mil") or
            ac.get("military") or
            ac.get("isMil") or
            ac.get("mil") or
            ("military" in str(ac.get("dbFlags") or "").lower())
        )
    )

# ---------------------------
# fetch military
# ---------------------------
def fetch_military() -> List[dict]:
    api_rate_guard()
    last_exc = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = requests.get(API_MIL, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            raw = r.json() or {}
            if isinstance(raw, dict) and "ac" in raw:
                data = raw["ac"]
            elif isinstance(raw, list):
                data = raw
            else:
                return []
            for ac in data:
                if isinstance(ac, dict):
                    ac["force_mil"] = True
            return data
        except Exception as e:
            last_exc = e
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_BACKOFF * (attempt + 1))
    print(f"[WARN] Fetch militare fallito {API_MIL} — {last_exc}", file=sys.stderr)
    return []

# ---------------------------
# hex filters, csv, telegram, anomalies
# ---------------------------
def load_hex_filters(path: Optional[str]) -> List[str]:
    if not path:
        return []
    if not os.path.isfile(path):
        print(f"[WARN] File filtri HEX non trovato: {path}", file=sys.stderr)
        return []
    pats: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            pats.append(s.lower())
    return pats

def match_hex(hex_code: str, patterns: List[str]) -> bool:
    hx = hex_code.lower()
    for pat in patterns:
        if fnmatch.fnmatch(hx, pat):
            return True
    return False

def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[INFO] Telegram non configurato (manca TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[WARN] Telegram HTTP {r.status_code}: {r.text}", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] Telegram errore: {e}", file=sys.stderr)

def load_seen_csv(csv_path: str) -> Dict[str, dict]:
    seen: Dict[str, dict] = {}
    if not csv_path or not os.path.isfile(csv_path):
        return seen
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for row in rd:
                hx = (row.get("hex") or "").lower()
                if hx:
                    seen[hx] = row
    except Exception as e:
        print(f"[WARN] Lettura CSV fallita: {e}", file=sys.stderr)
    return seen

def append_seen_csv(csv_path: str, rows: List[dict]) -> None:
    must_write_header = not os.path.isfile(csv_path)
    fieldnames = ["first_seen_utc", "hex", "callsign", "reg", "model_t", "lat", "lon", "alt_ft", "gs_kt", "note"]
    try:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=fieldnames)
            if must_write_header:
                wr.writeheader()
            for r in rows:
                wr.writerow(r)
    except Exception as e:
        print(f"[WARN] Scrittura CSV fallita: {e}", file=sys.stderr)

def now_utc_str() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# --------- Formattazioni output ---------
def _fmt_latlon(lat: Optional[float], lon: Optional[float]) -> str:
    if lat is None or lon is None:
        return "NA,NA"
    return f"{lat:.6f},{lon:.6f}"

def format_ac_console(ac: Aircraft) -> str:
    """Riga per stampa console (senza link), con # e spazi come richiesto."""
    parts = []
    parts.append(f"HEX: #{ac.hex}" if ac.hex else "HEX: -")
    parts.append(f"FLT: #{ac.flight}" if ac.flight else "FLT: -")
    if ac.reg:
        parts.append(f"REG: #{ac.reg}")
    parts.append(f"ALT: {ac.alt_baro if ac.alt_baro is not None else 'NA'} ft")
    if ac.gs is not None:
        parts.append(f"GS: {ac.gs:.1f} kt")
    else:
        parts.append("GS: NA kt")
    parts.append(f"POS: {_fmt_latlon(ac.lat, ac.lon)}")
    return "  ".join(parts)

def format_ac_telegram(ac: Aircraft) -> str:
    """Riga per Telegram (Markdown), con POS cliccabile su Google Maps."""
    parts = []
    parts.append(f"HEX: #{ac.hex}" if ac.hex else "HEX: -")
    parts.append(f"FLT: #{ac.flight}" if ac.flight else "FLT: -")
    if ac.reg:
        parts.append(f"REG: #{ac.reg}")
    if ac.model_desc:
        parts.append(f"MODEL: {ac.model_desc}")
    parts.append(f"ALT: {ac.alt_baro if ac.alt_baro is not None else 'NA'} ft")
    if ac.gs is not None:
        parts.append(f"GS: {ac.gs:.1f} kt")
    else:
        parts.append("GS: NA kt")

    if ac.lat is not None and ac.lon is not None:
        latlon = _fmt_latlon(ac.lat, ac.lon)
        map_url = f"https://maps.google.com/?q={ac.lat:.6f},{ac.lon:.6f}"
        parts.append(f"POS: [{latlon}]({map_url})")
    else:
        parts.append("POS: NA,NA")

    return "  ".join(parts)

# --------- Rilevazione anomalie ---------
def detect_anomalies(ac: Aircraft, prev: Optional[Aircraft], dt_sec: Optional[float]) -> List[str]:
    notes = []
    if ac.gs is not None:
        if ac.gs > MAX_GS_KT:
            notes.append(f"GS alta {ac.gs:.0f} kt")
        elif ac.gs < MIN_GS_KT:
            notes.append(f"GS bassa {ac.gs:.0f} kt")
    if ac.alt_baro is not None:
        if ac.alt_baro > MAX_ALT_FT:
            notes.append(f"ALT alta {ac.alt_baro} ft")
        elif ac.alt_baro < MIN_ALT_FT:
            notes.append(f"ALT bassa {ac.alt_baro} ft")
    if prev and dt_sec and dt_sec > 0:
        if ac.gs is not None and prev.gs is not None:
            dgs = abs(ac.gs - prev.gs)
            if dgs > MAX_DGS_KTS:
                notes.append(f"ΔGS anomalo +{dgs:.0f} kt")
        if ac.alt_baro is not None and prev.alt_baro is not None:
            dalt = ac.alt_baro - prev.alt_baro
            vs_fpm = (dalt / dt_sec) * 60.0
            if abs(vs_fpm) > MAX_VS_FPM:
                notes.append(f"VS anomala {vs_fpm:.0f} fpm")
    return notes

# ---------------------------
# Main loop
# ---------------------------
def main():
    ap = argparse.ArgumentParser(description="Monitor aereo (adsb.fi Open Data) con poligoni")
    ap.add_argument("--interval", type=int, default=60, help="Secondi tra i polling (default: 60)")
    ap.add_argument("--csv", type=str, default="contacts.csv", help="CSV per nuovi contatti")
    ap.add_argument("--notify-telegram", action="store_true", help="Abilita notifiche Telegram")
    ap.add_argument("--hex-filter-file", type=str, default=None, help="File con pattern HEX (wildcard *)")
    ap.add_argument("--hex-filter-mode", type=str, choices=["include", "exclude"], default="include")
    ap.add_argument("--polygons-file", type=str, default=None, help="GeoJSON/JSON file con poligoni (Polygon/MultiPolygon)")
    ap.add_argument("--print-all", action="store_true", help="Stampa tutti i contatti")
    args = ap.parse_args()

    # carica poligoni
    if args.polygons_file:
        try:
            polygons = load_polygons_from_geojson(args.polygons_file)
            print(f"[INFO] Poligoni caricati da {args.polygons_file}: {len(polygons)}")
        except Exception as e:
            print(f"[ERR] Caricamento poligoni fallito: {e}", file=sys.stderr)
            polygons = sample_approx_polygons()
            print("[WARN] Uso poligoni esempio (approx).")
    else:
        polygons = sample_approx_polygons()
        print("[INFO] Nessun --polygons-file fornito: uso poligoni esempio (approx).")

    hex_patterns = load_hex_filters(args.hex_filter_file)
    if hex_patterns:
        print(f"[INFO] Filtri HEX caricati ({args.hex_filter_mode}): {len(hex_patterns)} pattern")

    seen_csv = load_seen_csv(args.csv)
    seen_runtime: Dict[str, Aircraft] = {}
    last_poll_time = None

    print(f"Monitor aereo con poligoni — start {now_utc_str()}")
    while True:
        t0 = time.time()
        raw = fetch_all_tiles()
        raw += fetch_military()  # 🔹 aggiunta

        # Parse
        aircraft = [to_aircraft(ac) for ac in raw]

        # Poligoni
        aircraft = [ac for ac in aircraft if in_any_polygon(ac.lat, ac.lon, polygons)]

        # Filtri HEX opzionali
        if hex_patterns:
            if args.hex_filter_mode == "include":
                aircraft = [ac for ac in aircraft if match_hex(ac.hex, hex_patterns)]
            else:
                aircraft = [ac for ac in aircraft if not match_hex(ac.hex, hex_patterns)]

        by_hex: Dict[str, Aircraft] = {ac.hex: ac for ac in aircraft if ac.hex}
        now_str = now_utc_str()
        print(f"\n[{now_str}] Contatti nella zona (poligoni): {len(by_hex)}")

        new_rows = []
        for hx, ac in by_hex.items():
            prev_ac = seen_runtime.get(hx)
            dt_sec = None
            if last_poll_time is not None:
                dt_sec = time.time() - last_poll_time

            anomalies = detect_anomalies(ac, prev_ac, dt_sec)
            anomalies_str = (" | " + "; ".join(anomalies)) if anomalies else ""

            if args.print_all:
                print("  " + format_ac_console(ac) + anomalies_str)

            # Primo avvistamento (nuovo contatto)
            if hx not in seen_csv:
                row = {
                    "first_seen_utc": now_str,
                    "hex": ac.hex,
                    "callsign": ac.flight or "",
                    "reg": ac.reg or "",
                    "model_t": ac.model_t or "",
                    "lat": ac.lat if ac.lat is not None else "",
                    "lon": ac.lon if ac.lon is not None else "",
                    "alt_ft": ac.alt_baro if ac.alt_baro is not None else "",
                    "gs_kt": f"{ac.gs:.0f}" if ac.gs is not None else "",
                    "note": "; ".join(anomalies) if anomalies else "",
                }
                new_rows.append(row)

                # ----------- Messaggio Telegram -----------
                if args.notify_telegram:
                    base_line = format_ac_telegram(ac)
                    msg = f"NUOVO CONTATTO\n{base_line}"
                    if anomalies:
                        msg += "\nAnomalie: " + "; ".join(anomalies)

                    # Link dinamici + piattaforme
                    links = []
                    hex_code = ac.hex
                    flight_code = ac.flight or ""
                    reg_code = ac.reg or ""

                    if hex_code:
                        links.append(f"[ADSB.fi](https://globe.adsb.fi/): https://globe.adsb.fi/?icao={hex_code}")
                        links.append(f"[ADSB Exchange](https://globe.adsbexchange.com/): https://globe.adsbexchange.com/?icao={hex_code}")
                        links.append(f"[Planespotters](https://www.planespotters.net/): https://www.planespotters.net/hex/{hex_code}")

                    if flight_code:
                        links.append(f"[FlightAware](https://www.flightaware.com/it-IT/): https://flightaware.com/live/flight/{flight_code}")

                    if reg_code:
                        links.append(f"[AirHistory](https://www.airhistory.net/): https://www.airhistory.net/marks-all/{reg_code}")
                        links.append(f"[JetPhotos](https://www.jetphotos.com/): https://www.jetphotos.com/registration/{reg_code}")

                    if links:
                        msg += "\n\n" + "\n".join(links)

                    send_telegram(msg)

            # Aggiorna runtime
            seen_runtime[hx] = ac

            # 🔹 Blocco per i voli militari
            if ac.is_mil:
                row = {
                    "first_seen_utc": now_str,
                    "hex": ac.hex,
                    "callsign": ac.flight or "",
                    "reg": ac.reg or "",
                    "model_t": ac.model_t or "",
                    "lat": ac.lat if ac.lat is not None else "",
                    "lon": ac.lon if ac.lon is not None else "",
                    "alt_ft": ac.alt_baro if ac.alt_baro is not None else "",
                    "gs_kt": f"{ac.gs:.0f}" if ac.gs is not None else "",
                    "note": "mil"
                }
                new_rows.append(row)
                print("  [MIL] " + format_ac_console(ac))

                if args.notify_telegram:
                    msg = f"VOLO MILITARE\n{format_ac_telegram(ac)}\nFlag: military"
                    send_telegram(msg)

        # Scrivi eventuali nuovi contatti su CSV
        if new_rows:
            append_seen_csv(args.csv, new_rows)
            for r in new_rows:
                seen_csv[r["hex"]] = r

        last_poll_time = time.time()
        elapsed = time.time() - t0
        to_sleep = max(1, args.interval - int(elapsed))
        time.sleep(to_sleep)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Interrotto dall'utente.")
