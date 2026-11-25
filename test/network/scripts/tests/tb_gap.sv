`timescale 1ns/1ps
// Exercises the unchanged MP RTL with the real Xilinx fp32_add simulation IP.
module tb_gap;
    reg clk = 0;
    always #5 clk = ~clk;
    reg rst_n = 0, start = 0;
    wire ready;
    reg [31:0] h, w, c;
    wire [15:0] addr;
    wire [127:0] din;
    wire [15:0] web;
    wire en;
    reg [127:0] dout = 0;
    reg valid = 0;
    reg [127:0] memory [0:8191];
    reg [15:0] read_addr [0:2];
    reg [2:0] read_valid = 0;
    integer writes = 0;
    reg all_pass = 0;
    localparam SRC = 32, DST = 7500;

    mp_unit_fixed #(.VB_ADDR_WIDTH(16)) dut (
        .clk(clk), .rst_n(rst_n), .mp_unit_start(start), .mp_unit_ready(ready),
        .mp_src_addr(SRC * 16), .mp_dst_addr(DST * 16),
        .mp_src_h(h), .mp_src_w(w), .mp_src_c(c), .mp_cfg(32'd2),
        .gb_addrb(addr), .gb_dinb(din), .gb_web(web), .gb_enb(en),
        .gb_doutb(dout), .gb_doutb_valid(valid)
    );

    always @(posedge clk) begin
        read_valid <= {read_valid[1:0], en && web == 0};
        read_addr[0] <= addr;
        read_addr[1] <= read_addr[0];
        read_addr[2] <= read_addr[1];
        valid <= read_valid[2];
        if (read_valid[2]) dout <= memory[read_addr[2]];
        if (en && web != 0) begin
            if (web !== 16'hffff || addr < DST || addr >= DST + c/4)
                $fatal(1, "unexpected GAP write: addr=%0d web=%h", addr, web);
            memory[addr] <= din;
            writes <= writes + 1;
        end
    end

    task run_case(input integer height, width, channels, seed);
        integer p, ch, lane, word, old_writes;
        shortreal value, expected, got;
        reg [31:0] bits;
        begin
            @(negedge clk);
            h = height; w = width; c = channels;
            old_writes = writes;
            for (p = 0; p < height*width; p = p + 1)
                for (ch = 0; ch < channels; ch = ch + 1) begin
                    value = ((p + ch*3 + seed) % 31 - 15) * 0.125;
                    memory[SRC + p*(channels/4) + ch/4][(ch%4)*32 +: 32] = $shortrealtobits(value);
                end
            for (word = DST - 1; word <= DST + channels/4; word = word + 1)
                memory[word] = {4{32'hdeadbeef}};
            start = 1;
            @(negedge clk); start = 0;
            wait (!ready);
            wait (ready);
            @(negedge clk);
            if (writes - old_writes != channels/4) $fatal(1, "wrong GAP write count");
            for (ch = 0; ch < channels; ch = ch + 1) begin
                expected = 0;
                for (p = 0; p < height*width; p = p + 1) begin
                    value = ((p + ch*3 + seed) % 31 - 15) * 0.125;
                    expected = expected + value;
                end
                bits = memory[DST + ch/4][(ch%4)*32 +: 32];
                got = $bitstoshortreal(bits);
                if (bits !== $shortrealtobits(expected))
                    $fatal(1, "GAP ch=%0d got=%f expected=%f", ch, got, expected);
            end
            if (memory[DST-1] !== {4{32'hdeadbeef}} || memory[DST+channels/4] !== {4{32'hdeadbeef}})
                $fatal(1, "GAP overwrote output guard");
            $display("GAP PASS: %0dx%0dx%0d, %0d channel sums", height, width, channels, channels);
        end
    endtask

    initial begin
        repeat (8) @(negedge clk);
        rst_n = 1;
        repeat (3) @(negedge clk);
        run_case(7, 7, 512, 0);
        run_case(1, 1, 8, 13);
        run_case(3, 5, 12, 7);
        $display("GAP_RTL_ALL_PASS");
        all_pass = 1;
        $finish;
    end
    initial begin
        #5000000;
        $fatal(1, "GAP timeout");
    end
endmodule
