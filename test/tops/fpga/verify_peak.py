"""Extract the INT8 throughput and correctness evidence from one TOPS capture."""
import csv
from pathlib import Path

import numpy as np

from ila_runtime import parse_bus


PROBES = {
    "issue_mask": 8, "issue_phase_index": 7, "act_ch0_nibble": 4,
    "ma_accept_mask": 8, "ma_ch0_col_sums": 28,
    "write_done_mask": 8, "write_job": 6, "write_ch0": 32,
}
STAGES = (
    ("issue_mask", "issue_phase_index", "act_ch0_nibble"),
    ("ma_accept_mask", "ma_ch0_col_sums"),
    ("write_done_mask", "write_job", "write_ch0"),
)
FULL_MASK = 0xff
JOB_COUNT = 64
PHASE_COUNT = 2 * JOB_COUNT
RESULT_WORDS = JOB_COUNT * 8 * 4


def read_events(path: Path):
    events = [[] for _ in STAGES]
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = csv.reader(stream)
        for header in rows:
            names = [field.strip().rsplit("/", 1)[-1].split("[", 1)[0] for field in header]
            if "issue_mask" in names:
                break
        else:
            raise ValueError("TOPS probe names are missing from the Vivado CSV")
        for name in PROBES:
            if names.count(name) != 1:
                raise ValueError(f"Expected exactly one CSV column for {name}")
        indexes = {name: names.index(name) for name in PROBES}
        radices = ["HEX"] * len(header)
        sample = 0
        for row in rows:
            if not any(field.strip() for field in row):
                continue
            if row[0].strip().upper().split(" - ", 1)[0] == "RADIX":
                radices = [field.strip().upper() for field in row]
                continue
            if len(row) < len(header):
                raise ValueError(f"Incomplete Vivado CSV row at sample {sample}")

            def bus(name):
                i = indexes[name]
                return parse_bus(row[i], radices[i], PROBES[name])

            for records, (mask_name, *signals) in zip(events, STAGES, strict=True):
                mask = bus(mask_name)
                if mask == FULL_MASK:
                    records.append((sample, *(bus(signal) for signal in signals)))
                elif mask:
                    raise ValueError(f"{mask_name}=0x{mask:02X} at sample {sample}, expected all 8 Tile bits")
            sample += 1
    return events


def verify_peak(folder: Path, host_report: dict) -> dict:
    checks = host_report["host_results"]
    if (host_report["host_correctness_pass"] is not True or
            not checks or any(check["pass"] is not True or check["failed"] for check in checks) or
            sum(check["total_words"] for check in checks) != RESULT_WORDS or
            sum(check["passed"] for check in checks) != RESULT_WORDS):
        raise ValueError(f"Host readback must match all {RESULT_WORDS} result words")

    issues, columns, writes = read_events(folder / "peak_int8.csv")
    counts = (len(issues), len(columns), len(writes))
    if counts != (PHASE_COUNT, PHASE_COUNT, JOB_COUNT):
        raise ValueError(f"Expected issue/MA/write counts {(PHASE_COUNT, PHASE_COUNT, JOB_COUNT)}, got {counts}")
    issue_start, ma_start, write_start = (stage[0][0] for stage in (issues, columns, writes))
    issue_end = issues[-1][0] + 1
    if not issue_start < ma_start < write_start < issue_end:
        raise ValueError("The issue, MA and write stages did not overlap")

    with np.load(folder / "operands.npz") as inputs:
        activation = inputs["act"].astype(np.int32)
        weight = inputs["weight"][0].astype(np.int32)
        expected = inputs["golden"][:, 0].astype(np.int64)
    for phase, ((issue_sample, index, nibble), (ma_sample, packed)) in enumerate(zip(issues, columns, strict=True)):
        job, lo = divmod(phase, 2)
        a = activation[job] & 15 if lo else activation[job] >> 4
        c0, c1 = int(a @ (weight & 15)), int(a @ (weight >> 4))
        expected_columns = (c0 & 0x3fff) | ((c1 & 0x3fff) << 14)
        if (issue_sample, index, nibble) != (issue_start + phase, phase, int(a[0]) & 15):
            raise ValueError(f"Wrong issue sample, tag or activation for phase {phase}")
        if (ma_sample, packed) != (ma_start + phase, expected_columns):
            raise ValueError(f"Wrong MA sample or column sums for phase {phase}")
    for job, (sample, index, value) in enumerate(writes):
        if (sample, index, value) != (write_start + 2 * job, job, int(expected[job]) & 0xffffffff):
            raise ValueError(f"Wrong write cadence, job tag or result for job {job}")
        if sample <= columns[2 * job + 1][0]:
            raise ValueError(f"Job {job} completed before its final MA phase")

    return {
        "issue_sample_start": issue_start,
        "issue_sample_end_exclusive": issue_end,
        "issue_cycles": issue_end - issue_start,
        "ma_sample_start": ma_start,
        "ma_cycles": len(columns),
        "first_write_sample": write_start,
        "last_write_sample": writes[-1][0],
        "write_batch_count": len(writes),
        "write_interval_cycles": 2,
        "tile0_results_checked": len(writes),
        "host_words_matched": RESULT_WORDS,
    }
