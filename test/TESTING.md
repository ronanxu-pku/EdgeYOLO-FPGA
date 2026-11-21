# 测试说明与实测记录

## 配对烧录文件

- Network 和 TOPS 共用 [top_network_tops.bit](../bitstream/top_network_tops.bit) 和 [top_network_tops.ltx](../bitstream/top_network_tops.ltx)。这对文件来自 `ca47abbe` 的 `top.bit` / `top.ltx`。实测两项时可保持同一份镜像在板上。
- Precision 使用 [top_precision.bit](../bitstream/top_precision.bit) 和 [top_precision.ltx](../bitstream/top_precision.ltx)。

```text
test/network/output/    每张图片的 result.png 和 result.json
test/network/cached/    编译产物、主机参考缓存、中间数据和历史输出
test/network/scripts/   主机推理（host）、校验（golden）、XDMA 工具（xdma）
test/tops/output/       峰值上板向量与报告
test/topsw/output/      能效（尚未接入）
```

一律在仓库根目录执行。同一时刻只跑一路上板进程。

```powershell
python -m pip install -r requirements-fpga-runtime.txt
```

`top_network_tops.bit` 有 84 989 276 字节，SHA256 `93d0ff5f8388a1238d63d189b65adfaa8f34cd73ae5908e70be4d10351757b06`。

---

## 网络 FPGA

- `--model` 选择 `yolo`、`resnet` 或 `all`，默认 `all`。
- `--precision` 选择 `int8`、`int16` 或 `both`，默认 `both`。
- `--num` 指定每个模型和精度组合的图片数，范围 1–20，默认 1。
- `--conf` 指定YOLO检测置信度阈值，默认0.25；`--conf 0.15`可复现原低阈值输出。

单图：

```powershell
python test/network/run.py --model yolo --precision int8 --num 1
python test/network/run.py --model yolo --precision int16 --num 1
python test/network/run.py --model resnet --precision int8 --num 1
python test/network/run.py --model resnet --precision int16 --num 1
```

全部（每组 20 张；YOLO 用 `examples/coco`，ResNet 用 `examples/imagenet`。`all` + `both` 共 80 个用例）：

```powershell
python test/network/run.py --model all --precision both --num 20
python test/network/run.py --model yolo --precision int8 --num 20
python test/network/run.py --model yolo --precision int16 --num 20
python test/network/run.py --model resnet --precision int8 --num 20
python test/network/run.py --model resnet --precision int16 --num 20
```

网络测试复用 `cached/compiled/` 中的编译产物，缓存缺失或不完整时重新编译。FPGA 跑完后在主机计算参考特征，将其存入 `cached/golden_cache/`，并比对 FPGA 回读特征。中间特征、二进制读回和计时文件放在 `cached/intermediate/`，单个用例结束后自动清理。单图终端显示检测或分类结果及比对状态，批量运行还显示计时、各输出路径和通过数量。

单图和批量运行都将每张图片的结果直接写到 `output/<模型>_<精度>_<图片编号>_<执行时间>/`。YOLO 使用 COCO 原图 ID，例如 `000000000139.jpg` 对应 `139`。ResNet 使用图片在清单中的位置，从 1 开始。同一次运行的时间戳相同。

```text
test/network/output/
    yolo_int8_139_20261121_143025_123456/
        result.png
        result.json
    resnet_int8_2_20261121_143025_123456/
        result.png
        result.json
```

YOLO的 `result.json` 保存类别、置信度 `conf`、检测框 `bbox` 和阈值；坐标为原图像素，顺序为 `[x1, y1, x2, y2]`。ResNet保存Top-k类别及原始得分，`score_type`为`logit`，得分不是概率。`result`字段与终端的`result: same/different`一致，表示FPGA输出特征与主机参考结果的数值比对。绘图和JSON使用同一组预测结果。

首次运行会将已有编译产物和主机参考缓存迁入 `cached/`。可识别的旧版推理、验收结果和重复缓存归档到 `cached/legacy/<执行时间>/`。

默认阈值0.25会过滤置信度较低的候选框，也可能漏掉真实目标。以下历史实测的YOLO“5框”对应原阈值0.15。

**单图实测：四套 golden max_abs=0。**

| Workload | 图 | execute | host |
| --- | --- | ---: | --- |
| YOLO INT8 | `000000000139.jpg` | 1.12 s | 5 框 |
| YOLO INT16 | 同上 | 1.33 s | 5 框 |
| ResNet INT8 | `n01443537_goldfish.JPEG` | 0.82 s | Top-1 goldfish |
| ResNet INT16 | 同上 | 1.38 s | Top-1 goldfish |

**全部实测（reset 后单路重跑）：PASS，80/80。** 墙钟约 12.0 min。

| 组 | 张数 | 墙钟 | execute / 图 |
| --- | ---: | ---: | ---: |
| YOLO INT8 | 20 | 227 s | 1.17 s |
| YOLO INT16 | 20 | 231 s | 1.38 s |
| ResNet INT8 | 20 | 103 s | 0.85 s |
| ResNet INT16 | 20 | 149 s | 1.48 s |

不要并行跑两路上板进程，否则会抢 XDMA，H2C 可能卡住。

---

## Compiler 契约

```powershell
python project/compiler/test_int16_contract.py
python project/compiler/test_resnet_native_int16_frontend.py
python project/compiler/check_release_repro.py
```

**实测：全部 PASS。** 四组程序各编译两次，`program.bin` / `weights.bin` / `wb.bin` 字节一致。

| Workload | program words | weights.bin | wb.bin |
| --- | ---: | ---: | ---: |
| YOLO INT8 | 17813 | 1 761 280 | 38928 |
| YOLO INT16 | 32261 | 3 522 560 | 38928 |
| ResNet INT8 | 17977 | 11 169 792 | 93472 |
| ResNet INT16 | 53194（2 段） | 22 339 584 | 93472 |

---

## 计算精度（INT8 / INT16 矩阵乘）

在接板 Windows 主机从仓库根目录运行。脚本自动启动 Vivado 并通过 XDMA 发 INT8、INT16 任务，逐拍校验并导出完整片上波形。

~~~powershell
python test/precision/run.py
~~~

镜像、探针和输出路径见 [精度测试说明](precision/README.md)。

---

## 峰值 TOPS

烧录 `bitstream/top_network_tops.bit` 并加载 `bitstream/top_network_tops.ltx`。
确认 XDMA 正常并关闭占用 JTAG 的在线会话后，在仓库根目录运行：

```powershell
python test/tops/run.py
```

脚本自动 arm ILA、通过 XDMA 发任务并校验回读，再打开 Vivado Lab 原生波形。
采样范围、报告和判定条件见 [峰值测试说明](tops/README.md)。

---

## topsw

占位。产物将写到 `test/topsw/output/`。

---

## 边界

- Detect head / NMS 和 ResNet GAP / FC / Top-k 在 host。
- 20+20 证明功能与数据一致性，不是完整 COCO mAP / ImageNet Top-1。
- 完整 2 TOPS 结论需要 ILA 有效周期，不能用主机比对代替。
