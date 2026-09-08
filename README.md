# 🚦 TPSMC — AI Powered Smart Traffic & Emergency Management System

TPSMC (Traffic Prediction & Smart Management Controller) is an intelligent real-time traffic monitoring and emergency response platform built using:

* **ESP32 sensor nodes**
* **PYNQ-Z2 FPGA edge acceleration**
* **MQTT communication**
* **Flask backend APIs**
* **Mapbox live dashboard**
* **ML-based route intelligence**

The system continuously monitors traffic congestion, accidents, pollution, crowd density, and emergency situations, then performs intelligent decision-making for:

* Smart traffic signal control
* Emergency dispatch workflows
* Hospital routing
* ML-based safest route prediction
* Real-time congestion analytics
* Traffic visualization dashboards

---

# 🧠 System Architecture

```text
ESP32 Sensors
   │
   ▼
UART2 Communication
   │
   ▼
PYNQ-Z2 FPGA Edge Processor
   │
   ├── FPGA Signal Processing
   ├── Alert Classification
   └── Sensor Filtering
   │
   ▼
MQTT Broker (traffic/edge)
   │
   ▼
Flask Backend APIs
   │
   ├── Intelligence Engine
   ├── Emergency Dispatch
   ├── Hospital Routing
   ├── ML Route Prediction
   └── Dashboard APIs
   │
   ▼
Mapbox Live Dashboard
```

---

# 🔧 Hardware Stack

## ESP32 Sensor Node

Connected Sensors:

* PIR Sensor
* MQ-135 Gas Sensor
* Microphone Module
* Vibration Sensor

ESP32 sends packets in format:

```text
DATA,PIR,MIC_AVG,MIC_PEAK,GAS,VIB
```

via UART2 → PYNQ-Z2 FPGA.

---

## FPGA Edge Processing (PYNQ-Z2)

The FPGA layer performs:

* Real-time filtering
* Signal thresholding
* Accident detection
* Noise analysis
* Priority alert generation

Processed outputs are pushed to MQTT for backend intelligence.

---

# 🌐 Backend Features

## 🚑 Emergency Dispatch Workflow

LifeAlert-inspired accident response system:

* Accident countdown timer
* Auto dispatch after timeout
* Manual cancel/dispatch
* Nearest hospital assignment
* Offline SMS fallback support

### APIs

```http
GET  /api/emergency/status
POST /api/emergency/cancel
POST /api/emergency/dispatch
```

---

## 🏥 Smart Hospital Routing

Features:

* Google Places hospital loading
* Capacity-aware hospital assignment
* ETA-based selection
* Trauma-ready prioritization
* 100km dynamic search radius

---

## 📊 Congestion Intelligence Engine

Deterministic congestion scoring using:

* Crowd density
* Signal delay
* Pollution/AQI
* Noise levels
* Historical trends
* Alert severity

Outputs:

* Congestion score
* Congestion level
* Safety score
* Route recommendations

---

## 🤖 ML Route Engine — TPSMC RouteNet v1.0

Custom lightweight ML engine written in pure Python.

### Features

* Safety risk prediction
* Safest path generation
* Shortest path comparison
* Junction risk ranking
* Live dashboard integration

### Algorithms Used

* 2-layer MLP
* Dijkstra shortest path
* Weighted risk scoring
* Haversine graph routing

---

# 🖥 Dashboard Features

## Live Mapbox Dashboard

### Includes:

* Real-time junction monitoring
* Congestion heat visualization
* ML route overlays
* Emergency status panel
* Incident timeline
* Hospital overlays
* Traffic layer integration
* Manual signal override controls

---

# 📡 Communication Stack

| Layer         | Technology   |
| ------------- | ------------ |
| Sensors       | ESP32        |
| Edge Compute  | PYNQ-Z2 FPGA |
| Communication | UART2 + MQTT |
| Backend       | Flask        |
| Frontend      | HTML/CSS/JS  |
| Maps          | Mapbox       |
| ML Engine     | Pure Python  |

---

# ⚙️ Configuration

Environment variables supported:

```env
GOOGLE_MAPS_API_KEY=
EMERGENCY_COUNTDOWN_SECONDS=
EMERGENCY_INTERNET_ONLINE=
TPSMC_DEMO_MODE=
STALE_SENSOR_SECONDS=
HOSPITAL_RADIUS_KM=
HOSPITAL_SEARCH_GRID=
```

---

# 🚀 Key Innovations

* FPGA accelerated edge intelligence
* Real-time emergency dispatch automation
* Deterministic congestion analytics
* ML-based traffic safety routing
* Capacity-aware hospital recommendation
* Smart signal override system
* Fully interactive live dashboard

---

# 📌 Future Improvements

* YOLO-based accident detection
* Live CCTV analytics
* Reinforcement learning traffic optimization
* Vehicle-to-infrastructure communication (V2I)
* Mobile emergency responder app
* Edge AI acceleration using FPGA overlays

---

# 👨‍💻 Authors

Developed as an advanced intelligent traffic management and emergency response research project using embedded systems, FPGA acceleration, edge intelligence, and machine learning.

