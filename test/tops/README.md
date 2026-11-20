# FPGA 实机 INT8 峰值算力

在接有 VCU128 的 Windows 主机，用 Vivado Lab 烧录
`bitstream/top_network_tops.bit` 并加载 `bitstream/top_network_tops.ltx`。这对文件也用于 Network 测试。
确认 XDMA 正常，再关闭占用 JTAG 的在线 Hardware Manager 会话。从仓库根目录运行：

```powershell
python test/tops/run.py
```

脚本启动 ILA、通过 XDMA 注入一轮 64-job INT8 测试、比对全部结果，再打开 Vivado
Lab 原生波形。每轮在 `test/tops/output/fpga_peak_int8/` 下生成独立的时间戳目录，
保存 `peak_int8.ila`、`peak_int8.csv`、`fpga_peak_report.json` 和 `vivado.log`。
用 `--view-only <采样目录或 peak_int8.ila>` 重开原生波形。自定义 Vivado 路径用
`--vivado`，其他兼容的 TOPS 镜像用 `--ltx` 指定与已烧录 BIT 配对的探针文件。

## 波形和验收

| 波形行 | 截图时看哪里 |
| --- | --- |
| `peak_compute_mask` | `FF` 的 8 位对应 8 个 Tile 都接受了当前相位。首个 `FF` 到下一个非 `FF` 应跨 128 拍。 |
| `peak_job`、`peak_phase` | 从 job 0 / phase 0 到 job 63 / phase 1。每个 job 有两个 INT8 相位。掩码有效时读数。 |
| `peak_dcim_input` | Tile 0 前 8 个输入通道在当拍的 4-bit 分片，脚本逐拍与输入向量对比。 |
| `peak_result_valid`、`peak_result_data` | Tile 0 写出每个 job 的首拍结果。流水线填满后，每 2 拍出现一次写回，且与后续 job 的发射重叠。数据是通道 0 的完整 32-bit INT8 累加结果。 |

截图把两个游标放在首个 `FF` 样本和最后一个 `FF` 样本的后一拍，并保留输出
有效行，方便同时看到吞吐窗口和输入／写回重叠。报告还会核对 64 笔 Tile 0 写回
及 8 个 Tile 全部 `2,048 / 2,048` 个 128-bit 结果字。

本测例的矩阵尺寸为 `M=64, K=64, N=128`。按一次 MAC 计两次操作，运算量为
`2 × 64 × 64 × 128 = 1,048,576 OP`。若同轮采样确认 128 拍且时钟为 250 MHz，
发射窗口为 512 ns，**实测 INT8 峰值为 2.048 TOPS**。PPT 以同一轮的原生波形和
JSON 报告为依据，窗口从首个输入发射计到末个输入发射（含），输出仍继续排空。
