# INT8 / INT16 片上计算精度

在 VCU128 的 Tile 0 只发一个任务，逐拍检查输入、4×4 部分积、列和、移位累加和 OBUF 写回。两种精度共用同一组 225 位探针。

## 实机测试

在接有 VCU128 JTAG 和 XDMA 的 Windows 主机运行。板卡需已烧录 [bitstream/top.bit](../../bitstream/top.bit)，主机需装有 XDMA 驱动、Python 和 NumPy，以及 Vivado Lab 或 Vivado。

从仓库根目录执行，默认依次测试 INT8 和 INT16。

~~~powershell
python test/precision/run.py
~~~

`run.py` 通过 XDMA 下发任务并校验 ILA 采样。通过后，默认只在 `test/precision/output/<本次运行>/` 保存 `int8.ila` 和 `int16.ila`，并在 Vivado Lab 打开两个原生波形标签。波形按输入、部分积、列和、移位累加、写回排列。保存到 PPT 时直接截取原生波形窗口。

在已有采样上打开相同的原生视图。

~~~powershell
python test/precision/run.py --view-only <本次运行目录>
~~~

只测一项或只看一项可加 `--case int8`、`--case int16` 或 `--case int16-high`。Vivado 在自定义路径时传 `--vivado <启动器路径>`。多个 JTAG 目标可用 `--hw-target <目标名称>` 指定。

## 测例

| 测例 | 输入 | 预期写回 |
| --- | --- | --- |
| `int8` | −93 × 109 | −10,137（`0xFFFFD867`） |
| `int16` | 27,341 × −18,530 | −506,628,730（`0xFFFFFFFFE1CD7586`） |
| `int16-high` | 4 个通道均为 −32,768 × −32,768 | 4,294,967,296（`0x0000000100000000`） |

## 探针

| ILA | 信号 | 位宽 | 采样原因 |
| --- | --- | ---: | --- |
| probe0 | `prec_stage_valid` | 5 | 标记输入、乘积、列和、累加、写回五个有效阶段。 |
| probe1 | `prec_act_slice` | 4 | 核对每拍从高到低送入的激活分片。 |
| probe2 | `prec_weight_slices` | 16 | 核对对应物理列的权重分片。 |
| probe3 | `prec_partial_products` | 32 | 核对第 0 输入通道在各列的 4×4 乘积。 |
| probe4 | `prec_column_sums` | 56 | 核对各列加法树输出。 |
| probe5 | `prec_phase_acc` | 48 | 核对每拍移位加和后的累加值。 |
| probe6 | `prec_result_ch0` | 64 | 核对通道 0 的 OBUF 写回，并和主机回读比较。 |

采样时钟 250 MHz，深度 1024，触发位置 32，触发条件 `prec_stage_valid[0] == 1`。INT8 读取物理列 0–1 和写回低 32 位。INT16 读取物理列 0–3 和完整 64 位。最低位切片对应第 0 物理列。

## 离线生成激励

从仓库根目录运行。`--prepare-only` 只生成板测输入、指令和参考值。

~~~powershell
python test/precision/run.py --case int8 --prepare-only
python test/precision/run.py --case int16 --prepare-only
~~~
