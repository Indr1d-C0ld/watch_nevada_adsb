#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch_nevada_secret.py — Monitor aeree con poligoni precisi (point-in-polygon).
Aggiunge:
 - supporto a poligoni (GeoJSON o JSON semplice)
 - algoritmo ray-casting (no dipendenze)
 - gestione rate-limit globale tramite lockfile
 - parsing sicuro dei tipi numerici (altitudine, velocità, coordinate)
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
    ts: Optional[int]

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
    for attempt in range(1, HTTP_RETRIES + 2):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            j = r.json()
            return j.get("aircraft", []) or []
        except Exception as e:
            last_exc = e
            if attempt <= HTTP_RETRIES:
                time.sleep(HTTP_BACKOFF * attempt)
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
        ts=ac.get("seen_pos_timestamp") or ac.get("seen_timestamp") or None,
    )

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
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
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
    fieldnames = ["first_seen_utc", "hex", "callsign", "lat", "lon", "alt_ft", "gs_kt", "note"]
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


def format_ac(ac: Aircraft) -> str:
    return (f"HEX:{ac.hex}  FLT:{ac.flight or '-'}  "
            f"ALT:{(ac.alt_baro or 'NA')} ft  GS:{(ac.gs or 'NA')} kt  "
            f"POS:{ac.lat if ac.lat is not None else 'NA'},{ac.lon if ac.lon is not None else 'NA'}")


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
        raw = []
        seen = set()
        for (lat, lon, rng) in TILES:
            acs = fetch_tile(lat, lon, rng)
            for ac in acs:
                hx = (ac.get("hex") or "").lower()
                if hx and hx not in seen:
                    seen.add(hx)
                    raw.append(ac)

        aircraft = [to_aircraft(ac) for ac in raw]
        aircraft = [ac for ac in aircraft if in_any_polygon(ac.lat, ac.lon, polygons)]

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
                print("  " + format_ac(ac) + anomalies_str)

            if hx not in seen_csv:
                row = {
                    "first_seen_utc": now_str,
                    "hex": ac.hex,
                    "callsign": ac.flight or "",
                    "lat": ac.lat if ac.lat is not None else "",
                    "lon": ac.lon if ac.lon is not None else "",
                    "alt_ft": ac.alt_baro if ac.alt_baro is not None else "",
                    "gs_kt": f"{ac.gs:.0f}" if ac.gs is not None else "",
                    "note": "; ".join(anomalies) if anomalies else "",
                }
                new_rows.append(row)
                print("  [NEW] " + format_ac(ac) + anomalies_str)
                if args.notify_telegram:
                    msg = f"NUOVO CONTATTO\n{format_ac(ac)}"
                    if anomalies:
                        msg += "\nAnomalie: " + "; ".join(anomalies)
                    send_telegram(msg)

            seen_runtime[hx] = ac

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
