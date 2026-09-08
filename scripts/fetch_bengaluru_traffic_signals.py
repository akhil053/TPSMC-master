#!/usr/bin/env python3
"""
Download Bengaluru UA traffic_signals nodes from OpenStreetMap (Overpass API)
into data/bengaluru_traffic_signals.json for TPSMC /api/map.

Usage (from repo root):  python scripts/fetch_bengaluru_traffic_signals.py

Requires outbound HTTPS. Respect Overpass fair-use limits.
"""
from __future__ import annotations

import json
import os
import urllib.request


# South-West to North-East (Blr metro + outskirts)
BBox = tuple[float, float, float, float]  # south, west, north, east
DEFAULT_BBOX: BBox = (12.72, 77.38, 13.23, 77.92)

OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")


def fetch_signals(bbox: BBox = DEFAULT_BBOX) -> list[dict]:
    south, west, north, east = bbox
    query = (
        '[out:json][timeout:180];'
        f'node["highway"="traffic_signals"]({south},{west},{north},{east});'
        "out;"
    )
    req = urllib.request.Request(
        OVERPASS_URL,
        data=query.encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "TPSMC-scripts/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.load(resp)

    signals: list[dict] = []
    for el in data.get("elements", []):
        if el.get("type") != "node":
            continue
        oid = el.get("id")
        lat, lon = el.get("lat"), el.get("lon")
        tags = el.get("tags") or {}
        if oid is None or lat is None or lon is None:
            continue
        name = tags.get("name") or ""
        if not name:
            name = f"Traffic signal #{oid}"
        signals.append({"osm_id": int(oid), "lat": lat, "lng": lon, "name": name[:120]})

    signals.sort(key=lambda x: (x["lat"], x["lng"]))
    return signals


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "bengaluru_traffic_signals.json")

    bbox = DEFAULT_BBOX
    raw_bb = os.getenv("OVERPASS_BLR_BBOX")
    if raw_bb:
        parts = [float(x) for x in raw_bb.replace(",", " ").split()]
        if len(parts) == 4:
            bbox = (parts[0], parts[1], parts[2], parts[3])

    signals = fetch_signals(bbox)
    payload = {
        "source": "OpenStreetMap",
        "bbox": list(bbox),
        "tags": {"highway": "traffic_signals"},
        "count": len(signals),
        "signals": signals,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Wrote {len(signals)} signals to {out_path}")


if __name__ == "__main__":
    main()
