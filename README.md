# EdgeYOLO-FPGA

HBM 近存 AI 处理器的 FPGA 验证工程。顶层只保留源码、测试，以及烧录文件。

```text
bitstream/            Network / TOPS / Precision 共用的 Combined 烧录文件与 ILA 探针
project/              RTL、编译器、运行时、模型、Vivado 脚本
test/network/         网络执行与量化一致性
test/tops/            峰值算力
test/precision/       INT8 / INT16 计算精度
test/utils/ila/       公共 ILA 采样与报告工具
```

| 测试 | FPGA 烧录文件 | ILA 探针文件 |
| --- | --- | --- |
| Network / TOPS / Precision | [top.bit](bitstream/top.bit) | [top.ltx](bitstream/top.ltx) |

当前测试命令、产物目录和判定标准见 [`test/TEST.md`](test/TEST.md)。每组测试的产物写在自己的 `output/` 里。

Python 依赖：

```powershell
python -m pip install -r requirements-fpga-runtime.txt
```

综合入口在 `project/`：

```powershell
cd project
make synth
```
