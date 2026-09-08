#!/usr/bin/env python3
"""
server/app.py  —  TPSMC Flask Backend
──────────────────────────────────────
• Subscribes to MQTT broker
• Writes all data to InfluxDB
• Exposes REST API for dashboard
• Serves live HTML dashboard
"""

import json
import math
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request, Response
import paho.mqtt.client as mqtt
import os
import route_model

# ── Optional InfluxDB ────────────────────────────────────────────
try:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS
    INFLUX_AVAILABLE = True
except ImportError:
    INFLUX_AVAILABLE = False
    print("[WARN] influxdb-client not installed — data stored in memory only")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# ════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════
MQTT_BROKER   = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT     = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC    = os.getenv("MQTT_TOPIC", "traffic/edge")

INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN", "your-token-here")
INFLUX_ORG    = "traffic_org"
INFLUX_BUCKET = "traffic_data"
ENABLE_INFLUX = os.getenv("ENABLE_INFLUX", "0") == "1"

TPSMC_DEMO_MODE = os.getenv("TPSMC_DEMO_MODE", "false").lower() in ("true", "1", "yes")
STALE_SENSOR_SECONDS = int(os.getenv("STALE_SENSOR_SECONDS", "60"))

# ════════════════════════════════════════════════════════════════
# In-Memory State
# ════════════════════════════════════════════════════════════════
latest_data   = {}      # {location_id: payload}
alert_history = []      # Last 200 alerts
all_history   = []      # Last 500 all readings (for charts)
alert_state   = {}      # {location_id: {"alert": str, "last_logged": float}}
emergency_state = {
    "active": {},        # {location_id: incident}
    "history": []        # Last 100 emergency dispatch/cancel events
}
incident_timeline = []   # Last 200 incident lifecycle events
manual_overrides  = {}   # {location_id: {mode, duration_seconds, started_at, expires_at}}
active_corridors  = {}   # {location_id: corridor_info}  — active green corridors
lock          = threading.RLock()

# Global MQTT publish client (set in start_mqtt)
mqtt_publish_client = None
ALERT_LOG_REPEAT_SEC = int(os.getenv("ALERT_LOG_REPEAT_SEC", "30"))

# Named pilot junctions (always kept; MQTT can still use junction_A ...)
BASE_JUNCTION_COORDS = {
    "junction_A": {"lat": 12.9716, "lng": 77.5946, "name": "MG Road Junction"},
    "junction_B": {"lat": 12.9784, "lng": 77.6408, "name": "Indiranagar Cross"},
    "junction_C": {"lat": 12.9352, "lng": 77.6245, "name": "Koramangala 80ft"},
    "junction_D": {"lat": 12.9173, "lng": 77.6228, "name": "Silk Board Junction"},
    "junction_E": {"lat": 13.0292, "lng": 77.5859, "name": "Hebbal Junction"},
    "junction_F": {"lat": 13.0007, "lng": 77.6753, "name": "KR Puram Junction"},
    "junction_G": {"lat": 12.9562, "lng": 77.7019, "name": "Marathahalli Junction"},
}

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_signals_json_path():
    return os.getenv(
        "BENGALURU_SIGNALS_JSON",
        os.path.join(_THIS_DIR, "data", "bengaluru_traffic_signals.json"),
    )


def _merge_junction_coordinates():
    """Merge curated junctions + OpenStreetMap `highway=traffic_signals` dataset."""
    merged = dict(BASE_JUNCTION_COORDS)
    seen_keys = {(round(float(v["lat"]), 6), round(float(v["lng"]), 6)) for v in merged.values()}
    path = _default_signals_json_path()
    cap = max(100, min(20000, int(os.getenv("BENGALURU_SIGNALS_MAX", "8000"))))
    if not os.path.isfile(path):
        print(f"[Junctions] No OSM signals file ({path}); using {len(merged)} base junctions only.")
        return merged
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:
        print(f"[Junctions] Cannot read OSM signals {path}: {exc}")
        return merged

    signals = doc.get("signals") or []
    added = 0
    skipped_dup = 0
    for rec in signals:
        if added >= cap:
            break
        oid = rec.get("osm_id")
        lat_v, lng_v = rec.get("lat"), rec.get("lng")
        if oid is None or lat_v is None or lng_v is None:
            continue
        lat_f, lng_f = float(lat_v), float(lng_v)
        key = (round(lat_f, 6), round(lng_f, 6))
        if key in seen_keys:
            skipped_dup += 1
            continue
        loc_id = f"osm_{int(oid)}"
        nm = (rec.get("name") or loc_id).strip()
        merged[loc_id] = {"lat": lat_f, "lng": lng_f, "name": nm[:120]}
        seen_keys.add(key)
        added += 1
    print(
        f"[Junctions] OSM signals: +{added} from {path} (skipped {skipped_dup} duplicate coords); "
        f"total map nodes={len(merged)} cap={cap}"
    )
    return merged


JUNCTION_COORDS = _merge_junction_coordinates()

EMERGENCY_COUNTDOWN_SECONDS = int(os.getenv("EMERGENCY_COUNTDOWN_SECONDS", "20"))
EMERGENCY_INTERNET_ONLINE = os.getenv("EMERGENCY_INTERNET_ONLINE", "true").lower() != "false"

VICTIM_PROFILE = {
    "name": os.getenv("VICTIM_NAME", "Unknown rider"),
    "blood_type": os.getenv("VICTIM_BLOOD_TYPE", "O+"),
    "medical_notes": os.getenv("VICTIM_MEDICAL_NOTES", "No known allergies"),
}

EMERGENCY_CONTACTS = [
    {"name": "Emergency Contact 1", "phone": os.getenv("EMERGENCY_CONTACT_1", "+91-90000-00001")},
    {"name": "Emergency Contact 2", "phone": os.getenv("EMERGENCY_CONTACT_2", "+91-90000-00002")},
]

HOSPITALS = [
    {"name": "Manipal Hospital Old Airport Road", "phone": "+91-80-2502-4444", "type": "Trauma / multi-specialty", "lat": 12.9583, "lng": 77.6484, "capacity_status": "TRAUMA_READY", "available_beds": 12, "eta_minutes": 8},
    {"name": "Bowring and Lady Curzon Hospital", "phone": "+91-80-2559-1372", "type": "Government emergency", "lat": 12.9824, "lng": 77.6045, "capacity_status": "AVAILABLE", "available_beds": 6, "eta_minutes": 12},
    {"name": "St. John's Medical College Hospital", "phone": "+91-80-2206-5000", "type": "Emergency / trauma", "lat": 12.9293, "lng": 77.6181, "capacity_status": "TRAUMA_READY", "available_beds": 8, "eta_minutes": 15},
    {"name": "Fortis Hospital Bannerghatta Road", "phone": "+91-80-6621-4444", "type": "Multi-specialty", "lat": 12.8958, "lng": 77.5942, "capacity_status": "AVAILABLE", "available_beds": 15, "eta_minutes": 20},
    {"name": "Sakra World Hospital", "phone": "+91-80-4969-4969", "type": "Emergency / multi-specialty", "lat": 12.9328, "lng": 77.6782, "capacity_status": "AVAILABLE", "available_beds": 10, "eta_minutes": 18},
    {"name": "Narayana Health City", "phone": "+91-80-7122-2222", "type": "Cardiac / trauma support", "lat": 12.8072, "lng": 77.6958, "capacity_status": "BUSY", "available_beds": 2, "eta_minutes": 35},
    {"name": "Aster CMI Hospital", "phone": "+91-80-4342-0100", "type": "Emergency / multi-specialty", "lat": 13.0545, "lng": 77.5914, "capacity_status": "AVAILABLE", "available_beds": 9, "eta_minutes": 22},
    {"name": "Ramaiah Memorial Hospital", "phone": "+91-80-2360-8888", "type": "Emergency / teaching hospital", "lat": 13.0309, "lng": 77.5652, "capacity_status": "TRAUMA_READY", "available_beds": 5, "eta_minutes": 25},
    {"name": "KC General Hospital", "phone": "+91-80-2334-1771", "type": "Government emergency", "lat": 13.0037, "lng": 77.5697, "capacity_status": "AVAILABLE", "available_beds": 4, "eta_minutes": 28},
]

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GOOGLE_TRAFFIC_ENABLED = os.getenv("GOOGLE_TRAFFIC_ENABLED", "true").lower() in ("true", "1", "yes")
GOOGLE_TRAFFIC_CACHE_TTL = int(os.getenv("GOOGLE_TRAFFIC_CACHE_TTL", "90"))

# Rough live road-traffic probe around a junction (Distance Matrix legacy API).
_google_traffic_cache = {}
_google_traffic_lock = threading.Lock()
HOSPITAL_CENTER_LAT = float(os.getenv("HOSPITAL_CENTER_LAT", "12.9716"))
HOSPITAL_CENTER_LNG = float(os.getenv("HOSPITAL_CENTER_LNG", "77.5946"))
HOSPITAL_RADIUS_KM = int(os.getenv("HOSPITAL_RADIUS_KM", "100"))
HOSPITAL_CACHE_TTL_SECONDS = int(os.getenv("HOSPITAL_CACHE_TTL_SECONDS", "43200"))
HOSPITAL_SEARCH_GRID = int(os.getenv("HOSPITAL_SEARCH_GRID", "5"))
HOSPITAL_SEARCH_KEYWORDS = [
    item.strip() for item in os.getenv(
        "HOSPITAL_SEARCH_KEYWORDS",
        "hospital,emergency hospital,trauma center,multispeciality hospital,medical college hospital"
    ).split(",") if item.strip()
]
hospital_cache = {
    "items": HOSPITALS,
    "source": "seed",
    "fetched_at": 0,
    "radius_km": 0,
    "center": None,
    "query_count": 0,
}


def _distance_score(lat_a, lng_a, lat_b, lng_b):
    return abs(float(lat_a) - float(lat_b)) + abs(float(lng_a) - float(lng_b))


def _distance_km(lat_a, lng_a, lat_b, lng_b):
    lat_a, lng_a, lat_b, lng_b = map(math.radians, map(float, (lat_a, lng_a, lat_b, lng_b)))
    dlat = lat_b - lat_a
    dlng = lng_b - lng_a
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(dlng / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(hav), math.sqrt(1 - hav))


def nearest_hospital(lat, lng):
    hospitals = hospital_cache["items"] or HOSPITALS
    return min(hospitals, key=lambda h: _distance_km(lat, lng, h["lat"], h["lng"]))


def simulate_hospital_capacity(hospitals):
    """Add simulated capacity fields to hospitals that lack them.
    Uses deterministic hour-based assignment; random only in demo mode."""
    enriched = []
    hour = datetime.now().hour
    for idx, h in enumerate(hospitals):
        h2 = dict(h)
        if "capacity_status" not in h2:
            if TPSMC_DEMO_MODE:
                import random
                if hour in (2, 3, 4, 5):
                    h2["capacity_status"] = random.choice(["AVAILABLE", "AVAILABLE", "TRAUMA_READY"])
                elif hour in (8, 9, 10, 17, 18, 19, 20):
                    h2["capacity_status"] = random.choice(["AVAILABLE", "BUSY", "BUSY"])
                else:
                    h2["capacity_status"] = random.choice(["AVAILABLE", "AVAILABLE", "TRAUMA_READY", "BUSY"])
            else:
                # Deterministic: rotate based on index + hour
                statuses = ["AVAILABLE", "TRAUMA_READY", "AVAILABLE", "BUSY"]
                h2["capacity_status"] = statuses[(idx + hour) % len(statuses)]
        if "available_beds" not in h2:
            if TPSMC_DEMO_MODE:
                import random
                h2["available_beds"] = random.randint(1, 18) if h2["capacity_status"] != "BUSY" else random.randint(0, 2)
            else:
                h2["available_beds"] = max(0, 10 - (idx % 8)) if h2["capacity_status"] != "BUSY" else 1
        if "eta_minutes" not in h2:
            if TPSMC_DEMO_MODE:
                import random
                h2["eta_minutes"] = random.randint(5, 40)
            else:
                h2["eta_minutes"] = 8 + (idx * 3) % 30
        enriched.append(h2)
    return enriched


def best_hospital(lat, lng):
    """Choose best hospital: TRAUMA_READY > AVAILABLE > BUSY, then by ETA."""
    hospitals = hospital_cache["items"] or HOSPITALS
    enriched = simulate_hospital_capacity(hospitals)
    STATUS_PRIORITY = {"TRAUMA_READY": 0, "AVAILABLE": 1, "BUSY": 2}
    def score(h):
        status_rank = STATUS_PRIORITY.get(h.get("capacity_status", "AVAILABLE"), 1)
        dist = _distance_km(lat, lng, h["lat"], h["lng"])
        eta = float(h.get("eta_minutes", dist * 3))
        return (status_rank, eta, dist)
    ranked = sorted(enriched, key=score)
    non_busy = [h for h in ranked if h.get("capacity_status") != "BUSY"]
    return non_busy[0] if non_busy else ranked[0]


def log_timeline_event(location, event, details=""):
    """Append an event to the incident timeline."""
    entry = {
        "id": f"{location}-{event}-{int(time.time()*1000)}",
        "location": location,
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": details,
    }
    incident_timeline.append(entry)
    if len(incident_timeline) > 200:
        try: incident_timeline.pop(0)
        except IndexError: pass
    return entry


def calculate_alert_confidence(current):
    """Calculate confidence score and contributing factors for the current alert."""
    alert = current.get("alert", "CLEAR")
    vib = float(current.get("vib_raw", 0))
    mic_peak = float(current.get("mic_peak_raw", 0))
    mic_avg = float(current.get("mic_avg_raw", 0))
    noise = float(current.get("noise_db", 40))
    gas = float(current.get("gas_raw", 0))
    aqi = float(current.get("aqi", 0))
    crowd = float(current.get("crowd_percent", 0))
    phase = current.get("signal_phase", "RED")
    factors = []
    confidence = 0

    if alert == "ACCIDENT_CRITICAL":
        if vib > 0: confidence += 40; factors.append("vibration sensor triggered")
        else: confidence += 10
        if mic_peak > 3500: confidence += 25; factors.append(f"mic_peak={int(mic_peak)}")
        elif mic_peak > 2000: confidence += 15; factors.append(f"mic_peak moderate")
        else: confidence += 5
        if crowd > 40: confidence += 15; factors.append("crowd density elevated")
        else: confidence += 5
        if phase == "GREEN": confidence += 20; factors.append("collision during GREEN phase")
        else: confidence += 8
    elif alert == "EMERGENCY_SIREN":
        if mic_avg > 2500: confidence += 50; factors.append("high sustained mic level")
        elif mic_avg > 1500: confidence += 30; factors.append("moderate mic level")
        else: confidence += 15
        if noise > 80: confidence += 30; factors.append(f"noise={int(noise)}dB")
        elif noise > 65: confidence += 18
        else: confidence += 8
        confidence += 20; factors.append("siren alert code match")
    elif alert == "POLLUTION_ALERT":
        if aqi > 150: confidence += 45; factors.append(f"AQI={int(aqi)} critical")
        elif aqi > 100: confidence += 30; factors.append(f"AQI={int(aqi)} high")
        else: confidence += 15
        if gas > 2000: confidence += 35; factors.append("gas_raw elevated")
        elif gas > 1000: confidence += 20
        else: confidence += 10
        confidence += 20; factors.append("sustained pollution pattern")
    elif alert == "CROWD_WARNING":
        if crowd > 70: confidence += 55; factors.append(f"crowd={int(crowd)}%")
        elif crowd > 50: confidence += 35; factors.append(f"crowd={int(crowd)}%")
        else: confidence += 20
        if noise > 70: confidence += 25; factors.append("high ambient noise")
        else: confidence += 10
        confidence += 15
    elif alert == "CLEAR":
        confidence = 95
        factors.append("all sensors nominal")
    else:
        confidence = 15
        factors.append("sensor offline or unknown")

    return {
        "alert_confidence": min(100, max(0, confidence)),
        "confidence_factors": factors[:4],
    }


def _google_places_nearby(lat, lng, radius_m, keyword):
    places = []
    token = None
    for _ in range(3):
        params = {
            "key": GOOGLE_MAPS_API_KEY,
            "location": f"{lat},{lng}",
            "radius": min(radius_m, 50000),
            "type": "hospital",
            "keyword": keyword,
        }
        if token:
            params = {"key": GOOGLE_MAPS_API_KEY, "pagetoken": token}
            time.sleep(2)
        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=12) as res:
            data = json.loads(res.read().decode("utf-8"))
        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            break
        for item in data.get("results", []):
            loc = item.get("geometry", {}).get("location", {})
            if "lat" not in loc or "lng" not in loc:
                continue
            places.append({
                "place_id": item.get("place_id", item.get("name")),
                "name": item.get("name", "Hospital"),
                "type": "Google Places hospital",
                "phone": "Google Places",
                "lat": loc["lat"],
                "lng": loc["lng"],
                "rating": item.get("rating"),
                "address": item.get("vicinity", ""),
                "keyword": keyword,
            })
        token = data.get("next_page_token")
        if not token:
            break
    return places


def google_road_traffic_context(origin_lat, origin_lng):
    """Return congestion 0–100 from Google Driving distance matrix (traffic-aware).

    Note: Google's 'Popular times' place busyness is not available here; we use live
    **road** delay (duration_in_traffic vs free-flow duration) near the junction.
    """
    if origin_lat is None or origin_lng is None:
        return {"status": "no_coordinates", "score": None, "hint": "Need junction lat/lng for traffic probe"}
    if not GOOGLE_MAPS_API_KEY or not GOOGLE_TRAFFIC_ENABLED:
        return {"status": "disabled", "score": None, "hint": "Set GOOGLE_MAPS_API_KEY and GOOGLE_TRAFFIC_ENABLED"}

    ck = (round(origin_lat, 4), round(origin_lng, 4))
    now_ts = time.time()
    with _google_traffic_lock:
        cached = _google_traffic_cache.get(ck)
        if cached and now_ts - cached.get("cached_at", 0) < GOOGLE_TRAFFIC_CACHE_TTL:
            return {k: v for k, v in cached.items() if k != "cached_at"}

    lat_rad = math.radians(origin_lat)
    cos_lat = max(0.35, math.cos(lat_rad))
    d_deg = 0.006  # ~666 m latitude
    dests = [
        (origin_lat + d_deg, origin_lng),
        (origin_lat - d_deg, origin_lng),
        (origin_lat, origin_lng + d_deg / cos_lat),
        (origin_lat, origin_lng - d_deg / cos_lat),
    ]
    dest_param = "|".join(f"{a},{b}" for a, b in dests)

    params = {
        "key": GOOGLE_MAPS_API_KEY,
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": dest_param,
        "mode": "driving",
        "departure_time": "now",
        "traffic_model": "best_guess",
    }
    url = "https://maps.googleapis.com/maps/api/distancematrix/json?" + urllib.parse.urlencode(params)
    parsed = {}
    try:
        with urllib.request.urlopen(url, timeout=14) as res:
            parsed = json.loads(res.read().decode("utf-8"))
    except Exception as exc:
        err = {"status": "request_error", "score": None, "error": str(exc), "hint": "Distance Matrix request failed"}
        with _google_traffic_lock:
            _google_traffic_cache[ck] = {**err, "cached_at": now_ts}
        return dict(err)

    if parsed.get("status") != "OK":
        err = {"status": parsed.get("status", "error"), "score": None, "error_detail": parsed.get("error_message", "")}
        with _google_traffic_lock:
            _google_traffic_cache[ck] = {**err, "cached_at": now_ts}
        return dict(err)

    rows = parsed.get("rows") or []
    ratios = []
    if rows:
        for el in rows[0].get("elements") or []:
            if el.get("status") != "OK":
                continue
            base = float((el.get("duration") or {}).get("value", 0))
            dit = el.get("duration_in_traffic")
            if not dit:
                continue
            traffic_s = float(dit.get("value", base))
            if base <= 1:
                base = 1.0
            ratios.append(traffic_s / base)

    if not ratios:
        out = {
            "status": "no_traffic_data",
            "score": None,
            "hint": "No duration_in_traffic returned (billing or traffic not enabled for this key/route)",
            "samples": 0,
        }
        with _google_traffic_lock:
            _google_traffic_cache[ck] = {**out, "cached_at": now_ts}
        return dict(out)

    avg_ratio = sum(ratios) / len(ratios)
    # Typical ratio ~1.0 free flow, 1.3–2.0 crowded
    score = int(round(min(100, max(0, (avg_ratio - 1.0) * 85))))
    out = {
        "status": "ok",
        "score": score,
        "delay_ratio_avg": round(avg_ratio, 3),
        "samples": len(ratios),
        "sources": ["google_distance_matrix_duration_in_traffic"],
    }
    with _google_traffic_lock:
        _google_traffic_cache[ck] = {**out, "cached_at": now_ts}
    return dict(out)


def fetch_google_hospitals(center_lat, center_lng, radius_km):
    if not GOOGLE_MAPS_API_KEY:
        return HOSPITALS, "seed", 0

    radius_m = min(int(radius_km * 1000), 100000)
    grid = max(1, HOSPITAL_SEARCH_GRID if HOSPITAL_SEARCH_GRID % 2 == 1 else HOSPITAL_SEARCH_GRID + 1)
    step_km = (radius_km * 2) / max(grid - 1, 1)
    step_lat = step_km / 111.0
    lat_rad = math.radians(center_lat)
    step_lng = step_km / max(111.0 * math.cos(lat_rad), 1)
    half = grid // 2
    centers = []
    for row in range(-half, half + 1):
        for col in range(-half, half + 1):
            lat = center_lat + (row * step_lat)
            lng = center_lng + (col * step_lng)
            if _distance_km(center_lat, center_lng, lat, lng) <= radius_km:
                centers.append((lat, lng))

    deduped = {}
    query_count = 0
    for lat, lng in centers:
        cell_radius_m = min(50000, max(15000, int((step_km * 0.85) * 1000)))
        for keyword in HOSPITAL_SEARCH_KEYWORDS:
            query_count += 1
            try:
                for place in _google_places_nearby(lat, lng, cell_radius_m, keyword):
                    if _distance_km(center_lat, center_lng, place["lat"], place["lng"]) <= radius_km:
                        deduped[place["place_id"]] = place
            except Exception as e:
                print(f"[Hospitals] Google Places fetch failed near {lat},{lng} keyword={keyword}: {e}")

    if not deduped:
        return HOSPITALS, "seed", query_count

    hospitals = sorted(
        deduped.values(),
        key=lambda h: _distance_km(center_lat, center_lng, h["lat"], h["lng"])
    )
    return hospitals, "google_places", query_count


def get_hospitals(center_lat=None, center_lng=None, radius_km=None, force=False):
    center_lat = float(center_lat if center_lat is not None else HOSPITAL_CENTER_LAT)
    center_lng = float(center_lng if center_lng is not None else HOSPITAL_CENTER_LNG)
    radius_km = int(radius_km if radius_km is not None else HOSPITAL_RADIUS_KM)
    now = time.time()
    cache_fresh = (
        hospital_cache["items"]
        and hospital_cache["radius_km"] == radius_km
        and hospital_cache["center"] == (round(center_lat, 5), round(center_lng, 5))
        and now - hospital_cache["fetched_at"] < HOSPITAL_CACHE_TTL_SECONDS
    )
    if cache_fresh and not force:
        return hospital_cache["items"], hospital_cache["source"]

    hospitals, source, query_count = fetch_google_hospitals(center_lat, center_lng, radius_km)
    hospital_cache.update({
        "items": hospitals,
        "source": source,
        "fetched_at": now,
        "radius_km": radius_km,
        "center": (round(center_lat, 5), round(center_lng, 5)),
        "query_count": query_count,
    })
    return hospitals, source


def register_emergency(payload):
    """Start a cancellable LifeAlert-style emergency countdown for accidents."""
    location = payload.get("location", "unknown")
    lat = payload.get("lat", JUNCTION_COORDS.get(location, {}).get("lat", 0))
    lng = payload.get("lng", JUNCTION_COORDS.get(location, {}).get("lng", 0))
    now = time.time()

    with lock:
        existing = emergency_state["active"].get(location)
        if existing and existing["status"] == "COUNTDOWN":
            existing["last_seen_at"] = datetime.now(timezone.utc).isoformat()
            existing["payload"] = payload
            return existing

        incident = {
            "id": f"{location}-{int(now)}",
            "status": "COUNTDOWN",
            "location": location,
            "lat": lat,
            "lng": lng,
            "alert": payload.get("alert", "ACCIDENT_CRITICAL"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "cancel_deadline_epoch": now + EMERGENCY_COUNTDOWN_SECONDS,
            "countdown_seconds": EMERGENCY_COUNTDOWN_SECONDS,
            "hospital": best_hospital(lat, lng),
            "contacts": EMERGENCY_CONTACTS[:5],
            "victim": VICTIM_PROFILE,
            "impact": {
                "speed_kmph": payload.get("speed_kmph", None),
                "mic_peak_raw": payload.get("mic_peak_raw", 0),
                "vib_raw": payload.get("vib_raw", 0),
                "watchdog_fired": payload.get("watchdog_fired", False),
            },
            "payload": payload,
            "dispatch_channel": None,
            "dispatch_message": None,
        }
        emergency_state["active"][location] = incident

    log_timeline_event(location, "ACCIDENT_DETECTED", f"Alert: {payload.get('alert', 'ACCIDENT_CRITICAL')}")
    log_timeline_event(location, "COUNTDOWN_STARTED", f"{EMERGENCY_COUNTDOWN_SECONDS}s countdown")
    log_timeline_event(location, "HOSPITAL_ASSIGNED", incident["hospital"].get("name", "Unknown"))
    return incident


def dispatch_emergency(incident):
    channel = "FCM_PLUS_HOSPITAL_API" if EMERGENCY_INTERNET_ONLINE else "SMS_FALLBACK"
    message = (
        f"ACCIDENT ALERT at {incident['location']} "
        f"({incident['lat']}, {incident['lng']}). "
        f"Victim: {incident['victim']['name']}, blood: {incident['victim']['blood_type']}."
    )
    incident["status"] = "DISPATCHED"
    incident["dispatched_at"] = datetime.now(timezone.utc).isoformat()
    incident["dispatch_channel"] = channel
    incident["dispatch_message"] = message
    log_timeline_event(incident["location"], "DISPATCH_SENT", f"Channel: {channel}")
    return incident


def emergency_dispatch_loop():
    while True:
        now = time.time()
        to_dispatch = []
        with lock:
            for location, incident in emergency_state["active"].items():
                if incident["status"] == "COUNTDOWN" and now >= incident["cancel_deadline_epoch"]:
                    to_dispatch.append((location, incident))

            for location, incident in to_dispatch:
                dispatch_emergency(incident)
                emergency_state["history"].append(dict(incident))
                emergency_state["active"].pop(location, None)
                if len(emergency_state["history"]) > 100:
                    emergency_state["history"].pop(0)

            # Expire manual overrides
            expired = [loc for loc, ov in manual_overrides.items() if now >= ov.get("expires_at", 0)]
            for loc in expired:
                manual_overrides.pop(loc, None)

        time.sleep(0.5)

# ════════════════════════════════════════════════════════════════
# InfluxDB Setup
# ════════════════════════════════════════════════════════════════
write_api = None

def init_influx():
    global write_api
    if not ENABLE_INFLUX:
        print("[InfluxDB] Disabled - using in-memory dashboard data")
        return
    if not INFLUX_AVAILABLE:
        return
    try:
        client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        print("[InfluxDB] Connected")
    except Exception as e:
        print(f"[InfluxDB] Connection failed: {e}")

def write_influx(payload: dict):
    global write_api
    if not write_api:
        return
    try:
        p = (
            Point("traffic_sensor")
            .tag("location",     payload.get("location", "unknown"))
            .tag("alert",        payload.get("alert",    "CLEAR"))
            .tag("signal_phase", payload.get("signal_phase", "GREEN"))
            .field("crowd_percent",   float(payload.get("crowd_percent",  0)))
            .field("noise_db",        float(payload.get("noise_db",       40)))
            .field("aqi",             float(payload.get("aqi",            0)))
            .field("pir_raw",         float(payload.get("pir_raw",        0)))
            .field("mic_avg_raw",     float(payload.get("mic_avg_raw",    0)))
            .field("gas_raw",         float(payload.get("gas_raw",        0)))
            .field("vib_raw",         float(payload.get("vib_raw",        0)))
            .field("flag_siren",      int(payload.get("flag_siren",       False)))
            .field("flag_accident",   int(payload.get("flag_accident",    False)))
            .field("flag_crowd",      int(payload.get("flag_crowd",       False)))
            .field("flag_pollution",  int(payload.get("flag_pollution",   False)))
            .time(datetime.now(timezone.utc), WritePrecision.NS)
        )
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=p)
    except Exception as e:
        print(f"[InfluxDB] Write error: {e}")
        write_api = None

# ════════════════════════════════════════════════════════════════
# MQTT Subscriber
# ════════════════════════════════════════════════════════════════
def on_mqtt_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe(MQTT_TOPIC)
        print(f"[MQTT] Connected and subscribed to {MQTT_TOPIC}")
    else:
        print(f"[MQTT] Connection failed rc={rc}")

def on_mqtt_message(client, userdata, msg):
    try:
        payload  = json.loads(msg.payload.decode())
        location = payload.get("location", "unknown")

        # Inject known GPS coords if not in payload
        if location in JUNCTION_COORDS:
            coords = JUNCTION_COORDS[location]
            payload.setdefault("lat",  coords["lat"])
            payload.setdefault("lng",  coords["lng"])
            payload.setdefault("name", coords["name"])

        with lock:
            latest_data[location] = payload
            all_history.append(payload)
            if len(all_history) > 500:
                all_history.pop(0)

            alert = payload.get("alert", "CLEAR")
            state = alert_state.get(location, {"alert": "CLEAR", "last_logged": 0})
            now = time.time()
            if alert == "CLEAR":
                alert_state[location] = {"alert": "CLEAR", "last_logged": state["last_logged"]}
            else:
                changed = alert != state["alert"]
                repeat_due = now - state["last_logged"] >= ALERT_LOG_REPEAT_SEC
                if changed or repeat_due:
                    alert_history.append(payload)
                    if len(alert_history) > 200:
                        alert_history.pop(0)
                    alert_state[location] = {"alert": alert, "last_logged": now}

        write_influx(payload)

        alert = payload.get('alert', 'CLEAR')
        if alert == "ACCIDENT_CRITICAL" or payload.get("flag_accident", False):
            register_emergency(payload)

        # ── Emergency Corridor: SIREN detected → force GREEN on route to hospital
        if alert == "EMERGENCY_SIREN" or payload.get("flag_siren", False):
            threading.Thread(
                target=activate_emergency_corridor,
                args=(location, payload),
                daemon=True
            ).start()

        print(f"[MQTT→DB] {location:12s} | {alert:22s} | "
              f"crowd={payload.get('crowd_percent',0):3d}% | "
              f"noise={payload.get('noise_db',40):3d}dB | "
              f"aqi={payload.get('aqi',0):3d} | "
              f"phase={payload.get('signal_phase','?')}")

    except Exception as e:
        print(f"[MQTT] Message parse error: {e}")

# ════════════════════════════════════════════════════════════════
# Emergency Corridor — Signal Preemption
# ════════════════════════════════════════════════════════════════
CORRIDOR_GREEN_DURATION = int(os.getenv("CORRIDOR_GREEN_DURATION", "90"))   # seconds
CORRIDOR_COMMAND_TOPIC  = os.getenv("CORRIDOR_COMMAND_TOPIC", "traffic/command")


def _closest_junction_to(lat, lng):
    """Find the junction ID in JUNCTION_COORDS closest to (lat, lng)."""
    best_id, best_dist = None, float("inf")
    for jid, jcoord in JUNCTION_COORDS.items():
        d = _distance_km(lat, lng, jcoord["lat"], jcoord["lng"])
        if d < best_dist:
            best_dist, best_id = d, jid
    return best_id, best_dist


def activate_emergency_corridor(origin_location, payload):
    """
    Called when EMERGENCY_SIREN is detected.
    1. Find nearest TRAUMA_READY/AVAILABLE hospital.
    2. Find closest junction to that hospital.
    3. Run Dijkstra to get route junctions.
    4. Publish FORCE_GREEN command to each junction via MQTT.
    5. Track active corridor so dashboard can draw it.
    """
    global mqtt_publish_client

    lat = payload.get("lat") or JUNCTION_COORDS.get(origin_location, {}).get("lat", 0)
    lng = payload.get("lng") or JUNCTION_COORDS.get(origin_location, {}).get("lng", 0)

    # 1. Best hospital
    hospital = best_hospital(lat, lng)
    h_lat, h_lng = hospital["lat"], hospital["lng"]

    # 2. Closest junction to hospital
    dest_id, _ = _closest_junction_to(h_lat, h_lng)

    # 3. Route via Dijkstra (distance-only weight for fastest corridor)
    try:
        import route_model as rm
        graph = rm.build_graph(JUNCTION_COORDS, max_edge_km=8.0)
        def weight_dist(f, t, d): return d
        _, path = rm.dijkstra(graph, weight_dist, origin_location, dest_id)
    except Exception as exc:
        print(f"[CORRIDOR] Route calculation failed: {exc}")
        path = [origin_location]   # fallback: just the origin

    if not path:
        path = [origin_location]

    now = time.time()
    expires_at = now + CORRIDOR_GREEN_DURATION

    # Build corridor record
    corridor = {
        "origin": origin_location,
        "hospital": hospital["name"],
        "hospital_lat": h_lat,
        "hospital_lng": h_lng,
        "dest_junction": dest_id,
        "path": path,
        "path_coords": [
            {"id": jid, **{k: v for k, v in JUNCTION_COORDS.get(jid, {}).items()}}
            for jid in path
        ],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "duration_seconds": CORRIDOR_GREEN_DURATION,
    }

    with lock:
        active_corridors[origin_location] = corridor

    log_timeline_event(
        origin_location, "CORRIDOR_ACTIVATED",
        f"Green corridor: {len(path)} junctions → {hospital['name']}"
    )

    print(f"[CORRIDOR] SIREN @ {origin_location} → {hospital['name']} | "
          f"{len(path)} junctions forced GREEN for {CORRIDOR_GREEN_DURATION}s")

    # 4. Publish FORCE_GREEN to each junction on the path
    if mqtt_publish_client:
        for jid in path:
            cmd = json.dumps({
                "type": "SIGNAL_COMMAND",
                "command": "FORCE_GREEN",
                "location": jid,
                "duration_seconds": CORRIDOR_GREEN_DURATION,
                "reason": "EMERGENCY_SIREN_CORRIDOR",
                "origin": origin_location,
                "hospital": hospital["name"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            try:
                mqtt_publish_client.publish(CORRIDOR_COMMAND_TOPIC, cmd)
            except Exception as exc:
                print(f"[CORRIDOR] MQTT publish failed for {jid}: {exc}")
    else:
        print("[CORRIDOR] MQTT publish client not ready — commands not sent to hardware")

    return corridor


def corridor_expiry_loop():
    """Background thread: expire corridors after their duration."""
    while True:
        now = time.time()
        with lock:
            expired = [loc for loc, c in active_corridors.items()
                       if now >= c.get("expires_at", 0)]
            for loc in expired:
                c = active_corridors.pop(loc)
                log_timeline_event(loc, "CORRIDOR_EXPIRED",
                                   f"Green corridor to {c['hospital']} ended")
                print(f"[CORRIDOR] Expired: {loc} → {c['hospital']}")
        time.sleep(2)


def start_mqtt():
    global mqtt_publish_client
    client = mqtt.Client(client_id=f"tpsmc_server_{os.getpid()}")
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    # Separate publish client so commands don't block data ingestion
    pub_client = mqtt.Client(client_id=f"tpsmc_pub_{os.getpid()}")
    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, 60)
            try:
                pub_client.connect(MQTT_BROKER, MQTT_PORT, 60)
                pub_client.loop_start()
                mqtt_publish_client = pub_client
                print(f"[CORRIDOR] MQTT publish client ready on {CORRIDOR_COMMAND_TOPIC}")
            except Exception as pe:
                print(f"[CORRIDOR] Publish client connect failed: {pe}")
            client.loop_forever()
        except Exception as e:
            print(f"[MQTT] Error: {e} — retrying in 5s")
            mqtt_publish_client = None
            import time; time.sleep(5)

# ════════════════════════════════════════════════════════════════
# REST API
# ════════════════════════════════════════════════════════════════

@app.route("/api/latest")
def api_latest():
    with lock:
        data = dict(latest_data)
    return jsonify({"status": "ok", "count": len(data), "data": data})

@app.route("/api/latest/<location>")
def api_latest_location(location):
    with lock:
        d = dict(latest_data.get(location, {}))
    if d:
        return jsonify({"status": "ok", "data": d})
    return jsonify({"status": "error", "message": "Not found"}), 404

@app.route("/api/alerts")
def api_alerts():
    limit = int(request.args.get("limit", 50))
    atype = request.args.get("type")         # Filter by alert type
    loc   = request.args.get("location")     # Filter by location
    with lock:
        alerts = list(reversed(alert_history))
    if atype:
        alerts = [a for a in alerts if a.get("alert") == atype]
    if loc:
        alerts = [a for a in alerts if a.get("location") == loc]
    return jsonify({"status": "ok", "count": len(alerts[:limit]),
                    "alerts": alerts[:limit]})

@app.route("/api/summary")
def api_summary():
    with lock:
        locs      = list(latest_data.values())
        total_alr = len(alert_history)
        emerg     = sum(1 for a in alert_history
                        if a.get("alert") in ("EMERGENCY_SIREN","ACCIDENT_CRITICAL"))
    total     = len(locs)
    by_alert  = {}
    for d in locs:
        a = d.get("alert", "CLEAR")
        by_alert[a] = by_alert.get(a, 0) + 1
    avg_crowd = sum(d.get("crowd_percent",0) for d in locs) / max(total,1)
    avg_noise = sum(d.get("noise_db",40)     for d in locs) / max(total,1)
    avg_aqi   = sum(d.get("aqi",0)           for d in locs) / max(total,1)
    return jsonify({
        "status":           "ok",
        "total_junctions":  total,
        "alert_breakdown":  by_alert,
        "avg_crowd_pct":    round(avg_crowd, 1),
        "avg_noise_db":     round(avg_noise, 1),
        "avg_aqi":          round(avg_aqi,   1),
        "total_alerts":     total_alr,
        "critical_alerts":  emerg,
    })

def compute_congestion_score(payload, location=None, is_online=True):
    """Deterministic congestion scoring from real sensor data.
    Weights: crowd 0-35, alert 0-30, signal 0-15, pollution 0-10, noise 0-5, trend 0-5.
    Returns (score, level, data_source, factors).
    """
    factors = []

    # Determine data source
    ts = payload.get("timestamp")
    now_epoch = time.time()
    if not is_online and not payload:
        if TPSMC_DEMO_MODE:
            # Demo fallback: provide a synthetic score
            demo_scores = {"junction_A": 52, "junction_B": 35, "junction_C": 20}
            score = demo_scores.get(location, 15)
            level = "HIGH" if score >= 75 else "MEDIUM" if score >= 45 else "LOW"
            return score, level, "demo_fallback", ["demo mode active"]
        return 0, "OFFLINE", "offline", ["no sensor data"]

    stale = False
    if ts:
        try:
            ts_epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if now_epoch - ts_epoch > STALE_SENSOR_SECONDS:
                stale = True
        except (ValueError, TypeError):
            pass
    data_source = "stale_sensor" if stale else "live_sensor"
    if stale:
        factors.append("stale data (>60s)")

    # ── Crowd / PIR: 0-35 pts ──
    crowd = float(payload.get("crowd_percent", 0))
    pir = float(payload.get("pir_raw", 0))
    crowd_pts = min(35, crowd * 0.35)
    if crowd > 70:
        factors.append(f"crowd={int(crowd)}%")
    elif crowd > 40:
        factors.append(f"crowd={int(crowd)}%")
    if pir > 0 and crowd < 10:
        crowd_pts = max(crowd_pts, 8)
        factors.append("PIR motion detected")

    # ── Alert severity: 0-30 pts ──
    alert = payload.get("alert", "CLEAR")
    alert_pts = {"ACCIDENT_CRITICAL": 30, "EMERGENCY_SIREN": 25,
                 "CROWD_WARNING": 15, "POLLUTION_ALERT": 12,
                 "CLEAR": 0, "OFFLINE": 0}.get(alert, 0)
    if alert_pts > 0:
        factors.append(f"alert={alert}")

    # ── Signal delay risk: 0-15 pts ──
    phase = payload.get("signal_phase", "GREEN")
    signal_pts = {"RED": 15, "AMBER": 8, "GREEN": 0}.get(phase, 0)
    if signal_pts > 0:
        factors.append(f"signal={phase}")

    # ── Pollution / AQI: 0-10 pts ──
    aqi = float(payload.get("aqi", 0))
    pollution_pts = min(10, aqi * 0.05)
    if aqi > 100:
        factors.append(f"AQI={int(aqi)}")

    # ── Noise / activity: 0-5 pts ──
    noise = float(payload.get("noise_db", 40))
    noise_pts = min(5, max(0, (noise - 50) * 0.1))
    if noise > 80:
        factors.append(f"noise={int(noise)}dB")

    # ── Trend / recency: 0-5 pts ──
    trend_pts = 0
    with lock:
        loc_history = [h for h in all_history if h.get("location") == location][-5:]
    if len(loc_history) >= 3:
        recent_crowds = [float(h.get("crowd_percent", 0)) for h in loc_history]
        if recent_crowds[-1] > recent_crowds[0] + 10:
            trend_pts = 5
            factors.append("rising crowd trend")
        elif recent_crowds[-1] < recent_crowds[0] - 10:
            trend_pts = 0
            factors.append("falling congestion")
        else:
            trend_pts = 2
            factors.append("stable recent flow")

    score = int(min(100, max(0, crowd_pts + alert_pts + signal_pts + pollution_pts + noise_pts + trend_pts)))

    if score >= 75:
        level = "CRITICAL"
    elif score >= 45:
        level = "HIGH"
    elif score >= 20:
        level = "MEDIUM"
    else:
        level = "LOW"

    if not factors:
        factors.append("all sensors nominal")

    return score, level, data_source, factors


@app.route("/api/map")
def api_map():
    """All junctions with current status for map markers — includes congestion scoring."""
    with lock:
        result = []
        # Include known junctions even if no data yet
        for loc_id, coords in JUNCTION_COORDS.items():
            data = latest_data.get(loc_id, {})
            is_online = loc_id in latest_data
            score, level, data_source, score_factors = compute_congestion_score(
                data, location=loc_id, is_online=is_online
            )
            result.append({
                "id":               loc_id,
                "name":             coords["name"],
                "lat":              coords["lat"],
                "lng":              coords["lng"],
                "alert":            data.get("alert",         "OFFLINE"),
                "signal_phase":     data.get("signal_phase",  "RED"),
                "crowd_percent":    data.get("crowd_percent", 0),
                "noise_db":         data.get("noise_db",      0),
                "aqi":              data.get("aqi",           0),
                "vib_raw":          data.get("vib_raw",       0),
                "timestamp":        data.get("timestamp",     None),
                "online":           is_online,
                "congestion_score": score,
                "congestion_level": level,
                "data_source":      data_source,
                "score_factors":    score_factors,
            })
    return jsonify({"status": "ok", "junctions": result})

@app.route("/api/hospitals")
def api_hospitals():
    """Hospitals from Google Places within the requested radius, with seeded fallback."""
    lat = float(request.args.get("lat", HOSPITAL_CENTER_LAT))
    lng = float(request.args.get("lng", HOSPITAL_CENTER_LNG))
    radius_km = int(float(request.args.get("radius_km", HOSPITAL_RADIUS_KM)))
    refresh = request.args.get("refresh", "false").lower() == "true"
    hospitals, source = get_hospitals(lat, lng, radius_km, force=refresh)
    enriched = simulate_hospital_capacity(hospitals)
    return jsonify({
        "status": "ok",
        "source": source,
        "count": len(enriched),
        "radius_km": radius_km,
        "center": {"lat": lat, "lng": lng},
        "google_configured": bool(GOOGLE_MAPS_API_KEY),
        "grid": HOSPITAL_SEARCH_GRID,
        "keywords": HOSPITAL_SEARCH_KEYWORDS,
        "query_count": hospital_cache.get("query_count", 0),
        "hospitals": enriched,
    })

@app.route("/api/history")
def api_history():
    """Recent readings for charts — last N entries."""
    limit    = int(request.args.get("limit", 60))
    location = request.args.get("location")
    with lock:
        data = list(all_history)
    if location:
        data = [d for d in data if d.get("location") == location]
    data = data[-limit:]
    return jsonify({"status": "ok", "count": len(data), "data": data})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Clear in-memory dashboard state for a fresh demo run."""
    with lock:
        latest_data.clear()
        alert_history.clear()
        all_history.clear()
        emergency_state["active"].clear()
        emergency_state["history"].clear()
    return jsonify({"status": "ok", "message": "Dashboard state reset"})

@app.route("/api/emergency/status")
def api_emergency_status():
    location = request.args.get("location")
    now = time.time()
    with lock:
        active = list(emergency_state["active"].values())
        if location:
            active = [i for i in active if i.get("location") == location]
        result = []
        for incident in active:
            item = dict(incident)
            item.pop("payload", None)
            item["seconds_remaining"] = max(0, int(item["cancel_deadline_epoch"] - now))
            result.append(item)
        history = list(reversed(emergency_state["history"][-20:]))
        for item in history:
            item.pop("payload", None)
    return jsonify({"status": "ok", "active": result, "history": history})

@app.route("/api/emergency/cancel", methods=["POST"])
def api_emergency_cancel():
    body = request.get_json(silent=True) or {}
    location = body.get("location") or request.args.get("location")
    if not location:
        return jsonify({"status": "error", "message": "location is required"}), 400

    with lock:
        incident = emergency_state["active"].get(location)
        if not incident:
            return jsonify({"status": "error", "message": "No active emergency for location"}), 404
        if incident["status"] != "COUNTDOWN":
            return jsonify({"status": "error", "message": f"Cannot cancel {incident['status']} incident"}), 409
        incident["status"] = "CANCELLED"
        incident["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        emergency_state["history"].append(dict(incident))
        emergency_state["active"].pop(location, None)
        if len(emergency_state["history"]) > 100:
            emergency_state["history"].pop(0)
        result = dict(incident)
        result.pop("payload", None)
    log_timeline_event(location, "USER_CANCELLED", "Operator cancelled during countdown")
    return jsonify({"status": "ok", "incident": result})

@app.route("/api/emergency/dispatch", methods=["POST"])
def api_emergency_dispatch():
    body = request.get_json(silent=True) or {}
    location = body.get("location") or request.args.get("location")
    if not location:
        return jsonify({"status": "error", "message": "location is required"}), 400

    with lock:
        incident = emergency_state["active"].get(location)
        if not incident:
            return jsonify({"status": "error", "message": "No active emergency for location"}), 404
        dispatch_emergency(incident)
        emergency_state["history"].append(dict(incident))
        emergency_state["active"].pop(location, None)
        if len(emergency_state["history"]) > 100:
            emergency_state["history"].pop(0)
        result = dict(incident)
        result.pop("payload", None)
    return jsonify({"status": "ok", "incident": result})

@app.route("/api/incidents/timeline")
def api_incidents_timeline():
    """Return incident lifecycle events."""
    location = request.args.get("location")
    limit = int(request.args.get("limit", 20))
    events = list(reversed(incident_timeline))
    if location:
        events = [e for e in events if e.get("location") == location]
    return jsonify({"status": "ok", "events": events[:limit]})

@app.route("/api/control/manual", methods=["POST"])
def api_control_manual():
    """Set a manual signal override for a junction."""
    body = request.get_json(silent=True) or {}
    location = body.get("location")
    mode = body.get("mode", "NORMAL")
    duration = int(body.get("duration_seconds", 60))
    if not location:
        return jsonify({"status": "error", "message": "location is required"}), 400
    valid_modes = ["FORCE_RED", "EXTEND_GREEN", "CLEAR_CORRIDOR", "NORMAL"]
    if mode not in valid_modes:
        return jsonify({"status": "error", "message": f"mode must be one of {valid_modes}"}), 400
    now = time.time()
    if mode == "NORMAL":
        with lock:
            manual_overrides.pop(location, None)
        return jsonify({"status": "ok", "message": "Override cleared", "location": location})
    override = {
        "location": location,
        "mode": mode,
        "duration_seconds": duration,
        "started_at": now,
        "expires_at": now + duration,
        "set_at": datetime.now(timezone.utc).isoformat(),
    }
    with lock:
        manual_overrides[location] = override
    return jsonify({"status": "ok", "override": override})

@app.route("/api/control/status")
def api_control_status():
    """Return active manual overrides."""
    location = request.args.get("location")
    now = time.time()
    with lock:
        if location:
            ov = manual_overrides.get(location)
            if ov:
                result = dict(ov)
                result["expires_in"] = max(0, int(ov["expires_at"] - now))
                return jsonify({"status": "ok", "overrides": [result]})
            return jsonify({"status": "ok", "overrides": []})
        result = []
        for loc, ov in manual_overrides.items():
            item = dict(ov)
            item["expires_in"] = max(0, int(ov["expires_at"] - now))
            result.append(item)
    return jsonify({"status": "ok", "overrides": result})

@app.route("/api/intelligence")
def api_intelligence():
    """Decision intelligence layer for dashboard/demo narrative."""
    location = request.args.get("location")
    with lock:
        if location and location in latest_data:
            current = latest_data[location]
        elif latest_data:
            current = list(latest_data.values())[-1]
        else:
            current = {}

    alert = current.get("alert", "OFFLINE")
    crowd = float(current.get("crowd_percent", 0))
    noise = float(current.get("noise_db", 40))
    aqi = float(current.get("aqi", 0))
    phase = current.get("signal_phase", "RED" if alert == "OFFLINE" else "GREEN")

    loc_resolve = location or current.get("location") or ("junction_A" if "junction_A" in JUNCTION_COORDS else next(iter(JUNCTION_COORDS), None))
    gj_lat = gj_lng = None
    try:
        if current.get("lat") not in (None, "", 0) and current.get("lng") not in (None, "", 0):
            gj_lat = float(current["lat"])
            gj_lng = float(current["lng"])
        elif loc_resolve and loc_resolve in JUNCTION_COORDS:
            gj_lat = float(JUNCTION_COORDS[loc_resolve]["lat"])
            gj_lng = float(JUNCTION_COORDS[loc_resolve]["lng"])
    except (TypeError, ValueError, KeyError):
        gj_lat = gj_lng = None

    google_road_ctx = google_road_traffic_context(gj_lat, gj_lng)
    google_drive_score = google_road_ctx.get("score")
    google_boost = min(24, int(google_drive_score * 0.24)) if google_drive_score is not None else 0

    emergency_weight = 60 if alert in ("EMERGENCY_SIREN", "ACCIDENT_CRITICAL") else 0
    crowd_weight = min(25, crowd * 0.25)
    pollution_weight = min(15, aqi * 0.075)
    lane_priority_score = int(min(100, emergency_weight + crowd_weight + pollution_weight + google_boost))

    # Use the same deterministic congestion model as /api/map
    is_online = bool(current)
    loc_key_for_score = current.get("location", location or "junction_A")
    cong_score, cong_level, cong_source, cong_factors = compute_congestion_score(
        current, location=loc_key_for_score, is_online=is_online
    )
    road_congestion_score = cong_score
    pir_crowd_score = int(max(0, min(100, crowd)))
    pollution_score = int(max(0, min(100, aqi / 2)))
    base_fused = int(
        min(100, (0.55 * pir_crowd_score) + (0.30 * road_congestion_score) + (0.15 * pollution_score))
    )
    if google_drive_score is not None:
        fused_density_score = int(min(100, 0.62 * base_fused + 0.38 * google_drive_score))
    else:
        fused_density_score = base_fused
    predicted_density_score = int(min(100, fused_density_score + (12 if alert != "CLEAR" else -5)))
    if predicted_density_score >= 75:
        ml_prediction = "Congestion likely"
        ml_confidence = 86
    elif predicted_density_score >= 45:
        ml_prediction = "Moderate flow"
        ml_confidence = 74
    else:
        ml_prediction = "Stable flow"
        ml_confidence = 68

    route_risk_score = int(min(100, (0.42 * predicted_density_score) + (0.30 * lane_priority_score) + (0.18 * pollution_score) + (0.10 * max(0, noise - 40))))
    route_safety_score = int(max(0, 100 - route_risk_score))
    route_free_flow_score = int(max(0, 100 - ((0.62 * predicted_density_score) + (0.25 * road_congestion_score) + (0.13 * crowd))))

    if alert == "ACCIDENT_CRITICAL":
        recommendation = "Hold cross traffic, force RED, dispatch incident response."
        priority_lane = "Incident approach"
        signal_action = "FORCE_RED_AND_CLEAR_CORRIDOR"
        recommended_route_mode = "Avoid incident approach"
    elif alert == "EMERGENCY_SIREN":
        recommendation = "Create emergency corridor and suppress conflicting lanes."
        priority_lane = "Emergency corridor"
        signal_action = "PREEMPT_FOR_EMERGENCY"
        recommended_route_mode = "Yield corridor, reroute general traffic"
    elif alert == "POLLUTION_ALERT":
        recommendation = "Reduce idling with shorter cycles and smoother release."
        priority_lane = "Low-emission release"
        signal_action = "SHORT_CYCLE_RELEASE"
        recommended_route_mode = "Prefer low-idle route"
    elif alert == "CROWD_WARNING":
        recommendation = "Extend pedestrian-safe amber and slow vehicle release."
        priority_lane = "Pedestrian-safe side"
        signal_action = "PROTECT_CROWD_CROSSING"
        recommended_route_mode = "Avoid pedestrian-dense edge"
    elif alert == "CLEAR" and predicted_density_score >= 65:
        recommendation = "Balance green time toward the lower-risk outbound lane."
        priority_lane = "Free-flow outbound lane"
        signal_action = "ADAPTIVE_GREEN_SPLIT"
        recommended_route_mode = "Prefer free-flow route"
    elif alert == "CLEAR":
        recommendation = "Run normal adaptive timing."
        priority_lane = "Balanced flow"
        signal_action = "NORMAL_ADAPTIVE"
        recommended_route_mode = "Direct route acceptable"
    else:
        recommendation = "No live data; keep fail-safe RED until heartbeat returns."
        priority_lane = "Fail-safe"
        signal_action = "FAIL_SAFE_RED"
        recommended_route_mode = "Avoid offline node"

    route_reasons = []
    if alert in ("ACCIDENT_CRITICAL", "EMERGENCY_SIREN"):
        route_reasons.append("critical alert active")
    if predicted_density_score >= 65:
        route_reasons.append("predicted density high")
    if aqi >= 100:
        route_reasons.append("air quality risk elevated")
    if crowd >= 60:
        route_reasons.append("crowd density high")
    if google_drive_score is not None and google_drive_score >= 55:
        route_reasons.append("Google live road delay elevated nearby")
    if not route_reasons:
        route_reasons.append("risk signals stable")

    if aqi >= 140:
        pollution_risk = "HIGH"
    elif aqi >= 75:
        pollution_risk = "MEDIUM"
    else:
        pollution_risk = "LOW"

    # Alert confidence
    conf = calculate_alert_confidence(current)

    # Manual override annotation
    loc_key = current.get("location", location or "junction_A")
    override_info = {}
    with lock:
        ov = manual_overrides.get(loc_key)
        if ov:
            now_ov = time.time()
            override_info = {
                "manual_override_active": True,
                "manual_override_mode": ov["mode"],
                "manual_override_expires_in": max(0, int(ov["expires_at"] - now_ov)),
            }
            signal_action = ov["mode"]
            recommendation = f"MANUAL OVERRIDE: {ov['mode']} for {max(0, int(ov['expires_at'] - now_ov))}s remaining."

    traffic_sources = ["sensor-derived congestion model"]
    if google_drive_score is not None:
        traffic_sources.append("Google Distance Matrix (duration_in_traffic)")

    resp = {
        "status": "ok",
        "location": loc_key,
        "alert": alert,
        "signal_phase": phase,
        "lane_priority_score": lane_priority_score,
        "priority_lane": priority_lane,
        "signal_action": signal_action,
        "route_safety_score": route_safety_score,
        "route_free_flow_score": route_free_flow_score,
        "recommended_route_mode": recommended_route_mode,
        "route_reasons": route_reasons,
        "pir_crowd_score": pir_crowd_score,
        "road_congestion_score": road_congestion_score,
        "fused_density_score": fused_density_score,
        "predicted_density_score": predicted_density_score,
        "ml_prediction": ml_prediction,
        "ml_confidence": ml_confidence,
        "traffic_context_source": " + ".join(traffic_sources),
        "prediction_horizon": "10 min",
        "pollution_risk": pollution_risk,
        "recommendation": recommendation,
        "alert_confidence": conf["alert_confidence"],
        "confidence_factors": conf["confidence_factors"],
        "congestion_score": cong_score,
        "congestion_level": cong_level,
        "congestion_data_source": cong_source,
        "congestion_factors": cong_factors,
        "google_road_traffic": google_road_ctx,
        "sensor_fused_density": base_fused,
    }
    resp.update(override_info)
    return jsonify(resp)

@app.route("/api/route-suggest")
def api_route_suggest():
    """ML-powered route suggestion: shortest vs safest path between junctions."""
    origin = request.args.get("origin", "").strip()
    destination = request.args.get("destination", "").strip()

    if not origin or not destination:
        return jsonify({
            "error": "Provide ?origin=junction_id&destination=junction_id",
            "available_junctions": [
                {"id": k, "name": v.get("name", k)}
                for k, v in list(JUNCTION_COORDS.items())[:30]
            ],
        }), 400

    # Build live data dict from latest sensor readings
    with lock:
        live_data = {}
        for jid, jcoord in JUNCTION_COORDS.items():
            payload = latest_data.get(jid, {})
            is_online = bool(payload)
            cong_score, cong_level, cong_src, cong_factors = compute_congestion_score(payload, jid, is_online)
            live_data[jid] = {
                "congestion_score": cong_score,
                "alert": payload.get("alert", "OFFLINE"),
                "crowd_percent": float(payload.get("crowd_percent", 0)),
                "aqi": float(payload.get("aqi", 0)),
                "noise_db": float(payload.get("noise_db", 0)),
            }

    result = route_model.suggest_routes(JUNCTION_COORDS, live_data, origin, destination)
    return jsonify(result)

@app.route("/api/route-rank")
def api_route_rank():
    """ML safety ranking of all junctions — used by dashboard safety panel."""
    with lock:
        live_data = {}
        for jid in JUNCTION_COORDS:
            payload = latest_data.get(jid, {})
            is_online = bool(payload)
            cong_score, _, _, _ = compute_congestion_score(payload, jid, is_online)
            live_data[jid] = {
                "congestion_score": cong_score,
                "alert": payload.get("alert", "OFFLINE"),
                "crowd_percent": float(payload.get("crowd_percent", 0)),
                "aqi": float(payload.get("aqi", 0)),
                "noise_db": float(payload.get("noise_db", 0)),
            }
    ranked = route_model.rank_junctions(JUNCTION_COORDS, live_data)
    return jsonify({"status": "ok", "ranked": ranked, "model": "TPSMC-RouteNet v1.0"})

@app.route("/api/health")
def api_health():
    with lock:
        online = len(latest_data)
    return jsonify({"status": "ok", "junctions_online": online,
                    "influx": write_api is not None,
                    "service": "TPSMC Server v2.0"})


@app.route("/api/emergency/corridor")
def api_emergency_corridor():
    """
    Returns all active emergency green corridors.
    Dashboard uses this to draw the green path on the map.
    """
    loc = request.args.get("location")
    with lock:
        if loc:
            corridors = {loc: active_corridors[loc]} if loc in active_corridors else {}
        else:
            corridors = dict(active_corridors)

    now = time.time()
    result = []
    for location, c in corridors.items():
        remaining = max(0, int(c["expires_at"] - now))
        result.append({
            "origin": c["origin"],
            "hospital": c["hospital"],
            "hospital_lat": c["hospital_lat"],
            "hospital_lng": c["hospital_lng"],
            "path": c["path"],
            "path_coords": c["path_coords"],
            "created_at": c["created_at"],
            "duration_seconds": c["duration_seconds"],
            "remaining_seconds": remaining,
            "active": remaining > 0,
        })

    return jsonify({
        "status": "ok",
        "active_corridors": len(result),
        "corridors": result,
    })


@app.route("/")
def dashboard():
    return render_template("dashboard.html")

# ════════════════════════════════════════════════════════════════
# Startup
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_influx()
    t = threading.Thread(target=start_mqtt, daemon=True)
    t.start()
    e = threading.Thread(target=emergency_dispatch_loop, daemon=True)
    e.start()
    c = threading.Thread(target=corridor_expiry_loop, daemon=True)
    c.start()
    print("[SERVER] TPSMC Server starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
