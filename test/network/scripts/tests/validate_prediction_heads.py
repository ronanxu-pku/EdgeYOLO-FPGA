"""Compare the new integer tails to the previous host heads on bundled images.

This is an offline numerical check, not FPGA readback or dataset accuracy.
Backbone outputs are computed by the existing compiler oracle. Both tails
consume those same outputs, isolating the effect of moving the boundary.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(REPO / "test/network/scripts/golden"), str(REPO / "test/network/scripts/host")]
import compare_one_shot as golden
from detect_head import DetectHead

CASES = (
    ("yolo_int8", "project/model/yolov5n_coco50k_qat/parsed_int8", "int8", "coco"),
    ("yolo_int16", "project/model/yolov5n_coco50k_qat/parsed_int16", "int16", "coco"),
    ("resnet_int8", "project/model/resnet18/parsed_vai", "int8", "imagenet"),
    ("resnet_int16_widened", "project/model/resnet18/parsed_vai_int16_widened", "int16", "imagenet"),
)


def integer_linear(x, weights, scales, bias, mode):
    """Independent matrix reference, with DQA's accumulator-to-FP32 rounding."""
    accumulator = x.reshape(-1, x.shape[-1]).astype(np.int64) @ weights.astype(np.int64).T
    # The hardware rounds INT32/INT64 to FP32 before the FP32 FMA.
    values = accumulator.astype(np.float32).astype(np.float64) * scales.astype(np.float64) + bias.astype(np.float64)
    return values.astype(np.float32).reshape(*x.shape[:-1], weights.shape[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--num", type=int, default=20)
    ap.add_argument("--case", choices=[x[0] for x in CASES])
    ap.add_argument("--out", type=Path, default=REPO / "test/network/cached/head_validation/report.json")
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = {"scope": "offline tail comparison on bundled functional samples; no board run", "cases": {}}
    for label, folder, mode, dataset in CASES:
        if args.case and label != args.case:
            continue
        parsed = REPO / folder
        net = json.loads((parsed / "network.json").read_text())
        examples_dir = REPO / "test/network/examples" / dataset
        examples = json.loads((examples_dir / "manifest.json").read_text())["images"][:args.num]
        rows = []
        for item in examples:
            started = time.perf_counter()
            image = examples_dir / item["file"]
            row = {"image": item["file"]}
            if dataset == "coco":
                _, features = golden.run_yolo_compiler_golden(image, mode, None, str(parsed))
                old_head = DetectHead(str(parsed / "weights"))
                qs, new_raw, old_raw = [], [], []
                for i, head in enumerate(net["prediction_head"]["heads"]):
                    feat = features[head["input"]]
                    with np.load(parsed / "weights" / (head["layer"].replace(".", "_") + ".npz")) as z:
                        q = golden._quantize_fp32(feat, float(z["act_scale"]), mode, reciprocal_multiply=True)
                        qs.append(q)
                        raw = integer_linear(q, z["weight_int8"][:, 0, 0, :], z["dqa_scale"], z["dqa_bias"], mode)
                        if np.any(raw[..., head["valid_c"]:] != 0):
                            raise AssertionError("nonzero padded YOLO output")
                        new_raw.append(raw[..., :head["valid_c"]].reshape(*feat.shape[:2], 3, -1))
                old_raw = old_head.forward(*qs)
                if not rows:
                    _, complete = golden.run_yolo_compiler_golden(image, mode, None, str(parsed), prediction_heads=True)
                    for spec, expected in zip(net["prediction_head"]["heads"], new_raw):
                        got = complete[spec["output"]][..., :spec["valid_c"]].reshape(expected.shape)
                        np.testing.assert_array_equal(got, expected)
                    row["compiler_golden_matches_independent_tail"] = True
                errors = np.concatenate([(new - old).reshape(-1) for new, old in zip(new_raw, old_raw)])
                old_boxes = old_head.postprocess(old_raw, conf_thres=.25)
                new_boxes = old_head.postprocess(new_raw, conf_thres=.25)
                row.update(old_detections=len(old_boxes), new_detections=len(new_boxes),
                           same_ordered_classes=bool(np.array_equal(old_boxes[:, 6], new_boxes[:, 6])))
                if row["same_ordered_classes"] and len(old_boxes):
                    row["box_max_abs_px"] = float(np.max(np.abs(new_boxes[:, :4] - old_boxes[:, :4])))
            else:
                feature = golden.run_resnet_compiler_golden(image, mode, None, str(parsed), quantize_residuals=True)
                with np.load(parsed / "weights/fc.npz") as old:
                    old_logits = feature.mean(axis=(0, 1)) @ old["weight"] + old["bias"]
                flat = feature.reshape(-1, 512)
                sums = np.zeros(512, np.float32)
                for values in flat:
                    sums = (sums + values).astype(np.float32)
                scale = len(flat) * net["prediction_head"]["input_act_scale"]
                q = golden._quantize_fp32(sums[None, None, :], scale, mode, reciprocal_multiply=True)
                with np.load(parsed / "weights/fc_fpga.npz") as z:
                    new_padded = integer_linear(q, z["weight_int8"][:, 0, 0, :], z["dqa_scale"], z["dqa_bias"], mode).reshape(-1)
                if np.any(new_padded[1000:] != 0):
                    raise AssertionError("nonzero padded FC output")
                new_logits = new_padded[:1000]
                if not rows:
                    complete = golden.run_resnet_compiler_golden(image, mode, None, str(parsed),
                                                                 quantize_residuals=True, prediction_heads=True)
                    np.testing.assert_array_equal(complete.reshape(-1), new_padded)
                    row["compiler_golden_matches_independent_tail"] = True
                errors = new_logits - old_logits
                row.update(old_top1=int(np.argmax(old_logits)), new_top1=int(np.argmax(new_logits)), label=int(item["class_id"]))
                row["same_top1"] = row["old_top1"] == row["new_top1"]
                # Keep the small tail inputs for independent host/RTL debugging.
                np.savez_compressed(args.out.parent / f"{label}_{image.stem}.npz",
                                    feature=feature, old_logits=old_logits, new_logits=new_padded)
            row.update(logit_max_abs=float(np.max(np.abs(errors))), logit_rmse=float(np.sqrt(np.mean(errors.astype(np.float64)**2))))
            rows.append(row)
            print(f"{label} {len(rows)}/{len(examples)} {row} ({time.perf_counter() - started:.2f}s)", flush=True)
            report["cases"][label] = {"samples": rows}
            args.out.write_text(json.dumps(report, indent=2) + "\n")
        summary = report["cases"][label]
        summary["max_logit_abs_difference"] = max(x["logit_max_abs"] for x in rows)
        if dataset == "coco":
            summary["same_detection_count_and_classes"] = sum(x["same_ordered_classes"] for x in rows)
        else:
            summary["same_top1"] = sum(x["same_top1"] for x in rows)
            summary["old_matches_sample_label"] = sum(x["old_top1"] == x["label"] for x in rows)
            summary["new_matches_sample_label"] = sum(x["new_top1"] == x["label"] for x in rows)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"report: {args.out}", flush=True)


if __name__ == "__main__":
    main()
