#!/usr/bin/env python3
import argparse
import json
import math
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import serial


MQTT_TOPIC = "traffic/edge"
LOCATION_ID = "junction_A"

MIC_SIREN_THRESH = 2400
MIC_ACCIDENT_THRESH = 3300
GAS_POLL_THRESH = 1800


def parse_line(line):
    line = line.strip()
    if not line.startswith("DATA,"):
        return None

    parts = line.split(",")
    if len(parts) != 6:
        return None

    try:
        return {
            "pir": int(parts[1]),
            "mic_avg": int(parts[2]),
            "mic_peak": int(parts[3]),
            "gas": int(parts[4]),
            "vib": int(parts[5]),
        }
    except ValueError:
        return None


def decide(sample):
    pir = sample["pir"]
    mic_avg = sample["mic_avg"]
    mic_peak = sample["mic_peak"]
    gas = sample["gas"]
    vib = sample["vib"]

    flag_accident = mic_peak > MIC_ACCIDENT_THRESH or vib == 1
    flag_siren = mic_avg > MIC_SIREN_THRESH
    flag_pollution = gas > GAS_POLL_THRESH
    flag_crowd = pir == 1

    if flag_accident:
        alert_code, alert, phase = 4, "ACCIDENT_CRITICAL", "RED"
    elif flag_siren:
        alert_code, alert, phase = 3, "EMERGENCY_SIREN", "RED"
    elif flag_pollution:
        alert_code, alert, phase = 2, "POLLUTION_ALERT", "AMBER"
    elif flag_crowd:
        alert_code, alert, phase = 1, "CROWD_WARNING", "AMBER"
    else:
        alert_code, alert, phase = 0, "CLEAR", "GREEN"

    noise_db = int(40 + (min(mic_avg, 4095) / 4095) * 60)
    aqi = int((min(gas, 4095) / 4095) * 200)

    return {
        "alert_code": alert_code,
        "alert": alert,
        "signal_phase": phase,
        "crowd_percent": 100 if flag_crowd else 0,
        "noise_db": noise_db,
        "aqi": aqi,
        "flag_siren": flag_siren,
        "flag_accident": flag_accident,
        "flag_crowd": flag_crowd,
        "flag_pollution": flag_pollution,
        "watchdog_fired": False,
        "fpga_processed": False,
    }


def build_payload(sample):
    result = decide(sample)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "location": LOCATION_ID,
        "lat": 12.9716,
        "lng": 77.5946,
        "pir_raw": sample["pir"],
        "mic_avg_raw": sample["mic_avg"],
        "mic_peak_raw": sample["mic_peak"],
        "gas_raw": sample["gas"],
        "vib_raw": sample["vib"],
        **result,
    }


def main():
    parser = argparse.ArgumentParser(description="ESP32 serial DATA bridge to TPSMC MQTT dashboard")
    parser.add_argument("--port", default="COM3", help="ESP32 serial port, e.g. COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--publish-interval", type=float, default=0.5)
    args = parser.parse_args()

    client = mqtt.Client(client_id="esp32_serial_bridge")
    client.connect(args.broker, args.mqtt_port, 60)
    client.loop_start()

    print(f"[SERIAL] Opening {args.port} at {args.baud}")
    print(f"[MQTT] Publishing to {args.broker}:{args.mqtt_port}/{MQTT_TOPIC}")
    print("[MAIN] Close Arduino Serial Monitor before running this script")

    ser = serial.Serial(args.port, args.baud, timeout=1)
    last_publish = 0

    last_status = 0

    try:
        while True:
            raw = ser.readline().decode("utf-8", errors="ignore")
            line = raw.strip()
            if line and not line.startswith("DATA,"):
                now = time.time()
                if now - last_status > 1.0:
                    print(f"[SERIAL] {line}", flush=True)
                    last_status = now

            sample = parse_line(raw)
            if sample is None:
                continue

            now = time.time()
            if now - last_publish < args.publish_interval:
                continue

            payload = build_payload(sample)
            client.publish(MQTT_TOPIC, json.dumps(payload), qos=0)
            print(
                f"[PUB] {payload['alert']:<18} phase={payload['signal_phase']:<5} "
                f"pir={sample['pir']} mic={sample['mic_avg']}/{sample['mic_peak']} "
                f"gas={sample['gas']} vib={sample['vib']}",
                flush=True,
            )
            last_publish = now
    except KeyboardInterrupt:
        print("\n[STOP] Serial bridge stopped")
    finally:
        ser.close()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
