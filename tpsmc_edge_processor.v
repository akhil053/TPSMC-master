// ================================================================
// TPSMC â€” FPGA Edge Processor
// Platform : PYNQ-Z2 (Xilinx Zynq 7020)
// Language : Verilog
// Function : IIR filter + threshold detection + priority encoder
//            + signal phase FSM + hardware watchdog
// AXI GPIO : Two 32-bit input registers, two 32-bit output registers
// ================================================================

module tpsmc_edge_processor (
    input  wire        clk,       // 125 MHz from PYNQ PL clock
    input  wire        rst_n,     // Active-low reset from PS

    // â”€â”€ Sensor inputs (written by ARM PS via AXI GPIO) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Register 0 (sensor_reg_0):
    //   [11:0]  pir_raw      PIR value  (0=no presence, 4095=max)
    //   [23:12] mic_avg_raw  Mic average 12-bit
    //   [27:24] vib_raw      Vibration flag (bit 24 = 1 means triggered)
    // Register 1 (sensor_reg_1):
    //   [11:0]  mic_peak_raw Mic peak 12-bit
    //   [23:12] gas_raw      Gas sensor 12-bit
    input  wire [31:0] sensor_reg_0,
    input  wire [31:0] sensor_reg_1,
    input  wire        sensor_valid,   // Pulse: new data ready

    // Watchdog service â€” ARM must pulse this every <200ms
    input  wire        wdog_kick,

    // â”€â”€ Processed outputs (read by ARM PS via AXI GPIO) â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Register 0 (result_reg_0):
    //   [7:0]   crowd_percent   0â€“100
    //   [15:8]  noise_db        40â€“100
    //   [23:16] aqi             0â€“500 (scaled)
    //   [31:24] alert_code      0=CLEAR 1=CROWD 2=POLLUTION
    //                           3=SIREN 4=ACCIDENT(CRITICAL)
    // Register 1 (result_reg_1):
    //   [1:0]   signal_phase    00=RED 01=AMBER 10=GREEN
    //   [2]     alert_emergency
    //   [3]     alert_crowd
    //   [4]     alert_pollution
    //   [5]     alert_accident
    //   [6]     watchdog_fired
    output reg [31:0] result_reg_0,
    output reg [31:0] result_reg_1,

    // Direct LED outputs (connect to PYNQ GPIO â†’ physical LEDs)
    output reg        led_red,
    output reg        led_amber,
    output reg        led_green,
    output reg        heartbeat    // Toggles every 0.5s â€” blinks onboard LED
);

// â”€â”€ Parameters â€” Thresholds â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// These are calibrated values â€” adjust after real sensor testing
parameter PIR_CROWD_THRESH    = 12'd1;     // PIR is digital: 1 = presence
parameter MIC_SIREN_THRESH    = 12'd2400;  // calibrated: baseline ~1830, siren/loud sustained above this
parameter MIC_ACCIDENT_THRESH = 12'd3300;  // calibrated: sharp impact/clap peak above this
parameter GAS_POLL_THRESH     = 12'd1800;  // calibrated: baseline ~1350, elevated gas/smoke above this
parameter IIR_SHIFT           = 3;         // IIR smoothing factor (divide by 8)

// â”€â”€ Watchdog parameters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// At 125MHz, 25_000_000 cycles = 200ms
parameter WDOG_LIMIT = 27'd25_000_000;

// â”€â”€ Internal signal extraction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
wire [11:0] pir_raw      = sensor_reg_0[11:0];
wire [11:0] mic_avg_raw  = sensor_reg_0[23:12];
wire        vib_raw      = sensor_reg_0[24];
wire [11:0] mic_peak_raw = sensor_reg_1[11:0];
wire [11:0] gas_raw      = sensor_reg_1[23:12];

// â”€â”€ IIR filter registers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
reg [15:0] mic_avg_accum;
reg [15:0] gas_accum;
reg [11:0] mic_avg_filt;
reg [11:0] gas_filt;

// â”€â”€ Scaled output registers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
reg [7:0]  crowd_percent;
reg [7:0]  noise_db;
reg [7:0]  aqi;

// â”€â”€ Alert flags â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
reg        flag_siren;
reg        flag_accident;
reg        flag_crowd;
reg        flag_pollution;

// â”€â”€ Signal phase â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// 00 = RED   01 = AMBER   10 = GREEN
reg [1:0]  phase;

// â”€â”€ Watchdog â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
reg [26:0] wdog_counter;
reg        wdog_fired;

// â”€â”€ Heartbeat â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
reg [26:0] hb_counter;

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// Heartbeat â€” toggles every 0.5s
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        hb_counter <= 0;
        heartbeat  <= 0;
    end else begin
        if (hb_counter >= 27'd62_500_000) begin
            hb_counter <= 0;
            heartbeat  <= ~heartbeat;
        end else begin
            hb_counter <= hb_counter + 1;
        end
    end
end

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// Watchdog Timer
// ARM must pulse wdog_kick high for one cycle every <200ms
// If it doesn't, wdog_fired goes high â†’ force RED + alert
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        wdog_counter <= 0;
        wdog_fired   <= 0;
    end else if (wdog_kick) begin
        wdog_counter <= 0;
        wdog_fired   <= 0;
    end else begin
        if (wdog_counter >= WDOG_LIMIT) begin
            wdog_fired <= 1;
            // Do not increment further â€” stay fired until kick
        end else begin
            wdog_counter <= wdog_counter + 1;
        end
    end
end

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// IIR Low-Pass Filter
// y[n] = y[n-1] + (x[n] - y[n-1]) >> IIR_SHIFT
// Smooths mic_avg and gas readings
// PIR is digital so no filter needed
// mic_peak is intentionally NOT filtered (we want raw peaks)
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        mic_avg_accum <= 16'd0;
        gas_accum     <= 16'd0;
        mic_avg_filt  <= 12'd0;
        gas_filt      <= 12'd0;
    end else if (sensor_valid) begin
        // Mic average IIR
        mic_avg_accum <= mic_avg_accum
                       + ({4'b0, mic_avg_raw} - mic_avg_accum[15:IIR_SHIFT]);
        mic_avg_filt  <= mic_avg_accum[14:3];

        // Gas IIR
        gas_accum <= gas_accum
                   + ({4'b0, gas_raw} - gas_accum[15:IIR_SHIFT]);
        gas_filt  <= gas_accum[14:3];
    end
end

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// Scale to Human-Readable Units
// crowd_percent = pir_raw (0 or 1) Ã— 100
// noise_db      = 40 + (mic_avg_filt / 4095) Ã— 60   â†’ 40â€“100 dB
// aqi           = (gas_filt / 4095) Ã— 500            â†’ 0â€“500
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        crowd_percent <= 8'd0;
        noise_db      <= 8'd40;
        aqi           <= 8'd0;
    end else begin
        crowd_percent <= pir_raw[0] ? 8'd100 : 8'd0;
        noise_db      <= 8'd40 + ((mic_avg_filt * 60) >> 12);
        aqi           <= (gas_filt * 200) >> 12;  // scaled 0â€“200 for display
    end
end

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// Threshold Comparators
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        flag_siren     <= 0;
        flag_accident  <= 0;
        flag_crowd     <= 0;
        flag_pollution <= 0;
    end else begin
        // Siren: sustained high mic average
        flag_siren     <= (mic_avg_filt  > MIC_SIREN_THRESH);
        // Accident: sharp mic peak OR vibration trigger
        flag_accident  <= (mic_peak_raw  > MIC_ACCIDENT_THRESH) | vib_raw;
        // Crowd: PIR triggered
        flag_crowd     <= (pir_raw[0] == 1'b1);
        // Pollution: gas above threshold
        flag_pollution <= (gas_filt      > GAS_POLL_THRESH);
    end
end

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// Priority Encoder + Signal Phase FSM
// Priority (highest first):
//   4 â€” ACCIDENT  (CRITICAL) â†’ RED
//   3 â€” SIREN     (EMERGENCY)â†’ RED
//   2 â€” POLLUTION             â†’ AMBER
//   1 â€” CROWD                 â†’ AMBER
//   0 â€” CLEAR                 â†’ GREEN
// Watchdog override: always RED if fired
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
reg [2:0] alert_code;

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        phase      <= 2'b10;  // Default GREEN
        alert_code <= 3'd0;
        led_red    <= 0;
        led_amber  <= 0;
        led_green  <= 1;
    end else if (wdog_fired) begin
        // Watchdog override â€” everything RED
        phase      <= 2'b00;
        alert_code <= 3'd5;   // code 5 = watchdog
        led_red    <= 1;
        led_amber  <= 0;
        led_green  <= 0;
    end else if (flag_accident) begin
        phase      <= 2'b00;  // RED
        alert_code <= 3'd4;   // ACCIDENT
        led_red    <= 1;
        led_amber  <= 0;
        led_green  <= 0;
    end else if (flag_siren) begin
        phase      <= 2'b00;  // RED
        alert_code <= 3'd3;   // SIREN
        led_red    <= 1;
        led_amber  <= 0;
        led_green  <= 0;
    end else if (flag_pollution) begin
        phase      <= 2'b01;  // AMBER
        alert_code <= 3'd2;   // POLLUTION
        led_red    <= 0;
        led_amber  <= 1;
        led_green  <= 0;
    end else if (flag_crowd) begin
        phase      <= 2'b01;  // AMBER
        alert_code <= 3'd1;   // CROWD
        led_red    <= 0;
        led_amber  <= 1;
        led_green  <= 0;
    end else begin
        phase      <= 2'b10;  // GREEN
        alert_code <= 3'd0;   // CLEAR
        led_red    <= 0;
        led_amber  <= 0;
        led_green  <= 1;
    end
end

// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
// Output Register Assembly
// â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        result_reg_0 <= 32'd0;
        result_reg_1 <= 32'd0;
    end else begin
        result_reg_0 <= {alert_code[2:0], 5'b0,
                         aqi,
                         noise_db,
                         crowd_percent};

        result_reg_1 <= {25'b0,
                         wdog_fired,
                         flag_accident,
                         flag_pollution,
                         flag_crowd,
                         flag_siren,
                         phase};
    end
end

endmodule

