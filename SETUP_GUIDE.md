# TPSMC — Complete Step-by-Step Setup Guide
## Hardware Wiring + Software Installation + Run Instructions

---

# PART 1 — HARDWARE WIRING

## Step 1.1 — Wire PIR Sensor (HC-SR501) to ESP32

```
HC-SR501 Pin    →    ESP32 Pin
─────────────────────────────
VCC             →    5V  (VVIN pin)
GND             →    GND
OUT             →    GPIO 34
```
- Set PIR sensitivity potentiometer to MIDDLE position
- Set delay potentiometer to MINIMUM (fully counter-clockwise)
- The sensor needs 30 seconds to stabilise after power on

## Step 1.2 — Wire Microphone (MAX9814) to ESP32

```
MAX9814 Pin     →    ESP32 Pin
─────────────────────────────
VDD             →    3.3V
GND             →    GND
OUT             →    GPIO 35  (ADC1_CH7)
GAIN            →    Leave unconnected (default 40dB gain)
AR              →    Leave unconnected
```

## Step 1.3 — Wire Gas Sensor (MQ-135) to ESP32

```
MQ-135 Pin      →    ESP32 Pin
─────────────────────────────
VCC             →    5V  (VVIN)
GND             →    GND
AOUT            →    GPIO 32  (ADC1_CH4)  ← use analog output pin
DOUT            →    Not connected
```
IMPORTANT: MQ-135 needs 5V for heater. Use voltage divider on AOUT if your module
outputs 5V logic:
  AOUT → 10kΩ → GPIO32
              → 10kΩ → GND
Most modules have onboard 3.3V compatible AOUT — check your module datasheet.

## Step 1.4 — Wire Vibration Sensor (SW-420) to ESP32

```
SW-420 Pin      →    ESP32 Pin
─────────────────────────────
VCC             →    3.3V
GND             →    GND
DO              →    GPIO 33
```

## Step 1.5 — Wire Signal LEDs to ESP32 (Demo Output)

```
LED Color    Resistor    ESP32 Pin
─────────────────────────────────
RED LED      220Ω        GPIO 25
AMBER LED    220Ω        GPIO 26
GREEN LED    220Ω        GPIO 27
```
LED long leg (anode) → resistor → ESP32 GPIO
LED short leg (cathode) → GND

Add these LED outputs to the ESP32 firmware:
  In setup(): pinMode(25,OUTPUT); pinMode(26,OUTPUT); pinMode(27,OUTPUT);
  In loop(): Set HIGH/LOW based on signal phase from PYNQ UART response

## Step 1.6 — Wire ESP32 UART to PYNQ-Z2

```
ESP32 Pin       →    PYNQ-Z2 Pin
─────────────────────────────────
GPIO 17 (TX2)   →    PMODA JA4 (UART RX = /dev/ttyPS1 RX)
GPIO 16 (RX2)   →    PMODA JA3 (UART TX = /dev/ttyPS1 TX)
GND             →    PMODA GND
```
IMPORTANT: Both boards must share common GND.
PYNQ-Z2 UART is 3.3V — ESP32 TX is 3.3V — compatible directly.

## Step 1.7 — Connect PYNQ-Z2 to Network

```
PYNQ-Z2 Ethernet port → Router/Switch → Same network as server laptop
```
OR use USB WiFi dongle on PYNQ-Z2 if no Ethernet available.

## Complete Wiring Diagram (Text)

```
                    ┌─────────────────────────────────┐
HC-SR501 OUT ──────►│ GPIO34                          │
MAX9814  OUT ──────►│ GPIO35  ESP32 DevKit V1         │
MQ-135   OUT ──────►│ GPIO32                          │
SW-420   DO  ──────►│ GPIO33                          │
                    │                                 │
RED LED ◄───220Ω───│ GPIO25                          │
AMB LED ◄───220Ω───│ GPIO26                          │
GRN LED ◄───220Ω───│ GPIO27                          │
                    │                                 │
                    │ GPIO17(TX2) ──────────────────► PMODA JA4 (PYNQ RX)
                    │ GPIO16(RX2) ◄────────────────── PMODA JA3 (PYNQ TX)
                    └─────────────────────────────────┘

                    ┌─────────────────────────────────┐
PMODA JA4 RX ◄─────│ PYNQ-Z2                         │
PMODA JA3 TX ──────►│                                 │──── Ethernet ──► Router
                    │ FPGA PL: Edge Processor Verilog │
                    │ ARM PS:  tpsmc_bridge.py         │
                    └─────────────────────────────────┘

Router ──── WiFi/Cable ──── Server Laptop
                            (Flask + MQTT + InfluxDB + Grafana)
```

---

# PART 2 — SOFTWARE SETUP

## Step 2.1 — Server Laptop Setup

### Install Python dependencies
```bash
pip install flask paho-mqtt influxdb-client
```

### Install Mosquitto MQTT Broker
```bash
# Ubuntu / WSL
sudo apt update
sudo apt install mosquitto mosquitto-clients -y
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# Verify broker is running
mosquitto_pub -t "test" -m "hello"
mosquitto_sub -t "test"   # Should print "hello"
```

### Install InfluxDB 2.x
```bash
# Ubuntu
wget -q https://repos.influxdata.com/influxdata-archive_compat.key
echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c \
influxdata-archive_compat.key' | sha256sum -c
cat influxdata-archive_compat.key | gpg --dearmor | \
  sudo tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null
echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] \
  https://repos.influxdata.com/debian stable main' | \
  sudo tee /etc/apt/sources.list.d/influxdata.list
sudo apt update && sudo apt install influxdb2 -y
sudo systemctl start influxdb
sudo systemctl enable influxdb
```

### Setup InfluxDB (first time only)
```bash
# Open browser: http://localhost:8086
# Click "Get Started"
# Username:  admin
# Password:  admin1234   (remember this)
# Org name:  traffic_org
# Bucket:    traffic_data
# Click Continue

# Get your API token:
# InfluxDB UI → Data → API Tokens → Generate All Access Token
# Copy the token — you will need it in app.py
```

### Set InfluxDB token in Flask app
```bash
# Edit server/app.py line:
# INFLUX_TOKEN = "your-token-here"
# Replace with your copied token
```

### Install and Setup Grafana
```bash
sudo apt install -y apt-transport-https software-properties-common
wget -q -O - https://packages.grafana.com/gpg.key | sudo apt-key add -
echo "deb https://packages.grafana.com/oss/deb stable main" | \
  sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt update && sudo apt install grafana -y
sudo systemctl start grafana-server
sudo systemctl enable grafana-server
# Open: http://localhost:3000  admin/admin
# Add InfluxDB datasource, import grafana/dashboard.json
```

### Find your server laptop IP address
```bash
ip addr show   # Linux
ipconfig       # Windows
# Note the IP — e.g. 192.168.1.100
# Update MQTT_BROKER in tpsmc_bridge.py with this IP
```

---

## Step 2.2 — ESP32 Firmware Upload

### Install Arduino IDE and ESP32 Support
```
1. Download Arduino IDE 2.x from arduino.cc
2. Open Arduino IDE
3. Go to File → Preferences
4. In "Additional boards manager URLs" add:
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
5. Go to Tools → Board → Boards Manager
6. Search "esp32" → Install "esp32 by Espressif Systems"
```

### Upload Firmware
```
1. Open esp32/tpsmc_sensor.ino in Arduino IDE
2. Tools → Board → ESP32 Arduino → "ESP32 Dev Module"
3. Tools → Port → Select your COM port (check Device Manager on Windows)
4. Tools → Upload Speed → 115200
5. Click Upload (→ button)
6. Open Serial Monitor (Tools → Serial Monitor, set 115200 baud)
7. You should see:
   DATA,0,823,1045,1122,0
   DATA,1,856,1200,1088,0
   (PIR=0/1, mic_avg, mic_peak, gas, vib=0/1)
```

### Verify ESP32 Sensor Readings
```
Normal readings (no events):
  PIR:      0 (no person) or 1 (person detected)
  MIC_AVG:  500–1200 (quiet room ambient)
  MIC_PEAK: 800–1500 (quiet room)
  GAS:      800–1500 (clean air, after warmup)
  VIB:      0 (no vibration)

Triggered readings:
  PIR:      1 (walk in front of sensor)
  MIC_AVG:  3500+ (play siren sound near mic)
  MIC_PEAK: 3800+ (clap/tap near mic)
  GAS:      2500+ (breathe heavily on sensor or lighter)
  VIB:      1 (tap breadboard sharply)
```

---

## Step 2.3 — PYNQ-Z2 Setup

### Boot PYNQ-Z2
```
1. Download PYNQ-Z2 image from pynq.io (PYNQ v3.0 for Zynq)
2. Flash to microSD card using Balena Etcher
3. Insert SD card, connect Ethernet, connect USB (for power or use 12V adapter)
4. Wait 2 minutes for boot
5. Open browser: http://192.168.2.99
   (default PYNQ IP on direct connection)
   Username: xilinx  Password: xilinx
6. Open Jupyter Notebook — you should see the PYNQ file browser
```

### Install paho-mqtt and pyserial on PYNQ
```bash
# In PYNQ Jupyter terminal or SSH:
ssh xilinx@192.168.2.99   # password: xilinx
pip install paho-mqtt pyserial
```

### Copy bridge script to PYNQ
```bash
# From your laptop:
scp pynq_bridge/tpsmc_bridge.py xilinx@192.168.2.99:/home/xilinx/
```

### Edit bridge configuration
```bash
# On PYNQ via SSH:
nano /home/xilinx/tpsmc_bridge.py

# Change these lines:
MQTT_BROKER = "192.168.1.100"   # ← Your server laptop IP
LOCATION_ID = "junction_A"      # ← Your junction name
UART_PORT   = "/dev/ttyPS1"     # ← Verify UART port exists: ls /dev/tty*
```

---

## Step 2.4 — FPGA Bitstream (Vivado)

### Create Vivado Project
```
1. Open Vivado 2023.1
2. Create New Project → RTL Project
3. Target: xc7z020clg400-1 (PYNQ-Z2 part)
4. Add sources: fpga/tpsmc_edge_processor.v
5. Add simulation: fpga/tb_tpsmc_edge_processor.v
```

### Run Simulation First
```
1. Flow Navigator → Run Simulation → Run Behavioral Simulation
2. Vivado opens waveform viewer
3. In TCL console type: run 500us
4. Verify waveforms show correct alert_code and phase for each scenario
5. Take screenshots — use in presentation
```

### Create Block Design (for AXI GPIO)
```
1. Flow Navigator → IP Integrator → Create Block Design
2. Add IP: Zynq7 Processing System
3. Add IP: AXI GPIO (×2)
   - GPIO 0: 32-bit input  (sensor registers from PS to PL)
   - GPIO 1: 32-bit output (result registers from PL to PS)
4. Add your custom IP (tpsmc_edge_processor) via Add Module
5. Connect clocks, resets, and AXI interfaces
6. Run Connection Automation
7. Note the base addresses from Address Editor
8. Update tpsmc_bridge.py with these addresses
```

### Generate Bitstream
```
1. Flow Navigator → Generate Bitstream
2. Wait 5–15 minutes
3. File → Export → Export Bitstream
4. Save as: tpsmc_edge.bit
5. Copy to PYNQ: scp tpsmc_edge.bit xilinx@192.168.2.99:/home/xilinx/
```

---

# PART 3 — RUNNING THE SYSTEM

## Step 3.1 — Start Server (on laptop)

Open 3 terminal windows:

### Terminal 1 — Start Mosquitto
```bash
mosquitto -v
# Should show: Opening ipv4 listen socket on port 1883
```

### Terminal 2 — Start Flask Server
```bash
cd server/
python app.py
# Should show:
# [MQTT] Connected and subscribed to traffic/edge
# [SERVER] TPSMC Server starting on http://0.0.0.0:5000
```

### Terminal 3 — Verify InfluxDB
```bash
curl http://localhost:8086/ping
# Should return: 204 No Content = OK
```

### Open Dashboard
```
Browser → http://localhost:5000
You should see the dark map dashboard
```

---

## Step 3.2 — Start PYNQ Bridge

```bash
# SSH into PYNQ
ssh xilinx@<pynq-ip>

# Run bridge (without FPGA bitstream first — simulation mode)
python3 /home/xilinx/tpsmc_bridge.py

# You should see:
# [BOOT] PYNQ library found — hardware mode  (or simulation mode)
# [UART] Connected to /dev/ttyPS1 at 115200 baud
# [MQTT] Connected
# [MQTT] ✓ CLEAR                | crowd=  0% | noise= 52dB | aqi=  0 | phase=GREEN
```

---

## Step 3.3 — Verify Full Pipeline

Watch for these in sequence:
```
ESP32 Serial Monitor:  DATA,0,823,1045,1122,0   ← sensors reading
PYNQ terminal:         [MQTT] ✓ CLEAR ...        ← bridge publishing
Flask terminal:        [MQTT→DB] junction_A...   ← server receiving
Browser dashboard:     Map marker appears        ← map updating
                       Gauges show live values   ← sidebar updating
                       Charts showing data       ← history plotting
```

---

## Step 3.4 — Test All Alert Scenarios

### Test 1 — Crowd Detection
```
Action: Walk slowly in front of PIR sensor
Expected:
  ESP32: DATA,1,xxx,xxx,xxx,0
  PYNQ:  CROWD_WARNING
  Dashboard: Marker turns blue, signal phase → AMBER
  Feed: "CROWD WARNING" appears in alert feed
```

### Test 2 — Emergency Siren
```
Action: Play ambulance siren from phone, hold near microphone
Expected:
  ESP32: DATA,x,3600,3700,xxx,0
  PYNQ:  EMERGENCY_SIREN
  Dashboard: Marker turns RED, signal phase → RED
  Feed: "EMERGENCY SIREN" appears highlighted red
```

### Test 3 — Pollution Alert
```
Action: Breathe slowly and heavily directly on MQ-135 sensor
Expected:
  ESP32: DATA,0,xxx,xxx,2600,0
  PYNQ:  POLLUTION_ALERT
  Dashboard: Marker turns amber, AQI gauge rises
  Feed: "POLLUTION ALERT" appears
```

### Test 4 — Accident Detection
```
Action: Sharply tap the breadboard or surface with sensor
Expected:
  ESP32: DATA,0,xxx,3900,xxx,1
  PYNQ:  ACCIDENT_CRITICAL
  Dashboard: Marker turns purple (highest priority), signal → RED
  Feed: "ACCIDENT CRITICAL" appears at top
```

### Test 5 — Simultaneous (Siren + Accident)
```
Action: Play siren + tap breadboard simultaneously
Expected: ACCIDENT_CRITICAL wins (higher priority)
  Dashboard: Purple marker, RED phase
```

### Test 6 — Watchdog
```
Action: Ctrl+C the tpsmc_bridge.py script on PYNQ
Expected: After 200ms, all signal LEDs go RED
  Dashboard: Junction shows as OFFLINE, marker dims
```

---

## Step 3.5 — Grafana Dashboard Setup

```
1. Browser → http://localhost:3000 (admin/admin)
2. Left menu → Connections → Data Sources → Add InfluxDB
3. Settings:
   URL:            http://localhost:8086
   Query Language: Flux
   Org:            traffic_org
   Token:          (paste your InfluxDB token)
   Bucket:         traffic_data
4. Click Save & Test → should say "datasource is working"
5. Left menu → Dashboards → Import
6. Upload grafana/dashboard.json
7. Dashboard appears with crowd, noise, AQI charts and alert log
```

---

# PART 4 — TROUBLESHOOTING

## ESP32 not sending data
```bash
# Check port: ls /dev/ttyUSB* or ls /dev/ttyACM*
# Check baud: both Serial Monitor and PYNQ must be 115200
# Check power: MQ-135 needs 5V — verify VVIN pin not 3.3V
# GPIO 34,35 are INPUT ONLY on ESP32 — correct, they are ADC pins
```

## PYNQ cannot read UART
```bash
ls /dev/tty*
# Try /dev/ttyPS0 or /dev/ttyPS1 or /dev/ttyACM0
# Verify: minicom -b 115200 -D /dev/ttyPS1  (install minicom if needed)
```

## MQTT messages not arriving at server
```bash
# Check broker is running:
systemctl status mosquitto
# Check from PYNQ side:
mosquitto_pub -h <server-ip> -t traffic/edge -m '{"test":1}'
# Check firewall:
sudo ufw allow 1883
```

## Dashboard map not showing junction
```bash
# Check /api/map endpoint:
curl http://localhost:5000/api/map
# Verify lat/lng in JUNCTION_COORDS match your location
# Check junction name matches LOCATION_ID in tpsmc_bridge.py
```

## InfluxDB write failing
```bash
# Verify token is correct
# Check org and bucket names match exactly
# Test write manually:
curl -XPOST 'http://localhost:8086/api/v2/write?org=traffic_org&bucket=traffic_data' \
  -H 'Authorization: Token YOUR_TOKEN' \
  --data-raw 'test_measure field1=1.0'
```

## MQ-135 giving random values
```
- Must warm up minimum 2 minutes (ideally 24 hours for calibration)
- Keep away from heat sources
- Voltage divider required if module output exceeds 3.3V
- Increase IIR filter shift in Verilog from 3 to 5 for more smoothing
```

---

# QUICK REFERENCE — Startup Sequence

Every time you set up at the hackathon venue:

```
Step 1:  Power on ESP32 (USB) — wait 30s for PIR warmup
Step 2:  Power on PYNQ-Z2 — wait 2 min for Linux boot
Step 3:  Start Mosquitto on server laptop
Step 4:  Start Flask server (python app.py)
Step 5:  SSH into PYNQ, run tpsmc_bridge.py
Step 6:  Open http://localhost:5000 in browser
Step 7:  Verify data flowing — check alert feed shows readings
Step 8:  Do one test of each sensor — confirm dashboard responds
Step 9:  You are live — call the judges over
```

Total setup time from scratch: ~10 minutes
