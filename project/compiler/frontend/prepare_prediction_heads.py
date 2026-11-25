"""Extend the bundled parsed models with hardware prediction layers.

Run once when regenerating assets; board-side inference only needs NumPy.
YOLO reuses the exact signed weights/scales in its QAT ONNX export. ResNet
recovers the existing VAI FC's power-of-two integer grid without weight loss.
The original host-head assets are retained for numerical comparisons.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
YOLO = REPO / "project/model/yolov5n_coco50k_qat"
RESNET = REPO / "project/model/resnet18"
HEAD_VERSION = 1


def conv_entry(name: str, cin: int, cout: int, scale: float) -> dict:
    return {
        "name": name, "type": "conv", "in_channels": cin, "out_channels": cout,
        "kernel_h": 1, "kernel_w": 1, "stride": [1, 1], "padding": [0, 0, 0, 0],
        "group": 1, "weight_shape": [cout, 1, 1, cin], "has_bn": False,
        "has_activation": False, "input_act_scale": scale,
        "in_act_zero_point": 0.0,
    }


def prepare_yolo(mode: str) -> dict:
    import onnx

    spec = importlib.util.spec_from_file_location("yolo_export", YOLO / "parse_quant_export.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    source = YOLO / mode / "best.quant.onnx"
    model = onnx.load(str(source))
    consts, specs = mod._const_map(model), mod._tensor_specs(model)
    parsed = YOLO / f"parsed_{mode}"
    net = json.loads((parsed / "network.json").read_text())
    net["layers"] = [x for x in net["layers"] if not x["name"].startswith("detect.")]
    by_name = {x["name"]: x for x in net["layers"]}
    heads = []
    for i, feeder in enumerate((17, 20, 23)):
        name = f"detect.{i}"
        qspec = specs.get(f"model.24.m.{i}.conv") or specs[f"model.24.m.{i}"]
        weights = consts[qspec["weight"]]
        dtype = np.int16 if mode == "int16" else np.int8
        if weights.dtype != dtype or np.any(consts[qspec["zero_point"]] != 0):
            raise ValueError(f"{source}: expected symmetric {mode} detect weights")
        # Existing head files use OIHW; the compiler consumes OHWI.
        scale = np.broadcast_to(consts[qspec["scale"]].reshape(-1), (weights.shape[0],))
        act_scale = float(by_name[f"model.{feeder}.cv3.conv"]["act_scale"])
        with np.load(parsed / "weights" / f"model_24_m_{i}.npz") as old:
            if not np.array_equal(weights, old["weight_int8"]):
                raise ValueError("QAT head weights differ from the bundled model")
            bias = old["bias"].astype(np.float32)
        valid_c, cin = weights.shape[:2]
        padded_c = (valid_c + 15) // 16 * 16
        pad = padded_c - valid_c
        weight = np.pad(weights.transpose(0, 2, 3, 1), ((0, pad), (0, 0), (0, 0), (0, 0)))
        np.savez_compressed(
            parsed / "weights" / f"detect_{i}.npz", weight_int8=weight,
            weight_scale=np.pad(scale, (0, pad)).astype(np.float32),
            weight_zero_point=np.array(0, np.int32),
            act_scale=np.float32(act_scale),
            dqa_scale=np.pad(scale * np.float32(act_scale), (0, pad)).astype(np.float32),
            dqa_bias=np.pad(bias, (0, pad)),
        )
        net["layers"].append(conv_entry(name, cin, padded_c, act_scale))
        heads.append({"layer": name, "input": f"PAN_P{i + 3}", "output": f"DETECT_P{i + 3}", "valid_c": valid_c})
    net["prediction_head"] = {"version": HEAD_VERSION, "kind": "yolo_detect", "heads": heads}
    (parsed / "network.json").write_text(json.dumps(net, indent=2) + "\n")
    return {"model": f"yolo_{mode}", "weight_source": str(source.relative_to(REPO)), "weight_requantization": False}


def prepare_resnet(parsed: Path) -> dict:
    net = json.loads((parsed / "network.json").read_text())
    net["layers"] = [x for x in net["layers"] if x["name"] != "fc.fpga"]
    with np.load(parsed / "weights/fc.npz") as z:
        weight, bias = z["weight"].astype(np.float32).T, z["bias"].astype(np.float32)
    # VAI's bundled FC is already quantized at 2^-8. Recover it exactly.
    weight_scale = np.float32(2.0 ** -8)
    q = np.rint(weight / weight_scale)
    if q.min() < -128 or q.max() > 127 or not np.array_equal(q * weight_scale, weight):
        raise ValueError("FC is not the bundled VAI W8 power-of-two grid; recalibration required")
    dtype = np.int16 if net.get("int16_from_int8") or "widened" in parsed.name else np.int8
    valid_c, cin = weight.shape
    padded_c = (valid_c + 15) // 16 * 16
    pad = padded_c - valid_c
    scales_path = parsed / "add_output_scales.json"
    if not scales_path.exists():
        scales_path = parsed.parent / "parsed_vai/add_output_scales.json"
    act_scale = float(json.loads(scales_path.read_text())["layer4.1.Add"])
    np.savez_compressed(
        parsed / "weights/fc_fpga.npz",
        weight_int8=np.pad(q.astype(dtype), ((0, pad), (0, 0)))[:, None, None, :],
        weight_scale=np.pad(np.full(valid_c, weight_scale, np.float32), (0, pad)),
        weight_zero_point=np.array(0, np.int32), act_scale=np.float32(act_scale),
        dqa_scale=np.pad(np.full(valid_c, act_scale * weight_scale, np.float32), (0, pad)),
        dqa_bias=np.pad(bias, (0, pad)),
    )
    net["layers"].append(conv_entry("fc.fpga", cin, padded_c, act_scale))
    net["prediction_head"] = {
        "version": HEAD_VERSION, "kind": "resnet_gap_fc", "layer": "fc.fpga",
        "valid_c": valid_c, "input_act_scale": act_scale,
        "gap_quantization": "round(sum_fp32 / (H*W*input_act_scale))",
        "weight_scale": float(weight_scale),
    }
    (parsed / "network.json").write_text(json.dumps(net, indent=2) + "\n")
    return {"model": parsed.name, "fc_weight_max_abs_error": 0.0, "fc_weight_scale": float(weight_scale), "gap_input_scale": act_scale}


def main() -> None:
    report = [prepare_yolo(mode) for mode in ("int8", "int16")]
    report += [prepare_resnet(RESNET / folder) for folder in ("parsed_vai", "parsed_vai_int16_widened")]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
