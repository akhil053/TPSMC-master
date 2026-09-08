// ================================================================
// TPSMC — ESP32 Sensor Node Firmware
// Reads: PIR, Microphone (MAX9814), MQ-135, SW-420 Vibration
// Sends: Raw CSV over UART to PYNQ-Z2 every 1 second
// Baud: 115200
// ================================================================

// ── Pin Definitions ─────────────────────────────────────────────
#define PIR_PIN        34    // Digital IN  — HC-SR501 output
#define MIC_PIN        35    // Analog  IN  — MAX9814 analog out
#define GAS_PIN        32    // Analog  IN  — MQ-135 analog out
#define VIB_PIN        33    // Digital IN  — SW-420 output
#define STATUS_LED     2     // Built-in LED — blinks on send
#define RED_LED        25    // Demo signal LED: critical/emergency
#define GREEN_LED      26    // Demo signal LED: clear
#define WHITE_LED      27    // Demo signal LED: warning/crowd/pollution
#define PYNQ_RX_PIN    16    // ESP32 RX2  <- PYNQ TX
#define PYNQ_TX_PIN    17    // ESP32 TX2  -> PYNQ RX

// ── Microphone sampling ─────────────────────────────────────────
#define MIC_SAMPLES    50    // Number of samples to average
#define MIC_PEAK_WIN   20    // Samples for peak detection

// ── Gas sensor warmup ───────────────────────────────────────────
#define GAS_WARMUP_MS  120000  // 2 minute warmup (reduce if short on time)

// ── Vibration debounce ──────────────────────────────────────────
#define VIB_DEBOUNCE   500   // ms — ignore repeated triggers within window

// Local demo thresholds. Keep these aligned with tpsmc_bridge.py / FPGA.
#define MIC_SIREN_THRESH     2400
#define MIC_ACCIDENT_THRESH  3300
#define GAS_POLL_THRESH      1800

// ── Global variables ────────────────────────────────────────────
volatile bool     vibrationTriggered = false;
volatile uint32_t lastVibTime        = 0;
bool              warmupDone         = false;
uint32_t          startTime          = 0;

// Microphone rolling buffer for peak detection
int micBuffer[MIC_PEAK_WIN];
int micBufIdx  = 0;
int micPeakVal = 0;

HardwareSerial PynqSerial(2);

void setSignalLeds(bool redOn, bool greenOn, bool whiteOn) {
  digitalWrite(RED_LED, redOn ? HIGH : LOW);
  digitalWrite(GREEN_LED, greenOn ? HIGH : LOW);
  digitalWrite(WHITE_LED, whiteOn ? HIGH : LOW);
}

void updateSignalFromSensors(int pirVal, int micAvg, int micPeak, int gasVal, int vibVal) {
  bool critical = (vibVal == 1) || (micPeak > MIC_ACCIDENT_THRESH) || (micAvg > MIC_SIREN_THRESH);
  bool warning = (gasVal > GAS_POLL_THRESH) || (pirVal == 1);

  if (critical) {
    setSignalLeds(true, false, false);
  } else if (warning) {
    setSignalLeds(false, false, true);
  } else {
    setSignalLeds(false, true, false);
  }
}

// ── Vibration ISR ───────────────────────────────────────────────
void IRAM_ATTR vibISR() {
  uint32_t now = millis();
  if (now - lastVibTime > VIB_DEBOUNCE) {
    vibrationTriggered = true;
    lastVibTime        = now;
  }
}

// ── Setup ───────────────────────────────────────────────────────
void setup() {
  // USB debug UART
  Serial.begin(115200);
  // UART2 to PYNQ-Z2
  PynqSerial.begin(115200, SERIAL_8N1, PYNQ_RX_PIN, PYNQ_TX_PIN);

  // Pin modes
  pinMode(PIR_PIN,    INPUT);
  pinMode(MIC_PIN,    INPUT);
  pinMode(GAS_PIN,    INPUT);
  pinMode(VIB_PIN,    INPUT_PULLUP);
  pinMode(STATUS_LED, OUTPUT);
  pinMode(RED_LED,    OUTPUT);
  pinMode(GREEN_LED,  OUTPUT);
  pinMode(WHITE_LED,  OUTPUT);
  setSignalLeds(false, true, false);

  // Vibration interrupt — triggers on any change
  attachInterrupt(digitalPinToInterrupt(VIB_PIN), vibISR, FALLING);

  // Init mic buffer
  memset(micBuffer, 0, sizeof(micBuffer));

  startTime = millis();

  // Warmup blink pattern
  for (int i = 0; i < 5; i++) {
    digitalWrite(STATUS_LED, HIGH); delay(100);
    digitalWrite(STATUS_LED, LOW);  delay(100);
  }
}

// ── Read microphone — returns smoothed average and peak ─────────
void readMicrophone(int &avgVal, int &peakVal) {
  long sum = 0;
  int  peak = 0;

  for (int i = 0; i < MIC_SAMPLES; i++) {
    int v = analogRead(MIC_PIN);  // 12-bit: 0–4095
    sum += v;
    if (v > peak) peak = v;
    delayMicroseconds(200);
  }

  avgVal = sum / MIC_SAMPLES;

  // Update rolling peak buffer
  micBuffer[micBufIdx] = peak;
  micBufIdx = (micBufIdx + 1) % MIC_PEAK_WIN;

  // Find max in rolling window
  micPeakVal = 0;
  for (int i = 0; i < MIC_PEAK_WIN; i++) {
    if (micBuffer[i] > micPeakVal) micPeakVal = micBuffer[i];
  }
  peakVal = micPeakVal;
}

// ── Read gas sensor — returns smoothed ADC value ────────────────
int readGas() {
  long sum = 0;
  for (int i = 0; i < 10; i++) {
    sum += analogRead(GAS_PIN);
    delay(5);
  }
  return sum / 10;
}

// ── Main loop ───────────────────────────────────────────────────
void loop() {
  // Check warmup
  if (!warmupDone) {
    uint32_t elapsed = millis() - startTime;
    if (elapsed < GAS_WARMUP_MS) {
      // During warmup, send status but mark gas as warming
      // Send warmup packet so PYNQ knows we are alive
      PynqSerial.print("WARMUP,");
      PynqSerial.print(elapsed / 1000);
      PynqSerial.print(",");
      PynqSerial.println(GAS_WARMUP_MS / 1000);
      Serial.print("WARMUP,");
      Serial.print(elapsed / 1000);
      Serial.print(",");
      Serial.println(GAS_WARMUP_MS / 1000);
      digitalWrite(STATUS_LED, !digitalRead(STATUS_LED));
      setSignalLeds(false, false, true);
      delay(1000);
      return;
    }
    warmupDone = true;
  }

  // ── Read all sensors ────────────────────────────────────────
  // 1. PIR (digital)
  int pirVal = digitalRead(PIR_PIN);  // 0 or 1

  // 2. Microphone (analog — average + peak)
  int micAvg, micPeak;
  readMicrophone(micAvg, micPeak);

  // 3. Gas / Air quality (analog)
  int gasVal = readGas();

  // 4. Vibration (interrupt flag)
  int vibVal = 0;
  if (vibrationTriggered) {
    vibVal             = 1;
    vibrationTriggered = false;  // clear flag after reading
  }

  updateSignalFromSensors(pirVal, micAvg, micPeak, gasVal, vibVal);

  // ── Format and send CSV over UART ───────────────────────────
  // Format: DATA,PIR,MIC_AVG,MIC_PEAK,GAS,VIB
  // All values are integers
  // PIR: 0 or 1
  // MIC_AVG: 0–4095
  // MIC_PEAK: 0–4095
  // GAS: 0–4095
  // VIB: 0 or 1

  PynqSerial.print("DATA,");
  PynqSerial.print(pirVal);
  PynqSerial.print(",");
  PynqSerial.print(micAvg);
  PynqSerial.print(",");
  PynqSerial.print(micPeak);
  PynqSerial.print(",");
  PynqSerial.print(gasVal);
  PynqSerial.print(",");
  PynqSerial.println(vibVal);

  Serial.print("DATA,");
  Serial.print(pirVal);
  Serial.print(",");
  Serial.print(micAvg);
  Serial.print(",");
  Serial.print(micPeak);
  Serial.print(",");
  Serial.print(gasVal);
  Serial.print(",");
  Serial.println(vibVal);

  // Blink status LED to show activity
  digitalWrite(STATUS_LED, HIGH);
  delay(50);
  digitalWrite(STATUS_LED, LOW);

  // Wait before next reading (total loop ~1 second)
  delay(900);
}
