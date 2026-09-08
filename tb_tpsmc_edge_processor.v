// ================================================================
// TPSMC Testbench — All 6 Alert Scenarios
// Run in Vivado Simulator
// Expected: Each scenario shows correct phase + alert_code
// ================================================================
`timescale 1ns / 1ps

module tb_tpsmc_edge_processor;

    // ── DUT ports ────────────────────────────────────────────────
    reg         clk, rst_n;
    reg  [31:0] sensor_reg_0, sensor_reg_1;
    reg         sensor_valid, wdog_kick;

    wire [31:0] result_reg_0, result_reg_1;
    wire        led_red, led_amber, led_green, heartbeat;

    // ── Instantiate DUT ──────────────────────────────────────────
    tpsmc_edge_processor dut (
        .clk(clk), .rst_n(rst_n),
        .sensor_reg_0(sensor_reg_0),
        .sensor_reg_1(sensor_reg_1),
        .sensor_valid(sensor_valid),
        .wdog_kick(wdog_kick),
        .result_reg_0(result_reg_0),
        .result_reg_1(result_reg_1),
        .led_red(led_red),
        .led_amber(led_amber),
        .led_green(led_green),
        .heartbeat(heartbeat)
    );

    // ── 125 MHz clock ────────────────────────────────────────────
    always #4 clk = ~clk;

    // ── Helper: pack sensor registers ────────────────────────────
    task send_sensors;
        input [11:0] pir, mic_avg, mic_peak, gas;
        input        vib;
        begin
            sensor_reg_0 = {7'b0, vib, mic_avg, pir};
            sensor_reg_1 = {8'b0, gas, mic_peak};
            sensor_valid = 1;
            @(posedge clk);
            sensor_valid = 0;
            repeat(200) @(posedge clk);  // Let IIR settle
            wdog_kick = 1; @(posedge clk); wdog_kick = 0;
        end
    endtask

    // ── Helper: decode and display result ────────────────────────
    task show_result;
        input [63:0] scenario_name;
        reg [2:0] code;
        reg [1:0] ph;
        begin
            code = result_reg_0[31:29];
            ph   = result_reg_1[1:0];
            $display("─────────────────────────────────────────");
            $display("Scenario : %s", scenario_name);
            $display("  Alert code  : %0d", code);
            $display("  Signal phase: %02b (%s)",
                ph, (ph==2'b00)?"RED":(ph==2'b01)?"AMBER":"GREEN");
            $display("  Crowd %%     : %0d", result_reg_0[7:0]);
            $display("  Noise dB    : %0d", result_reg_0[15:8]);
            $display("  AQI         : %0d", result_reg_0[23:16]);
            $display("  LED R/A/G   : %b/%b/%b", led_red, led_amber, led_green);
            $display("  flag_siren  : %b  flag_accident: %b",
                result_reg_1[2], result_reg_1[5]);
            $display("  flag_crowd  : %b  flag_pollut  : %b",
                result_reg_1[3], result_reg_1[4]);
        end
    endtask

    // ── Main test sequence ───────────────────────────────────────
    initial begin
        // Initialise
        clk          = 0; rst_n = 0;
        sensor_reg_0 = 0; sensor_reg_1 = 0;
        sensor_valid = 0; wdog_kick    = 0;
        repeat(10) @(posedge clk);
        rst_n = 1;
        repeat(20) @(posedge clk);

        // ── SCENARIO 1: All clear ────────────────────────────────
        // PIR=0, mic low, gas low, vib=0
        repeat(30) send_sensors(12'd0, 12'd800, 12'd1000, 12'd500, 1'b0);
        show_result("CLEAR — all normal");

        // ── SCENARIO 2: Crowd detected ───────────────────────────
        // PIR=1, mic normal, gas normal
        repeat(30) send_sensors(12'd1, 12'd900, 12'd1100, 12'd600, 1'b0);
        show_result("CROWD WARNING");

        // ── SCENARIO 3: Air pollution alert ─────────────────────
        // PIR=0, mic normal, gas HIGH
        repeat(30) send_sensors(12'd0, 12'd700, 12'd900, 12'd3000, 1'b0);
        show_result("POLLUTION ALERT");

        // ── SCENARIO 4: Emergency siren ──────────────────────────
        // PIR=1, mic_avg HIGH (sustained), gas normal
        repeat(30) send_sensors(12'd1, 12'd3600, 12'd3000, 12'd700, 1'b0);
        show_result("EMERGENCY SIREN");

        // ── SCENARIO 5: Accident — vibration spike ───────────────
        // mic_peak very high, vib=1
        repeat(30) send_sensors(12'd0, 12'd2000, 12'd3900, 12'd500, 1'b1);
        show_result("ACCIDENT DETECTED");

        // ── SCENARIO 6: Simultaneous siren + accident ────────────
        // Both — accident should win (higher priority)
        repeat(30) send_sensors(12'd1, 12'd3600, 12'd3900, 12'd500, 1'b1);
        show_result("SIMULTANEOUS — ACCIDENT wins");

        // ── SCENARIO 7: Watchdog test ────────────────────────────
        // Stop kicking the watchdog for >200ms equivalent cycles
        $display("─────────────────────────────────────────");
        $display("Scenario : WATCHDOG TIMEOUT TEST");
        $display("  Not kicking watchdog for 30M cycles...");
        repeat(30_000_000) @(posedge clk);
        $display("  wdog_fired = %b (expect 1)", result_reg_1[6]);
        $display("  LED R/A/G  = %b/%b/%b (expect 1/0/0)",
            led_red, led_amber, led_green);
        // Recover
        wdog_kick = 1; @(posedge clk); wdog_kick = 0;
        repeat(10) @(posedge clk);
        $display("  After kick: wdog_fired = %b (expect 0)", result_reg_1[6]);

        $display("═════════════════════════════════════════");
        $display("All scenarios complete.");
        $finish;
    end

endmodule
