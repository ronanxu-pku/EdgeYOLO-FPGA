# FPGA 实机 INT8 峰值算力

在接有 VCU128 JTAG 和 XDMA 的 Windows 主机，使用联合版的
`bitstream/top.bit` 和同目录的 `top.ltx`。在 Vivado Lab 烧录这对文件一次，
Precision 和 TOPS 都可运行。关闭占用 JTAG 的在线 Hardware Manager 会话后，在本目录执行：

```powershell
python run.py
```

默认测试一轮 `M=64, K=64, N=128` 的 INT8 矩阵乘。脚本先 arm `peak_tops_ila`，通过
XDMA 发任务与回读，逐拍校验采样，然后打开 Vivado Lab 原生波形。输出目录保存
`peak_int8.ila` 和同轮的 `fpga_peak_report.json`。只打开历史波形时，在本目录执行：

```powershell
python run.py --view-only <采样目录或 peak_int8.ila>
```

Vivado Lab 在自定义位置可传 `--vivado <启动器路径>`，probe 文件放在别处可传 `--ltx <top.ltx路径>`。
先确认该文件对应板上已经烧录的 bitstream。

## 波形怎么读

| 阶段 | 信号 | 读法 |
| --- | --- | --- |
| 输入 | `issue_mask`、`issue_phase_index`、`act_ch0_nibble` | 掩码 `FF` 表示 8 个 Tile 同拍接受输入。索引 `0…127` 对应 job 0 相位 0 到 job 63 相位 1。索引最低位是相位。 |
| 中间 | `ma_accept_mask`、`ma_ch0_col_sums` | 掩码为 `FF` 时 8 个 Tile 同拍交付列和。两段 14 bit 分别是 Tile 0 通道 0 的低权重列和高权重列，低列在低 14 bit。 |
| 输出 | `write_done_mask`、`write_job`、`write_ch0` | 掩码为 `FF` 时 8 个 Tile 完成该 job 最后一拍 OBUF 写入。每隔 2 拍应有一次，依次读 job 0 到 63。数据取 Tile 0 通道 0。 |

数据只在同阶段掩码有效时读。首段按各阶段的**第一个、第二个有效拍**对齐 job 0 的两个相位，
因为输入、列和和写回在流水线上同时处理不同的 job。第一笔应看到激活分片 `3 → 9`、
低列和高列 `(-845, 78)` 与 `(3144, −224)`。各相位按 `低列 + 16 × 高列` 合并成
`403` 和 `−440`，写回值为 `403 × 16 − 440 = 6008 = 0x00001778`。
脚本还校验整轮 128 个相位的列和、64 笔 Tile 0 写回，以及主机回读的 2,048 个 128-bit 结果字。

## PPT 怎么标注

主图直接截 Vivado Lab 原生窗口。用游标从 `issue_phase_index=0` 的有效拍标到
`127` **之后一拍**。这个差值应为 128 拍。保留尾部 `write_job=63` 完成的采样，
并在首段放大图标出输入、MA 和写回重叠的位置。图边写单轮报告里的三个实测结果：

- 8/8 Tile，连续 128 拍接受输入，128 个 MA 事件，64 批写完，每批相隔 2 拍。
- XDMA 回读 2,048/2,048 个 128-bit 字与 Golden 一致。
- 按一次 MAC 计两次操作，`64 × 8 × 16 × 64 × 2 = 1,048,576 OP`。报告确认
  128 拍且计算时钟为 250 MHz 时，写 `1,048,576 ÷ (128 × 4 ns) = 2.048 TOPS @ INT8 ≥ 2.000 TOPS`。

以上数字是判定条件。PPT 上的 PASS 来自成功完成的实机运行报告。

## 离线和手动模式

`--prepare-only` 只生成激励。多轮 benchmark、功耗测试和旧版手动 `--report-only`
沿用此前流程。该流程内的手填周期与联合版单轮自动校验是各自的报告。

## 完整 BD 接口检查

启动耗时较长的综合和布局布线前，可先检查 streamed DCIM、ILA、AXI BRAM
以及指令寄存器接口能否在完整 `chip-lite` Block Design 中正确连接：

```bash
BUILD_TAG=dcim_stream_bdcheck vivado -mode batch \
  -source test/tops/fpga/synth/validate_stream_bd.tcl
```

该检查会执行 BD 校验并生成 wrapper，但不会启动各 IP 的 OOC 综合。日志末尾出现
`STREAM_BD_VALIDATE_PASS` 即表示接口集成通过。

## 时序预检与单路线实现

本分支在进入实现前做了三类结构性硬化：

- 乘法、加法树、merge/accumulate 等纯数据寄存器不再承载 Tile 级异步复位；
  valid/状态寄存器仍保持确定复位，功能边界不变；
- 权重 BRAM 输出增加一级寄存，双 bank 选择改为每个 128-bit word 的本地副本，
  避免 BRAM cascade 长路径和单个 8194-load 选择网；
- 16 个 Tile IBUF/OBUF AXI BRAM controller 与三个本地 reset synchronizer 按
  `2+3+3` 固定到对应 SLR，避免 controller、URAM 与复位源跨 SLR。

先运行 8-Tile DCIM OOC 综合；默认只写报告，避免每轮序列化约 300 MB DCP：

```bash
DCIM_OOC_DIR=output/tops/fpga/dcim_stream_ooc_timingopt \
vivado -mode batch -source test/tops/fpga/synth/synth_dcim_stream_ooc.tcl
```

需要交互检查 OOC 网表时才加 `DCIM_OOC_WRITE_DCP=1`。完整设计直接用一个命令从
工程创建和综合开始，一直运行到 routing、bitstream 和 `.ltx`：

```bash
BUILD_TAG=tops_ila_ranked FLOW_MODE=project IMPL_FLOW=two_stage \
IMPL_PLACE_TOP_N=0 IMPL_ROUTE_VARIANTS=4 \
PLACE_THREADS=24 ROUTE_THREADS=16 SYNTH_JOBS=128 \
vivado -mode batch -source scripts/chip-lite/run.tcl
```

`run.tcl` 先完成 OOC/IP 综合、顶层综合和 `opt_design`，随后从本轮自动生成的
`post_opt.dcp` 并行运行 `ExtraTimingOpt`、`Explore`、`Default`，以及
SLR-aware 的 `SSI_SpreadLogic_high` 四个唯一 place，按
WNS、TNS 和失败端点数排序。默认
`IMPL_PLACE_TOP_N=0` 表示取前一半并向上取整，即选 top-2；每个入选 place 展开
4种 phys_opt/route 组合，共8个 routing worker。每个 worker 的 phys_opt、route 和
post-route 修复均受 `ROUTE_THREADS=16` 限制，总并发上限约128 CPU线程。最终按
timing 状态、WNS、WHS 选择获胜项，只对 winner 发布统一的 `post_route.dcp`、
`top.bit` 和 `top.ltx`。设置 `IMPL_FLOW=sequential` 可退回原有顺序 retry；
`IMPL_FLOW=full_race` 保留为不筛选的诊断备选。Project run 已经保存综合 DCP，因此默认跳过重复的
`SynOutputDir/post_synth.dcp` 和完整 post-synth 诊断报告；仅在需要这些中间产物时
设置 `FULL_SYNTH_REPORTS=1`。

各 attempt 默认复用 `post_opt.dcp` 内嵌的 XDC/Pblock，避免重复读取约束。如果
刻意使用旧 DCP 验证新版 `chip_timing.xdc`，设置
`RACE_RELOAD_XDC=1`。

若综合后 implementation 中断，可使用同一 tag 执行
`make resume TAG=tops_ila_ranked FROM=opt`，无需重新综合；也仍支持从单个
`post_place.dcp` 或 `post_phys_opt.dcp` 继续调试。

每次完成后还会生成 `build/lite/<tag>/summary/two_stage_impl_summary.md`：顶部直接
给出推荐 winner、验收裕量和 `.bit/.ltx/.dcp` 链接，随后列出全部 place 和 route
的 WNS/TNS/WHS/THS、失败端点和错误原因；同目录的两个 TSV 便于脚本继续处理。

同一生产脚本已通过 `impl_rank_smoke` 小 RTL 实测：4 个 place 全部成功，top-2
展开4种 route，严格启动8个 route；8个结果均为 `WNS=+1.358 ns`、
`WHS=+0.088 ns`，最终正确发布 winner DCP 和 Markdown 汇总。复现方法见
`test/tops/fpga/synth/impl_rank_smoke/README.md`。

最终8-Tile OOC预检结果保存在
`output/tops/fpga/dcim_stream_ooc_timingopt5/`：0 error、0 critical warning，
`WNS=+1.741 ns @ 250 MHz`，最大普通信号扇出748。与repeat功能基线相比，CLB LUT
减少19.37%、CLB Register减少23.83%，DSP/BRAM/URAM数量不变。

## INT8 / INT16 精度 ILA

VCU128 精度测试也使用 `bitstream/` 中同一次实现生成的 `top.bit` 和 `top.ltx`。

`precision_ila` 以 250 MHz 采样 Tile 0 的 7 组探针。INT8 和 INT16 共用探针，可逐拍观察输入分片、部分积、列和、移位累加和 OBUF 写回。触发条件为 `prec_stage_valid[0] == 1`。

测试向量、采集命令和导出文件见 [计算精度上板](../../precision/README.md)。
