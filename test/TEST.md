# FPGA 测试

从仓库根目录运行。Network、Precision 和 TOPS 共用
[`bitstream/top.bit`](../bitstream/top.bit) 和 [`bitstream/top.ltx`](../bitstream/top.ltx)。
位流包含 `precision_ila`（8 个探针）和 `peak_tops_ila`（19 个探针）。
脚本不会自动烧录 FPGA；已经烧录该 Combined 镜像时，无需在三项测试间切换位流。

## 环境和常用命令

板端为连接 VCU128 JTAG 与 XDMA 的 Windows 主机，需要 XDMA 驱动、Python、NumPy
及 Vivado Lab / Vivado。运行采样前关闭占用 JTAG 的在线 Hardware Manager 会话。
同一时刻只运行一个上板测试。

```powershell
python -m pip install -r requirements-fpga-runtime.txt

python .\test\network\run.py --model yolo --precision int8 --num 1
python .\test\precision\run.py
python .\test\tops\run.py
```

Precision 与 TOPS 默认加载 `bitstream/top.ltx`，不需要额外的 `--ltx` 参数。
三项测试都会打印 `result dir: <结果目录的绝对路径>`，可复制到资源管理器地址栏。
Precision/TOPS 在保存报告、打印目录后打开原生 Vivado 波形；打开波形失败不会删除已保存的结果。

## 输出约定

```text
test/
  TEST.md
  network/
    output/<模型>_<精度>_<时间戳>/
      result_<图片编号>.png
      result_<图片编号>.json
    cached/                       编译缓存、golden 缓存及中间数据
  precision/
    output/<时间戳>/
      int8.ila                    默认测试 INT8、INT16
      int16.ila
      result.json
    cached/prepared/              --prepare-only 生成的输入向量
  tops/
    output/<时间戳>/
      peak_int8.ila
      result.json
    cached/prepared/              --prepare-only 生成的输入向量
  utils/ila/
    ila_runtime.py                Vivado 启动、LTX 解析及波形视图
    ila_common.tcl                Hardware Manager、JTAG 和主机 Python 调用
    results.py                    输出目录及回读结果汇总
    view.tcl                      原生 ILA 查看入口
```

时间戳格式为 `YYYYMMDD_HHMMSS_ffffff`。每次普通运行新建一个目录，历史运行保留。
Precision/TOPS 可用 `--run-dir <目录>` 指定输出目录；目录存在时，替换本测试的结果文件，
并清除本测试上一轮的 ILA，避免把旧波形当成本轮数据。不会清空整个目录或删除无关文件。
例如重复执行下列命令会覆盖 `latest` 中的结果：

```powershell
python .\test\precision\run.py --run-dir .\test\precision\output\latest
python .\test\tops\run.py --run-dir .\test\tops\output\latest
```

新产生的采样目录中，除 ILA 外只保留一个 `result.json`。输入向量、原始回读、CSV 和内部报告
先在临时目录完成生成、采样和校验；需要保留的证据汇总到 JSON 后删除临时文件。
采样或校验失败也会写入 `result.json`，保留已经获得的回读数据、波形和错误信息；
缺失数据记为 `incomplete`，不会作为一致通过。

### Precision/TOPS 的 result.json

| 字段 | 含义 |
| --- | --- |
| `task`、`generated_at`、`run_dir` | 测试类型、运行时间及绝对目录 |
| `status`、`pass` | 总体结论；完整采样和校验通过才标记 PASS，TOPS 还要求达到阈值 |
| `result` | `same` / `different` / `incomplete`；TOPS 达标情况另见 `status` 和 `meets_2tops` |
| `probes_file` | 本轮加载的 LTX 路径 |
| `vivado_log`、`vivado_returncode` | Vivado 采样日志和进程退出码 |
| `error` | 失败原因（失败时） |

Precision 在 `cases.int8`、`cases.int16` 或 `cases.int16-high` 内分别保存：

- `inputs`：精度、随机种子、激活、权重、有效通道数和预期值。
- `host`：XDMA 回读的标量值、十六进制值及完整主机校验摘要。
- `comparison`：完整 FPGA 回读和 golden 的 128-bit 字列表、长度及不一致位置。
- `ila_file`、`ila_check`：ILA 文件名、ILA 写回值、周期和逐级校验结论。
- `samples_csv`：本轮原始采样 CSV 文本；不再单独输出 CSV 文件。

在同一个 `result.json` 的 `cases.int8` 或 `cases.int16` 中，`comparison.channel0`
并列显示 `fpga_signed` 与 `golden_signed`。核对完整回读时，按相同下标比较
`comparison.fpga_words_hex` 与 `comparison.golden_words_hex`。

TOPS 在顶层保存 `host_results`、`host_correctness_pass`、`comparison`、`ila_file`、
`samples_csv`、`ila_capture`，以及矩阵尺寸、操作数、时钟、周期数和 TOPS。
`comparison.fpga_words_hex` 与 `comparison.golden_words_hex` 按相同顺序逐项对应，
均为完整的 128-bit 十六进制字（不是只保存第一个结果或通过数量）。
字节序为 little-endian，字顺序与 `expected.hex` 一致：像素/任务、Tile、Tile 内结果字。
`comparison.match` 为完整长度和全部数值都一致；读取失败时为 `null`，同时记录原因。

## Network

```powershell
python .\test\network\run.py --model yolo --precision int8 --num 1
python .\test\network\run.py --model yolo --precision int16 --num 1
python .\test\network\run.py --model resnet --precision int8 --num 1
python .\test\network\run.py --model resnet --precision int16 --num 1
python .\test\network\run.py --model all --precision both --num 20
```

| 参数 | 可选值与默认值 |
| --- | --- |
| `--model` | `yolo` / `resnet` / `all`，默认 `all` |
| `--precision` | `int8` / `int16` / `both`，默认 `both` |
| `--num` | 每组 1–20 张，默认 1；`all + both + 20` 共 80 个用例 |
| `--conf` | YOLO 检测阈值，默认 0.25 |

一次运行中，每个模型和精度组合共用一个输出目录；`--num 20` 在该目录保存20对
`result_<图片编号>.png` 和 `result_<图片编号>.json`，不再逐图创建子目录。
YOLO 的图片编号使用 COCO 原图 ID（例如 `result_139.png/json`），ResNet 使用图片清单中的序号，均不补前导零。
单图与批量运行都会打印完整输出目录；选择 `all` 或 `both` 时，不同模型和精度分别保存。
JSON 保存预测结果、置信度/得分、检测框，以及 FPGA 回读和软件参考输出的完整特征值。
人工核对时，在同一个 `result_<图片编号>.json` 的 `comparison.outputs` 中选择输出名称，
按相同下标比较 `fpga_values` 与 `golden_values`。两个数组按 HWC 顺序展开，通道维变化最快。
每组同时记录张量形状、FPGA 原始数据类型、最大绝对误差和是否通过，`comparison.atol` 记录比对阈值。
YOLO 的框使用原图像素坐标 `xyxy`；ResNet 的得分是 logit，不是概率。

编译缓存缺失时自动编译。主机参考值在 FPGA 执行后计算并校验。
Detect head / NMS 以及 ResNet GAP / FC / Top-k 在主机执行。
20+20 张图片用于功能和数值一致性检查，不等同完整 COCO mAP / ImageNet Top-1 测评。

## Precision

```powershell
python .\test\precision\run.py
python .\test\precision\run.py --case int8
python .\test\precision\run.py --case int16
python .\test\precision\run.py --case int16-high
python .\test\precision\run.py --mode int8 --seed 0
python .\test\precision\run.py --mode int16 --seed 0
python .\test\precision\run.py --view-only "<已有采样目录>"
```

默认 `--case both`，依次执行 INT8 和 INT16，保存到同一个时间戳目录。
单项只保存对应 ILA；高位测试的文件名为 `int16-high.ila`。
`--view-only` 只打开已有波形，不再次运行 FPGA；新格式报告可自动识别保存了哪些精度。

| 固定测例 | 运算 | 预期写回 |
| --- | --- | --- |
| `int8` | −93 × 109 | −10,137，`0xFFFFD867` |
| `int16` | 27,341 × −18,530 | −506,628,730，`0xFFFFFFFFE1CD7586` |
| `int16-high` | 4 × (−32,768 × −32,768) | 4,294,967,296，`0x0000000100000000` |

不传 `--seed` 时使用表中固定向量。`--seed` 接受非负整数，INT8/INT16 可生成可复现的
非零操作数；随机用例保持输入通道 0 和输出通道 0 对应权重非零，其余为零。
相同精度、相同种子产生相同向量；`int16-high` 固定，不接受种子。

Precision 在 Tile 0 采样，默认 250 MHz、深度 1024、触发位置 32，
触发 `prec_stage_valid[0] == 1`。脚本逐拍校验原有 7 个探针总线：
有效阶段、激活分片、权重分片、部分积、列和、相位累加和最终写回。
新增 probe7 `prec_phase_sum[47:0]` 包含在 ILA 中，当前校验器未单独比较该探针。
主机完整回读、golden 和 ILA 校验都通过，才将该用例记为 PASS。

## TOPS

```powershell
python .\test\tops\run.py
python .\test\tops\run.py --view-only "<已有采样目录>"
python .\test\tops\run.py --view-only "<peak_int8.ila 的完整路径>"
```

默认 INT8、seed=123、`M=64, K=64, N=128`，一次 MAC 按两次操作计：
`2 × 64 × 64 × 128 = 1,048,576 OP`。
脚本先启动 ILA，再通过 XDMA 发一轮 64-job 任务，校验全部 2,048 个 128-bit 结果字，
并核对 8 个 Tile 的输入/MA/写回有效掩码及 512 个通道 0 的 ILA 结果。

自动采样取第一个满足条件的连续 16 批输出窗口。8 个 Tile 的输入和乘加在窗口内每拍均有效，
全 Tile 写回掩码每两拍为 0xFF。Tcl 将首个写回脉冲标为 `OUT16_START`，第 17 个脉冲
标为 `OUT16_END`（右端不计入），标记随 `peak_int8.ila` 保存。
用 `--view-only` 打开旧版 ILA 时，若归档中含完整探针和采样 CSV，查看器会为该视图补上标记。
终端的 `16-batch output window` 一行显示起止样点、操作数、周期数、耗时和区间算力。
16 批对应 2 × 16 × 64 × 128 = 262,144 次操作。若两标记相差 32 拍，250 MHz 下历时 128 ns，区间算力为 2.048 TOPS。
`result.json` 中的 `calculated_peak_tops` 沿用整轮口径，用整轮操作数 `operations`、时钟 `frequency_mhz` 和首个至末个有效输入拍（含首尾）的周期数 `ila_active_cycles` 计算。
`result: same` 表示数据校验一致，`status: PASS` 还要求峰值不低于 2 TOPS。
仅主机比对通过不足以形成自动 ILA 验证的 TOPS 结论。

### 公共采样参数

Precision/TOPS 支持 `--vivado <启动器路径>`、`--hw-server <地址>`（默认 `localhost:3121`）、
`--hw-target <目标名称>`、`--timeout-s <秒>`（默认 60）、`--quiet-xdma`、
`--ltx <配套探针路径>` 和 `--run-dir <结果目录>`。
只有路径在别处或连接配置不同时才需要额外指定。
TOPS 的 `--frequency-mhz` 默认 250；更改该值只改变计算参数，不会改变 FPGA 实际时钟。

### 离线向量与高级 TOPS 模式

```powershell
python .\test\precision\run.py --case int8 --prepare-only
python .\test\precision\run.py --case int16 --prepare-only
python .\test\tops\run.py --prepare-only
```

`--prepare-only` 不连接板卡，生成指令、输入和 golden 到各测试的 `cached/prepared/<时间戳>/`；
可用 `--run-dir` 显式指定向量目录。此模式是向量准备，不产生 ILA 或上板通过结论。
TOPS 的 `--regenerate` 强制重建向量。

TOPS 还保留 `--repeat-count`、`--staging preload|hbm`（默认 preload）、`--measure-power`
及串口功耗参数。重复轮次大于 1 或功耗模式走主机测试分支，默认不自动采 ILA；
输入向量在临时目录，回读、golden 和功耗数据汇总到时间戳输出目录的 `result.json`。
这类报告可为 `PASS_HOST_PENDING_ILA`，不能当作完整采样 PASS。
`--report-only --run-dir <已有目录> --ila-active-cycles <人工确认的周期数>`
用于补录人工观察的周期；JSON 中的周期来源会标记为人工输入，已有自动采样报告禁止覆盖周期。
所有高级参数以 `python .\test\tops\run.py --help` 为准。

## 离线检查与证据范围

```powershell
python -m unittest discover -s test/utils/ila/tests -v
python -m unittest discover -s test/network/scripts/tests -v
python project/compiler/test_int16_contract.py
python project/compiler/test_resnet_native_int16_frontend.py
python project/compiler/check_release_repro.py
```

2026-11-23 用户反馈 Combined 上板测试通过，随后统一发布为 `top.bit/top.ltx`。
输出目录与文档重构的离线检查不替代新脚本的上板采样；每次实际测试以该次 `result.json`
和原生 ILA 为准。旧文档中其他镜像的计时和 80/80 记录不能直接当成本版复测结果。
