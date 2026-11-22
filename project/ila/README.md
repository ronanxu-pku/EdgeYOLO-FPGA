# TOPS / Precision ILA 补丁

两份补丁均以 `test-top/project` 为基线，分别应用到新的构建目录。每个镜像只有一个 ILA 核；不叠加两份补丁。`prepare.py` 不修改原工程，拒绝覆盖已有输出目录，并校验补丁基线和改动范围。

| 配置 | 补丁 | ILA 核 | 物理探针 |
| --- | --- | --- | --- |
| TOPS | `tops/ila.patch` | `peak_tops_ila` | 9 个，包含时钟翻转、8 Tile 发射掩码、job、phase、输入、流水线 fire/stall 和结果 |
| Precision | `precision/ila.patch` | `precision_ila` | 8 个，包含新增的 `prec_phase_sum[47:0]` |

原始 `project` 仍带有旧的 6 探针 TOPS 配置。选择上述补丁后，分别得到这里定义的 TOPS 或 Precision 配置。

## 构建

在仓库根目录执行，输出目录必须是新目录，并位于 `project/` 外。

```sh
python project/ila/prepare.py tops --output ../build-tops-ila
python project/ila/prepare.py precision --output ../build-precision-ila
```

在 eda02 的对应构建目录执行：

```sh
nohup bash build_ila.sh > build.log 2>&1 < /dev/null &
```

默认复用此前 Precision 的 Vivado 2024.2.2、project flow、two-stage 实现策略、32 线程及 4 个路由候选。Vivado 路径可通过 `VIVADO` 环境变量指定。`exit_status` 为退出码；完整结果位于 `build/lite/<profile>_ila_<commit>/ImplOutputDir/`，包括同次生成的 `top.bit`、`top.ltx` 和时序报告。

`ila_manifest.json` 记录源提交、补丁 SHA-256、全部构建源文件 SHA-256 和探针宽度。发布时把它与同次构建的 BIT/LTX 一并保存。`source_commit` 是构建来源，不能用它推断旧镜像的来源。现用的历史镜像另行保留，新的构建结果在上板前标记为 `board_validation: pending`。

## XDMA 边界

两份补丁仅修改 DCIM 观测输出、ILA 配置和 ILA 连接。XDMA IP 参数、PCIe 时钟/复位、AXI 地址映射、HBM 配置、CDMA/指令译码器和 XDC 约束均保持当前基线内容。准备脚本校验所有未列入补丁的源文件，并拒绝非 ILA 的 BD 连接改动。

此前的部分合并镜像及一份 TOPS 镜像曾在 Windows 出现 XDMA Code 10；具体机制尚未定位。补丁隔离和时序通过都不等于 XDMA 实测通过。新镜像仍需用配对 BIT/LTX 上板，检查 XDMA 正常启动、主机读写及对应的 Precision/TOPS 用例，再记录硬件 PASS。构建脚本不启用强制忽略时序的位流输出。

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
