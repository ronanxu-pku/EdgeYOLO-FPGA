# INT8 / INT16 片上计算精度

在 VCU128 的 Tile 0 只发一个任务，逐拍检查输入、4×4 部分积、列和、移位累加和 OBUF 写回。两种精度共用同一组 225 位探针。

## 实机测试

在接有 VCU128 JTAG 和 XDMA 的 Windows 主机运行。板卡烧录 `bitstream/top_precision.bit` 并加载 `bitstream/top_precision.ltx`。
主机需装有 XDMA 驱动、Python 和 NumPy，以及 Vivado Lab 或 Vivado。
运行前关闭占用 JTAG 的在线 Hardware Manager 会话。

从仓库根目录执行，默认依次测试 INT8 和 INT16。

~~~powershell
python test/precision/run.py
~~~

`run.py` 通过 XDMA 下发任务并校验 ILA 采样。通过后，默认只在 `test/precision/output/<本次运行>/` 保存 `int8.ila` 和 `int16.ila`，并在 Vivado Lab 打开两个原生波形标签。波形按输入、部分积、列和、移位累加、写回排列。保存到 PPT 时直接截取原生波形窗口。

在已有采样上打开相同的原生视图。

~~~powershell
python test/precision/run.py --view-only <本次运行目录>
~~~

只测一项或只看一项可加 `--case int8`、`--case int16` 或 `--case int16-high`。探针文件放在别处时可用 `--ltx <top_precision.ltx 路径>`。Vivado 在自定义路径时传 `--vivado <启动器路径>`。多个 JTAG 目标可用 `--hw-target <目标名称>` 指定。

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

## 波形和 PPT 判定

分别截取 INT8 和 INT16 的原生 Vivado Lab 窗口。以触发游标作为首个有效输入相位，
沿着激活分片、权重分片、部分积、列和、移位累加再到通道 0 写回看。后面的行有流水延迟，
各相位依照该阶段的先后顺序对齐。原始采样的 `prec_stage_valid[4:0]` 从低到高对应输入、
乘积、列和、累加、写回。终端打印的写回周期相对于首个输入相位。

| 截图标注 | 输入分片与累加 | 写回与判定 |
| --- | --- | --- |
| INT8×INT8 | `A → 3`。两相位合并值 `−654、327`，累加到 `−10137`。 | 对比低 32 位 `0xFFFFD867`。 |
| INT16×INT16 | `6 → A → C → D`。累加值依次为 `−111180、−1964180、−31649240、−506628730`。 | 对比全部 64 位 `0xFFFFFFFFE1CD7586`。 |
| INT16 高位用例 | 用 `--case int16-high` 采一次。四个通道各做 `(−32768) × (−32768)` 并求和。 | 完整写回 `0x0000000100000000`，可在图中标出高 32 位为 `1`。 |

每张图在写回处标出 **ILA 实采值、XDMA 回读值、Golden**。三者一致且所有中间阶段逐拍通过，
才能在 PPT 上标记该精度验证 PASS。

## 离线生成激励

从仓库根目录运行。`--prepare-only` 只生成板测输入、指令和参考值。

~~~powershell
python test/precision/run.py --case int8 --prepare-only
python test/precision/run.py --case int16 --prepare-only
~~~
