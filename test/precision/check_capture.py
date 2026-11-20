"""Verify all seven FPGA ILA probe buses against the selected precision case."""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ila_runtime import parse_bus

PROBES = (
    "stage_valid", "act_slice", "weight_slices", "partial_products",
    "column_sums", "phase_acc", "result_ch0",
)


def signed(value, width):
    return value - (1 << width) if value & (1 << (width - 1)) else value


def read_stages(path):
    stages = [[] for _ in range(5)]
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = csv.reader(stream)
        for header in rows:
            if any("prec_stage_valid" in name for name in header):
                break
        else:
            raise ValueError("Precision probe names are missing from the Vivado CSV")
        indexes = {}
        for name in PROBES:
            matches = [i for i, field in enumerate(header)
                       if re.search(rf"prec_{name}(?:\[[^]]+\])?$", field.strip())]
            if len(matches) != 1:
                raise ValueError(f"Expected one CSV column for prec_{name}, found {len(matches)}")
            indexes[name] = matches[0]
        positions = {"stage_valid": 5, "act_slice": 4, "weight_slices": 16,
                     "partial_products": 32, "column_sums": 56, "phase_acc": 48, "result_ch0": 64}
        stage_probes = [("act_slice", "weight_slices"), ("partial_products",),
                        ("column_sums",), ("phase_acc",), ("result_ch0",)]
        radices = ["HEX"] * len(header)
        sample = 0
        for row in rows:
            if not row or all(not field.strip() for field in row):
                continue
            if row[0].strip().upper().split(" - ", 1)[0] == "RADIX":
                radices = [field.strip().upper() for field in row]
                continue

            def get(name):
                idx = indexes[name]
                return parse_bus(row[idx], radices[idx], positions[name])

            mask = get("stage_valid")
            for bit, names in enumerate(stage_probes):
                if mask & (1 << bit):
                    stages[bit].append((sample, *(get(name) for name in names)))
            sample += 1
    return stages


def verify(case, stages):
    bits = case["operand_bits"]
    count = bits // 4
    a, w, lanes = [case[key] for key in ("activation", "weight", "active_inputs")]
    if [len(stage) for stage in stages] != [count, count, count, count, 1]:
        raise ValueError(f"Expected one complete job, found stage event counts {[len(s) for s in stages]}")
    origin = stages[0][0][0]
    out_cycle, raw = stages[-1][0]
    width = case["output_bits"]
    actual = signed(raw & ((1 << width) - 1), width)
    acc = 0
    for i, (issue, product, column, update) in enumerate(zip(*stages[:4], strict=True)):
        issue_t, act_slice, weight_slices = issue
        product_t, products = product
        column_t, sums = column
        update_t, phase_acc = update
        raw_a = (a >> ((count - 1 - i) * 4)) & 15
        if issue_t != origin + i or act_slice != raw_a or (weight_slices ^ w) & ((1 << bits) - 1):
            raise ValueError(f"Input mismatch at phase {i}, sample {issue_t}")
        if not issue_t < product_t < column_t < update_t < out_cycle:
            raise ValueError(f"Invalid stage ordering at phase {i}")
        act = signed(act_slice, 4) if i == 0 else act_slice
        decoded_sums = []
        for j in range(count):
            nibble = (weight_slices >> (j * 4)) & 15
            weight = signed(nibble, 4) if j == count - 1 else nibble
            pp = act * weight
            col = lanes * pp
            if (products >> (j * 8)) & 255 != pp & 255:
                raise ValueError(f"Partial product mismatch at phase {i}, weight slice {j}")
            if (sums >> (j * 14)) & 16383 != col & 16383:
                raise ValueError(f"Column sum mismatch at phase {i}, weight slice {j}")
            decoded_sums.append(col)
        contribution = sum(value << (4 * j) for j, value in enumerate(decoded_sums))
        acc = 16 * acc + contribution
        if signed(phase_acc, 48) != acc:
            raise ValueError(f"Merge accumulator mismatch at phase {i}")
    if acc != actual or actual != case["expected"]:
        raise ValueError(f"Result mismatch: ILA={actual}, merged={acc}, expected={case['expected']}")
    return out_cycle - origin, actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    folder = parser.parse_args().capture_dir
    case = json.loads((folder / "readback.json").read_text(encoding="utf-8"))
    if not case["pass"]:
        raise ValueError("Host readback did not pass")
    cycle, actual = verify(case, read_stages(folder / "precision.csv"))
    if actual != case["actual"]:
        raise ValueError("ILA and host readback disagree")
    result = {"case": case["case"], "write_cycle": cycle, "actual": actual,
              "actual_hex": case["actual_hex"], "expected": case["expected"], "pass": True}
    (folder / "waveform_check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"ILA PASS {case['case']}: cycle {cycle}, result {case['actual_hex']} ({actual})")


if __name__ == "__main__":
    main()
