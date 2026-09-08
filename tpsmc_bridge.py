#!/usr/bin/env python3
"""
tpsmc_bridge.py  â€”  Runs on PYNQ-Z2 ARM core
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Flow:
  1. Read CSV from ESP32 via UART (Serial /dev/ttyPS1)
  2. Parse sensor values
  3. Write to FPGA PL via MMIO (AXI GPIO)
  4. Read processed results from FPGA
  5. Publish JSON to MQTT broker on server
  6. Service watchdog every 150ms

Usage:
  python3 tpsmc_bridge.py

Run on PYNQ via SSH or Jupyter terminal.
"""

import time
import json
import serial
import struct
import threading
import os
from datetime import datetime, timezone

# â”€â”€ Try importing PYNQ library â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    from pynq import Overlay, MMIO
    PYNQ_MODE = True
    print("[BOOT] PYNQ library found â€” hardware mode")
except ImportError:
    PYNQ_MODE = False
    print("[BOOT] PYNQ library NOT found â€” simulation mode")

import paho.mqtt.client as mqtt

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Configuration â€” edit these to match your setup
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
MQTT_BROKER    = os.getenv("MQTT_BROKER", "192.168.1.100")   # Server laptop IP
MQTT_PORT      = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC     = os.getenv("MQTT_TOPIC", "traffic/edge")
LOCATION_ID    = os.getenv("LOCATION_ID", "junction_A")      # Change per physical unit

UART_PORT      = os.getenv("UART_PORT", "/dev/ttyPS1")       # PYNQ UART connected to ESP32 TX
UART_BAUD      = int(os.getenv("UART_BAUD", "115200"))

BITSTREAM_PATH = os.getenv("BITSTREAM_PATH", "/home/xilinx/tpsmc_edge.bit")

# AXI GPIO Base Addresses (from Vivado Address Editor)
# Adjust these to match your block design
SENSOR_REG0_ADDR = 0x41200000   # Write: pir + mic_avg + vib
SENSOR_REG1_ADDR = 0x41200008   # Write: mic_peak + gas
VALID_ADDR       = 0x41200010   # Write: pulse to trigger FPGA
WDOG_ADDR        = 0x41200018   # Write: pulse to service watchdog
RESULT_REG0_ADDR = 0x41210000   # Read: crowd% + noise_db + aqi + alert_code
RESULT_REG1_ADDR = 0x41210008   # Read: phase + flags

WDOG_INTERVAL    = 0.15         # Service watchdog every 150ms
PUBLISH_INTERVAL = 2.0          # MQTT publish every 2 seconds
UART_STALE_SECONDS = float(os.getenv("UART_STALE_SECONDS", "8"))
MQTT_RECONNECT_INTERVAL = float(os.getenv("MQTT_RECONNECT_INTERVAL", "5"))

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# ALERT CODE MAP
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
ALERT_CODES = {
    0: "CLEAR",
    1: "CROWD_WARNING",
    2: "POLLUTION_ALERT",
    3: "EMERGENCY_SIREN",
    4: "ACCIDENT_CRITICAL",
    5: "WATCHDOG_FAULT"
}

PHASE_MAP = {
    0b00: "RED",
    0b01: "AMBER",
    0b10: "GREEN",
    0b11: "RED"
}

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# FPGA Interface
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
class FPGAInterface:
    def __init__(self):
        if PYNQ_MODE:
            print(f"[FPGA] Loading bitstream: {BITSTREAM_PATH}")
            try:
                self.overlay     = Overlay(BITSTREAM_PATH)
                self.s_reg0      = MMIO(SENSOR_REG0_ADDR, 4)
                self.s_reg1      = MMIO(SENSOR_REG1_ADDR, 4)
                self.s_valid     = MMIO(VALID_ADDR,       4)
                self.s_wdog      = MMIO(WDOG_ADDR,        4)
                self.r_reg0      = MMIO(RESULT_REG0_ADDR, 4)
                self.r_reg1      = MMIO(RESULT_REG1_ADDR, 4)
                print("[FPGA] Bitstream loaded. PL running.")
            except Exception as e:
                print(f"[FPGA] ERROR: {e}")
                print("[FPGA] Falling back to simulation mode")
                self.overlay = None
        else:
            self.overlay = None
            self._sim_state = {"alert_code": 0, "phase": 0b10}

    def write_sensors(self, pir, mic_avg, mic_peak, gas, vib):
        """Pack and write sensor values to FPGA input registers."""
        if PYNQ_MODE and self.overlay:
            # Pack reg0: [11:0]=pir  [23:12]=mic_avg  [24]=vib
            reg0 = (pir & 0xFFF) | ((mic_avg & 0xFFF) << 12) | ((vib & 1) << 24)
            # Pack reg1: [11:0]=mic_peak  [23:12]=gas
            reg1 = (mic_peak & 0xFFF) | ((gas & 0xFFF) << 12)
            self.s_reg0.write(0, reg0)
            self.s_reg1.write(0, reg1)
            # Pulse valid
            self.s_valid.write(0, 1)
            time.sleep(0.001)
            self.s_valid.write(0, 0)

    def service_watchdog(self):
        """Pulse watchdog kick signal."""
        if PYNQ_MODE and self.overlay:
            self.s_wdog.write(0, 1)
            time.sleep(0.0001)
            self.s_wdog.write(0, 0)

    def read_results(self, raw_sensors=None):
        """Read processed output from FPGA result registers."""
        if PYNQ_MODE and self.overlay:
            r0 = self.r_reg0.read(0)
            r1 = self.r_reg1.read(0)

            crowd_percent = r0 & 0xFF
            noise_db      = (r0 >> 8)  & 0xFF
            aqi           = (r0 >> 16) & 0xFF
            alert_code    = (r0 >> 29) & 0x7

            phase_bits    = r1 & 0x3
            flag_siren    = bool((r1 >> 2) & 1)
            flag_crowd    = bool((r1 >> 3) & 1)
            flag_pollution= bool((r1 >> 4) & 1)
            flag_accident = bool((r1 >> 5) & 1)
            wdog_fired    = bool((r1 >> 6) & 1)

        else:
            # â”€â”€ Simulation mode â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if raw_sensors:
                pir, mic_avg, mic_peak, gas, vib = raw_sensors
                flag_siren     = mic_avg   > 2400
                flag_accident  = mic_peak  > 3300 or vib
                flag_crowd     = pir       == 1
                flag_pollution = gas       > 1800
                wdog_fired     = False

                if flag_accident:
                    alert_code, phase_bits = 4, 0b00
                elif flag_siren:
                    alert_code, phase_bits = 3, 0b00
                elif flag_pollution:
                    alert_code, phase_bits = 2, 0b01
                elif flag_crowd:
                    alert_code, phase_bits = 1, 0b01
                else:
                    alert_code, phase_bits = 0, 0b10

                crowd_percent = 100 if pir else 0
                noise_db      = int(40 + (mic_avg / 4095) * 60)
                aqi           = int((gas / 4095) * 200)
            else:
                alert_code, phase_bits = 0, 0b10
                crowd_percent, noise_db, aqi = 0, 40, 0
                flag_siren = flag_accident = flag_crowd = flag_pollution = False
                wdog_fired = False

        return {
            "alert_code":      alert_code,
            "alert":           ALERT_CODES.get(alert_code, "UNKNOWN"),
            "signal_phase":    PHASE_MAP.get(phase_bits, "RED"),
            "crowd_percent":   crowd_percent,
            "noise_db":        noise_db,
            "aqi":             aqi,
            "flag_siren":      flag_siren    if PYNQ_MODE else locals().get('flag_siren', False),
            "flag_accident":   flag_accident if PYNQ_MODE else locals().get('flag_accident', False),
            "flag_crowd":      flag_crowd    if PYNQ_MODE else locals().get('flag_crowd', False),
            "flag_pollution":  flag_pollution if PYNQ_MODE else locals().get('flag_pollution', False),
            "watchdog_fired":  wdog_fired    if PYNQ_MODE else False,
            "fpga_processed":  True
        }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# UART Reader â€” reads ESP32 CSV stream
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
class UARTReader:
    def __init__(self):
        self.ser         = None
        self.last_values = (0, 0, 0, 0, 0)  # pir, mic_avg, mic_peak, gas, vib
        self.warmup_done = False
        self.last_rx     = time.time()
        self.last_warn   = 0
        self._connect()

    def _connect(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        try:
            ser = serial.Serial()
            ser.port = UART_PORT
            ser.baudrate = UART_BAUD
            ser.timeout = 1
            ser.bytesize = serial.EIGHTBITS
            ser.parity = serial.PARITY_NONE
            ser.stopbits = serial.STOPBITS_ONE
            ser.rtscts = False
            ser.dsrdtr = False
            ser.xonxoff = False
            ser.dtr = False
            ser.rts = False
            ser.open()
            self.ser = ser
            self.last_rx = time.time()
            print(f"[UART] Connected to {UART_PORT} at {UART_BAUD} baud")
        except (serial.SerialException, OSError, TimeoutError) as e:
            print(f"[UART] Cannot open {UART_PORT}: {e}")
            print("[UART] Will retry; check USB cable/port if this repeats")
            self.ser = None

    def reconnect_if_stale(self):
        if not self.ser:
            now = time.time()
            if now - self.last_warn >= UART_STALE_SECONDS:
                print(f"[UART] Not connected; retrying {UART_PORT}")
                self.last_warn = now
                self._connect()
            return
        now = time.time()
        if now - self.last_rx >= UART_STALE_SECONDS:
            if now - self.last_warn >= UART_STALE_SECONDS:
                print(f"[UART] No DATA for {int(now - self.last_rx)}s; reconnecting {UART_PORT}")
                self.last_warn = now
            self._connect()

    def read_line(self):
        """
        Read one CSV line from ESP32.
        Returns tuple: (pir, mic_avg, mic_peak, gas, vib) or None
        """
        if not self.ser:
            # Simulation: generate realistic data
            import random, math
            t = time.time()
            pir      = 1 if (t % 20) > 10 else 0
            mic_avg  = int(800  + 300  * abs(math.sin(t * 0.3)))
            mic_peak = mic_avg + int(200 * random.random())
            gas      = int(1000 + 500  * abs(math.sin(t * 0.1)))
            vib      = 1 if random.random() < 0.03 else 0
            # Simulate occasional siren
            if 25 < (t % 60) < 30:
                mic_avg = 3600; mic_peak = 3700
            self.last_values = (pir, mic_avg, mic_peak, gas, vib)
            return self.last_values

        try:
            raw = self.ser.readline().decode('utf-8', errors='replace').strip()
            if not raw:
                return None

            parts = raw.split(',')

            # Handle warmup packet
            if parts[0] == 'WARMUP':
                elapsed = int(parts[1]) if len(parts) > 1 else 0
                total   = int(parts[2]) if len(parts) > 2 else 120
                print(f"[UART] ESP32 warming up: {elapsed}/{total}s")
                return None

            # Handle data packet: DATA,PIR,MIC_AVG,MIC_PEAK,GAS,VIB
            if parts[0] == 'DATA' and len(parts) == 6:
                pir      = int(parts[1])
                mic_avg  = int(parts[2])
                mic_peak = int(parts[3])
                gas      = int(parts[4])
                vib      = int(parts[5])
                self.last_values = (pir, mic_avg, mic_peak, gas, vib)
                self.warmup_done = True
                self.last_rx = time.time()
                return self.last_values

        except (ValueError, UnicodeDecodeError) as e:
            print(f"[UART] Parse error: {e} â€” raw: {repr(raw)}")
        except serial.SerialException as e:
            print(f"[UART] Serial error: {e}")
            self.ser = None

        return None


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# MQTT Publisher
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
class MQTTPublisher:
    def __init__(self):
        self.client    = mqtt.Client(client_id=f"pynq_{LOCATION_ID}")
        self.connected = False
        self.last_reconnect_attempt = 0
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = (rc == 0)
        print(f"[MQTT] {'Connected' if rc==0 else 'FAILED rc='+str(rc)}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        print(f"[MQTT] Disconnected (rc={rc})")

    def connect(self):
        try:
            self.client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            self.client.loop_start()
            time.sleep(1)
        except Exception as e:
            print(f"[MQTT] Connection error: {e} â€” will retry")

    def publish(self, payload: dict):
        if not self.connected:
            now = time.time()
            if now - self.last_reconnect_attempt < MQTT_RECONNECT_INTERVAL:
                print("[MQTT] Not connected; skipping publish until reconnect")
                return
            self.last_reconnect_attempt = now
            try:
                self.client.reconnect()
                time.sleep(0.5)
            except Exception as e:
                print(f"[MQTT] Reconnect failed: {e}")
                return

        msg = json.dumps(payload)
        try:
            result = self.client.publish(MQTT_TOPIC, msg, qos=0)
            ok = (result.rc == mqtt.MQTT_ERR_SUCCESS)
            status = "âœ“" if ok else "âœ—"
            print(f"[MQTT] {status} {payload['alert']:20s} | "
                  f"crowd={payload['crowd_percent']:3d}% | "
                  f"noise={payload['noise_db']:3d}dB | "
                  f"aqi={payload['aqi']:3d} | "
                  f"phase={payload['signal_phase']}")
        except Exception as e:
            print(f"[MQTT] Publish error: {e}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Watchdog Service Thread
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def watchdog_thread(fpga: FPGAInterface, stop_event: threading.Event):
    """Kicks FPGA watchdog every WDOG_INTERVAL seconds."""
    while not stop_event.is_set():
        fpga.service_watchdog()
        time.sleep(WDOG_INTERVAL)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Main
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def main():
    print("=" * 60)
    print("  TPSMC Edge Bridge â€” PYNQ-Z2")
    print("  ESP32 â†’ UART â†’ FPGA PL â†’ MQTT â†’ Server")
    print("=" * 60)

    fpga     = FPGAInterface()
    uart     = UARTReader()
    mqtt_pub = MQTTPublisher()
    mqtt_pub.connect()

    stop_event  = threading.Event()
    wdog_thread = threading.Thread(
        target=watchdog_thread, args=(fpga, stop_event), daemon=True
    )
    wdog_thread.start()

    last_publish  = 0
    last_sensors  = (0, 0, 0, 0, 0)

    print(f"[MAIN] Location : {LOCATION_ID}")
    print(f"[MAIN] Server   : {MQTT_BROKER}:{MQTT_PORT}/{MQTT_TOPIC}")
    print(f"[MAIN] Publish  : every {PUBLISH_INTERVAL}s")
    print(f"[MAIN] Watchdog : every {WDOG_INTERVAL}s")
    print("[MAIN] Running... (Ctrl+C to stop)\n")

    try:
        while True:
            try:
                # 1. Read from ESP32
                sensors = uart.read_line()
                uart.reconnect_if_stale()
                if sensors:
                    last_sensors = sensors
                    pir, mic_avg, mic_peak, gas, vib = sensors
                    # 2. Write to FPGA
                    fpga.write_sensors(pir, mic_avg, mic_peak, gas, vib)
                    time.sleep(0.01)  # Let FPGA logic settle (1 clock = 8ns, 10ms is plenty)

                # 3. Publish at interval
                now = time.time()
                if now - last_publish >= PUBLISH_INTERVAL:
                    # 4. Read FPGA results
                    results = fpga.read_results(raw_sensors=last_sensors)

                    # 5. Build full payload
                    pir, mic_avg, mic_peak, gas, vib = last_sensors
                    payload = {
                        "timestamp":     datetime.now(timezone.utc).isoformat(),
                        "location":      LOCATION_ID,
                        "lat":           12.9716,   # â† Set your actual GPS coords
                        "lng":           77.5946,
                        "pir_raw":       pir,
                        "mic_avg_raw":   mic_avg,
                        "mic_peak_raw":  mic_peak,
                        "gas_raw":       gas,
                        "vib_raw":       vib,
                        **results
                    }

                    # 6. Publish MQTT
                    mqtt_pub.publish(payload)
                    last_publish = now

                time.sleep(0.1)
            except Exception as e:
                print(f"[MAIN] Loop error: {type(e).__name__}: {e}; continuing")
                time.sleep(1)

    except KeyboardInterrupt:
        print("\n[MAIN] Stopping...")
        stop_event.set()
        mqtt_pub.client.loop_stop()
        mqtt_pub.client.disconnect()
        print("[MAIN] Done.")


if __name__ == "__main__":
    main()
