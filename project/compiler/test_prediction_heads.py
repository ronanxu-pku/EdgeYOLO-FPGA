"""Offline contracts for prediction layers, padded lanes, and host boundaries."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "project"), str(REPO / "test/network/scripts/host")]
from compiler import _mini_yaml
from compiler.lowering.lower_full import lower_full, IM2COL
from compiler.packer.wb_packer import pack_all_layers
from compiler.model_contract import validate_model_precision
import one_shot_host_head as host

CASES = (
    ("project/model/yolov5n_coco50k_qat/parsed_int8", "int8"),
    ("project/model/yolov5n_coco50k_qat/parsed_int16", "int16"),
    ("project/model/resnet18/parsed_vai", "int8"),
    ("project/model/resnet18/parsed_vai_int16_widened", "int16"),
)


def load_case(folder, mode, max_layers=None):
    parsed = REPO / folder
    network = json.loads((parsed / "network.json").read_text())
    network["_parsed_dir"] = str(parsed)
    info = network.setdefault("model_info", {})
    info.setdefault("name", network.get("model"))
    info.setdefault("input_shape", network.get("input_shape"))
    info.setdefault("output_shape", network.get("output_shape", []))
    hw = _mini_yaml.load_file(str(REPO / "project/compiler/lowering/hw_caps.yaml"))
    return parsed, network, lower_full(network, hw, mode=mode, max_layers=max_layers)


class PredictionHeadTest(unittest.TestCase):
    def test_padded_weights_and_precision_contract(self):
        for folder, mode in CASES:
            with self.subTest(folder=folder):
                parsed, net, plan = load_case(folder, mode)
                validate_model_precision(net, parsed / "weights", mode=mode,
                                         allow_widened_int16="widened" in folder)
                self.assertFalse(plan["unsupported"])
                tail = net["prediction_head"]
                specs = tail.get("heads", [tail])
                for spec in specs:
                    with np.load(parsed / "weights" / (spec["layer"].replace(".", "_") + ".npz")) as z:
                        for key in ("weight_int8", "dqa_scale", "dqa_bias"):
                            self.assertTrue(np.all(z[key][int(spec["valid_c"]):] == 0), key)
                self.assertEqual(plan["compile_meta"]["num_conv_layers_compiled"], len(net["layers"]))

    def test_yolo_outputs_survive_later_heads(self):
        for folder, mode in CASES[:2]:
            _, _, plan = load_case(folder, mode)
            specs = plan["host_io"]["outputs"]
            self.assertEqual([x["valid_c"] for x in specs], [255] * 3)
            self.assertEqual([x["c"] for x in specs], [256] * 3)
            previous_end = 0
            for spec in specs:
                self.assertGreaterEqual(spec["obuf_off"], previous_end)
                previous_end = spec["obuf_off"] + int(np.prod(spec["hw"])) * spec["c"] * 4
                self.assertLessEqual(previous_end, IM2COL)
            # Every later head writes outside all previously completed outputs.
            for i in (1, 2):
                start = specs[i]["obuf_off"]
                for op in plan["ops"]:
                    if op.get("layer") == f"detect.{i}" and op["kind"] == "vpu_exec":
                        self.assertGreaterEqual(op["args"]["dst_addr"], start)
                        if op["unit"] == "dqa":
                            self.assertFalse(op.get("flags", 0) & 1, "linear head must retain negative logits")

    def test_gap_normalization_is_folded_once_and_bias_is_unchanged(self):
        for folder, mode in CASES[2:]:
            parsed, net, plan = load_case(folder, mode)
            gap_ops = [op for op in plan["ops"] if op.get("unit") == "mp" and op["args"]["addr_break"] == 2]
            self.assertEqual(len(gap_ops), 1)
            self.assertEqual([gap_ops[0]["args"][f"src_{d}"] for d in ("h", "w", "c")], [7, 7, 512])
            blob, layout = pack_all_layers(plan, str(parsed / "weights"))
            offset = layout["fc.fpga"]["offset"]
            self.assertEqual(np.frombuffer(blob, np.float32, count=1, offset=offset)[0], np.float32(1 / (49 * .125)))
            scales = np.frombuffer(blob, np.float32, count=1008, offset=offset + 16)
            biases = np.frombuffer(blob, np.float32, count=1008, offset=offset + 16 + 1008 * 4)
            np.testing.assert_array_equal(scales[:1000], np.full(1000, .125 / 256, np.float32))
            with np.load(parsed / "weights/fc.npz") as z:
                np.testing.assert_array_equal(biases[:1000], z["bias"])
                with np.load(parsed / "weights/fc_fpga.npz") as q:
                    np.testing.assert_array_equal(q["weight_int8"][:1000, 0, 0, :].T / 256, z["weight"])
            self.assertEqual(plan["host_io"]["boundary"], "classification_logits")
            self.assertEqual(plan["host_io"]["valid_c"], 1000)

    def test_partial_backbone_does_not_execute_tail(self):
        for folder, mode in CASES:
            count = 57 if "yolo" in folder else 20
            _, _, plan = load_case(folder, mode, max_layers=count)
            self.assertEqual(plan["host_io"]["boundary"], "backbone_features")
            self.assertFalse(any(op.get("layer", "").startswith(("detect.", "fc.fpga")) for op in plan["ops"]))

    def test_host_resnet_never_runs_fc_or_ranks_padded_channels(self):
        import resnet_e2e
        folder, mode = CASES[2]
        parsed, _, plan = load_case(folder, mode)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logits.bin"
            logits = np.zeros(1008, np.float32)
            logits[:1000] = -np.arange(1, 1001, dtype=np.float32)
            logits.tofile(path)
            with patch.object(resnet_e2e, "load_fc_from_parsed", side_effect=AssertionError("host FC called")):
                result = host._run_resnet(plan, Path("image.jpg"), path, parsed, 5, None)
            self.assertEqual([x["class_id"] for x in result["topk"]], list(range(5)))

    def test_host_yolo_never_runs_detect_convolution(self):
        from detect_head import DetectHead
        parsed, _, plan = load_case(*CASES[0])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            for spec in plan["host_io"]["outputs"]:
                values = np.full((*spec["hw"], spec["c"]), -20, np.float32)
                values[..., spec["valid_c"]:] = 100
                values.tofile(output / f"{spec['name']}.bin")
            with patch.object(DetectHead, "forward", side_effect=AssertionError("host detect Conv called")):
                result = host._run_yolo(plan, REPO / "test/network/examples/coco/000000000139.jpg",
                                        output, parsed, .25, .45, None)
            self.assertEqual(result["num_detections"], 0)


if __name__ == "__main__":
    unittest.main()
