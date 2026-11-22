# TOPS / Precision ILA 补丁

三份补丁均以 `test-top/project` 为基线，每次选择一份应用到新的构建目录。TOPS、Precision 独立版各有一个 ILA 核；Combined 合并版同时包含两个核。合并时使用完整的 `combined/ila.patch`，无需手工叠加两份有重叠的补丁。`prepare.py` 不修改原工程，拒绝覆盖已有输出目录，并校验补丁基线和改动范围。

| 配置 | 补丁 | ILA 核 | 物理探针 |
| --- | --- | --- | --- |
| TOPS | `tops/ila.patch` | `peak_tops_ila` | 19 个，保留原 9 个，增加 8 Tile 的 MA/写回掩码及每个 Tile 独立的通道 0 结果 |
| Precision | `precision/ila.patch` | `precision_ila` | 8 个，包含新增的 `prec_phase_sum[47:0]` |
| Combined | `combined/ila.patch` | `peak_tops_ila` + `precision_ila` | 19 + 8 个，两套探针及顺序与独立版一致 |

原始 `project` 仍带有旧的 6 探针 TOPS 配置。选择上述补丁后，得到对应的独立版或合并版配置。

## 构建

在仓库根目录执行，输出目录必须是新目录，并位于 `project/` 外。

```sh
python project/ila/prepare.py tops --output ../build-tops-ila
python project/ila/prepare.py precision --output ../build-precision-ila
python project/ila/prepare.py combined --output ../build-combined-ila
```

在 eda02 的对应构建目录执行：

```sh
nohup bash build_ila.sh > build.log 2>&1 < /dev/null &
```

默认复用此前 Precision 的 Vivado 2024.2.2、project flow、two-stage 实现策略、32 线程及 4 个路由候选。Vivado 路径可通过 `VIVADO` 环境变量指定。`exit_status` 为退出码；完整结果位于 `build/lite/<profile>_ila_<commit>/ImplOutputDir/`，包括同次生成的 `top.bit`、`top.ltx` 和时序报告。

`ila_manifest.json` 记录源提交、补丁 SHA-256、全部构建源文件 SHA-256，以及 `ila_cores` 中每个核的名称和探针宽度。独立版保留原有 `ila_core` / `probe_widths` 字段。发布时把它与同次构建的 BIT/LTX 一并保存。`source_commit` 是构建来源，不能用它推断旧镜像的来源。现用的历史镜像另行保留，新的构建结果在上板前标记为 `board_validation: pending`。

Combined 的两个 ILA 均连接现有 `xdma_0/axi_aclk`，深度均为 1024，输入流水级均为 1。Precision 观测仅在 Tile 0 启用，TOPS 观测覆盖全部 8 Tile。现有 `test/tops/run.py` 和 `test/precision/run.py` 仍按各自的核名称采样，指定合并版对应的同一份 `.ltx` 即可，不需要重新下载另一份位流。两个用例应依次运行，避免同时通过 XDMA 修改同一组工作负载。

合并版已通过 7 个 Precision RTL 用例、启用 Precision 观测的完整 8 Tile TOPS RTL 用例，以及两个核共 27 个探针的模块级 BD 校验。Precision 功能与探针 CSV、TOPS 全 Tile CSV 均与对应独立版逐字节一致；TOPS 的 512 个通道 0 结果和 2,048 个 OBUF 字全部匹配。完整实现及板端状态另见对应构建记录。

## XDMA 边界

三份补丁仅修改 DCIM 观测输出、ILA 配置和 ILA 连接。XDMA IP 参数、PCIe 时钟/复位、AXI 地址映射、HBM 配置、CDMA/指令译码器和 XDC 约束均保持当前基线内容。准备脚本校验所有未列入补丁的源文件，并拒绝非 ILA 的 BD 连接改动。

此前的部分合并镜像及一份 TOPS 镜像曾在 Windows 出现 XDMA Code 10；具体机制尚未定位。补丁隔离和时序通过都不等于 XDMA 实测通过。新镜像仍需用配对 BIT/LTX 上板，检查 XDMA 正常启动、主机读写及对应的 Precision/TOPS 用例，再记录硬件 PASS。构建脚本不启用强制忽略时序的位流输出。

## TOPS 的 8 Tile 直接证据

原 9 个探针中，`peak_compute_mask[7:0]` 覆盖全部 Tile，其他数据及流水线观测主要来自 Tile 0。新补丁保留 probe0–8，追加：

| 物理探针 | 信号 | 含义 |
| --- | --- | --- |
| probe9 | `peak_ma_fire_mask[7:0]` | 每一位来自对应 Tile 的真实 `ma_valid && ma_ready` |
| probe10 | `peak_result_valid_mask[7:0]` | 每一位来自对应 Tile 的真实 `writer_accept` |
| probe11–18 | `peak_tile0_result_ch0` 至 `peak_tile7_result_ch0` | 各 Tile 独立的 32 位通道 0 写回值；无效拍归零 |

按当前 seed=123、M=64/K=64/N=128 的用例，8 Tile 完整 RTL 仿真得到以下预期位置。第一拍输入对齐到 ILA 样本 32，尚不代表新镜像已完成上板采样。

| 信号 | 预期采样 |
| --- | --- |
| `peak_compute_mask` | 样本 32–159 连续 128 拍为 `FF` |
| `peak_ma_fire_mask` | 样本 42–169 连续 128 拍为 `FF` |
| `peak_result_valid_mask` | 样本 48、50、…、174 为 `FF`，共 64 组写回 |
| Tile 0–7 首组通道 0 结果 | `00001778`、`000004AD`、`FFFFF798`、`000006D3`、`000007DB`、`FFFFFD1D`、`FFFFFD36`、`00000676` |

`test/tops/run.py` 自动识别新探针，显示上述三个掩码、job/phase 和 8 行独立结果。校验器将各 Tile 的 64 个通道 0 结果逐个与 golden 对比（合计 512 个），并继续要求主机回读的 2,048 个 128 位结果字全部匹配。旧的 6/9 探针镜像仍按原方式校验，不会声称已经观测了新探针。

完整 Array 仿真已检查 128 次全 Tile 输入、128 次全 Tile MA 接受、64 次全 Tile 写回、512 个通道 0 结果和 2,048 个 OBUF 字；均通过。采样校验器也已验证能拒绝缺失一个 Tile 的 MA 有效位及单个 Tile 的错误结果。

## Precision INT8 显示

采样时钟 250 MHz，触发 `prec_stage_valid[0] == 1`，深度 1024，触发位置 32。物理探针 0–6 的顺序保持不变，probe7 新增 `prec_phase_sum[47:0]`，有效标志同 `prec_stage_valid[3]`。旧的七总线校验脚本仍可使用。

显示下表的 8 行即可，所有数值用十六进制。该表为 RTL 仿真得到的预期采样位置，并将第一拍输入对齐到样本 32。`—` 表示该阶段无效、探针数据为 0。

| 显示信号 | 32 | 33 | 34 | 35 | 43 | 44 | 48 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `prec_act_slice[3:0]` | A | 3 | — | — | — | — | — |
| `prec_weight_slices[7:0]` | 6D | 6D | — | — | — | — | — |
| `prec_weight_slices[7:4]` | 6 | 6 | — | — | — | — | — |
| `prec_weight_slices[3:0]` | D | D | — | — | — | — | — |
| `prec_partial_products[15:8]` | — | — | DC | 12 | — | — | — |
| `prec_partial_products[7:0]` | — | — | B2 | 27 | — | — | — |
| `prec_phase_sum[15:0]` | — | — | — | — | FD72 | 0147 | — |
| `prec_result_ch0[31:0]` | — | — | — | — | — | — | FFFFD867 |

`A` 是激活的有符号高分片，表示 -6；权重低分片 `D` 表示无符号 13。因此两个高相位部分积为 `DC` 和 `B2`。本相位贡献是 `FD72`、`0147`，最终结果为 `(FD72 << 4) + 0147` 的 32 位补码 `FFFFD867`。

`prec_phase_sum` 是当前相位合并值；`prec_phase_acc` 是累计值，仍为 `FD72 → D867`。最终写回比累计值完成晚 4 拍。无效拍归零仅作用于观测寄存器，计算控制、结果和有效拍位置不变。相邻两拍确实输入相同分片时，波形仍会连续显示相同值，应按采样网格和 valid 判断拍数。

当前补丁已通过 7 个 INT8/INT16 RTL 用例和 1,318 个采样拍的前后对比，包括重复分片、零值和超过 32 位的 INT16 结果。功能轨迹、原有有效采样值及位置一致；新增相位贡献也与独立算术模型一致。Precision 的模块引用与 8 个探针宽度已通过本地 Vivado BD 校验。完整实现及板端结果以对应构建记录为准。
