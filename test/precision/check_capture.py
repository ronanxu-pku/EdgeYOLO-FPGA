"""Verify all seven FPGA ILA probe buses against the selected precision case."""
import argparse
import csv
from html import escape
import json
import re
from pathlib import Path

PROBES = (
    "stage_valid", "act_slice", "weight_slices", "partial_products",
    "column_sums", "phase_acc", "result_ch0",
)


def signed(value, width):
    return value - (1 << width) if value & (1 << (width - 1)) else value


def parse_bus(value, radix, width):
    text = value.strip().replace("_", "")
    match = re.fullmatch(r"(?:\d+)?'([hbdo])([0-9a-fA-F]+)", text)
    if match:
        return int(match[2], {"h": 16, "b": 2, "d": 10, "o": 8}[match[1]])
    base = {"BIN": 2, "BINARY": 2, "OCT": 8, "SIGNED": 10, "UNSIGNED": 10, "DEC": 10}.get(radix, 16)
    if text.lower().startswith(("0x", "0b")):
        return int(text, 0)
    return int(text, base) & ((1 << width) - 1)


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
    summary = []
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
        decoded_products = []
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
            decoded_products.append(pp)
            decoded_sums.append(col)
        contribution = sum(value << (4 * j) for j, value in enumerate(decoded_sums))
        acc = 16 * acc + contribution
        if signed(phase_acc, 48) != acc:
            raise ValueError(f"Merge accumulator mismatch at phase {i}")
        summary.append([i, issue_t-origin, f"{act_slice:X}", act, f"{weight_slices:04X}",
                        product_t-origin, decoded_products, column_t-origin, decoded_sums,
                        contribution, update_t-origin, acc])
    if acc != actual or actual != case["expected"]:
        raise ValueError(f"Result mismatch: ILA={actual}, merged={acc}, expected={case['expected']}")
    return summary, out_cycle - origin, actual


def write_waveform(path, case, phases, write_cycle, actual):
    """Render each valid sample into a one-cycle box on a shared time axis."""
    cols = case["operand_bits"] // 4
    tracks = [
        ("Activation phase / slice", [(p[1], 1, f"{p[0]} / {p[2]}") for p in phases], "#17618e"),
        ("Weight slices", [(0, len(phases), "0x" + phases[0][4][-cols:])], "#17618e"),
    ]
    tracks.extend((f"4 x 4 product w{j}", [(p[5], 1, str(p[6][j])) for p in phases], "#17618e")
                  for j in range(cols))
    tracks.extend((f"Adder tree column w{j}", [(p[7], 1, str(p[8][j])) for p in phases], "#654297")
                  for j in range(cols))
    tracks.extend([
        ("Weighted phase contribution", [(p[10], 1, str(p[9])) for p in phases], "#654297"),
        ("Accumulator after phase", [(p[10], 1, str(p[11])) for p in phases], "#654297"),
        ("OBUF channel 0 write", [(write_cycle, 1, "")], "#00745e"),
    ])
    origin = 276
    step = 74
    extent = write_cycle + 4
    bottom = 116 + len(tracks) * 31
    width = origin + extent * step + 30
    height = bottom + 54
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<g font-family="Arial, sans-serif" fill="#14263b">']

    def label(x, y, value, size=16, color="#14263b", anchor="start"):
        svg.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}">'
                   f'{escape(str(value))}</text>')

    source = case.get("sample_source", "FPGA ILA")
    label(25, 37, f"{source}  |  {case['case'].upper()}  |  Tile 0 / output channel 0", 25)
    label(25, 70, f"A = {case['activation']:,}    W = {case['weight']:,}    "
                  f"Active inputs = {case['active_inputs']}    Clock = 250 MHz", 17)
    for cycle in range(extent):
        x = origin + cycle * step
        svg.append(f'<line x1="{x}" y1="88" x2="{x}" y2="{bottom}" stroke="#e3e9f0"/>')
        label(x, bottom + 18, cycle, 13, anchor="middle")
    for i, (name, values, color) in enumerate(tracks):
        y = 110 + i * 31
        label(25, y + 6, name)
        svg.append(f'<line x1="{origin}" y1="{y}" x2="{width-30}" y2="{y}" stroke="#b6c5d4"/>')
        for cycle, length, text in values:
            x = origin + cycle * step
            svg.append(f'<rect x="{x}" y="{y-12}" width="{length*step-1}" height="24" rx="2" fill="{color}"/>')
            if text:
                label(x + length * step / 2, y + 5, text, 12 if len(text) > 8 else 15,
                      "white", "middle")
    x = origin + (write_cycle + 1) * step + 6
    label(x, 116 + (len(tracks)-1) * 31, f"{actual:,} / PASS", 17, "#00745e")
    label(origin, bottom + 42, "DCIM sample cycle from first input accept", 15)
    path.write_text("\n".join(svg + ["</g></svg>"]) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    folder = parser.parse_args().capture_dir
    case = json.loads((folder / "readback.json").read_text(encoding="utf-8"))
    if not case["pass"]:
        raise ValueError("Host readback did not pass")
    phases, cycle, actual = verify(case, read_stages(folder / "precision.csv"))
    if actual != case["actual"]:
        raise ValueError("ILA and host readback disagree")
    with (folder / "phase_trace.csv").open("w", newline="", encoding="ascii") as stream:
        out = csv.writer(stream)
        out.writerow(["phase", "input_cycle", "activation_hex", "activation_value", "weight_hex",
                      "multiply_cycle", "products", "column_cycle", "column_sums",
                      "phase_contribution", "accumulate_cycle", "accumulated"])
        out.writerows(phases)
    result = {"case": case["case"], "write_cycle": cycle, "actual": actual,
              "actual_hex": case["actual_hex"], "expected": case["expected"], "pass": True}
    (folder / "waveform_check.json").write_text(json.dumps(result, indent=2) + "\n")
    write_waveform(folder / "waveform.svg", case, phases, cycle, actual)
    print(f"ILA PASS {case['case']}: cycle {cycle}, result {case['actual_hex']} ({actual})")


if __name__ == "__main__":
    main()
