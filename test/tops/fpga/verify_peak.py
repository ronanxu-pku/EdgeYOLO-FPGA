"""Extract the INT8 throughput and correctness evidence from one TOPS capture."""
import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.ila.ila_runtime import parse_bus


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
BOARDREF_PROBES = {
    "peak_compute_mask": 8, "peak_job": 6, "peak_phase": 2, "peak_dcim_input": 32,
    "peak_result_valid": 1, "peak_result_data": 32,
}
BOARDREF_STAGES = (
    ("peak_compute_mask", "peak_job", "peak_phase", "peak_dcim_input"),
    ("peak_result_valid", "peak_result_data"),
)
TILE_RESULTS = tuple(f"peak_tile{tile}_result_ch0" for tile in range(8))
ALL_TILE_PROBES = {
    "peak_ma_fire_mask": 8, "peak_result_valid_mask": 8,
    **{name: 32 for name in TILE_RESULTS},
}
ALL_TILE_STAGES = (("peak_ma_fire_mask",), ("peak_result_valid_mask", *TILE_RESULTS))
ALL_TILE_VIEW = ("peak_compute_mask", "peak_ma_fire_mask", "peak_result_valid_mask",
                 "peak_job", "peak_phase", *TILE_RESULTS)
JOB_COUNT = 64
PHASE_COUNT = 2 * JOB_COUNT
RESULT_WORDS = JOB_COUNT * 8 * 4


def read_events(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = csv.reader(stream)
        for header in rows:
            names = [field.strip().rsplit("/", 1)[-1].split("[", 1)[0].removeprefix("dcim_array_0_")
                     for field in header]
            if "issue_mask" in names or "peak_compute_mask" in names:
                break
        else:
            raise ValueError("TOPS probe names are missing from the Vivado CSV")
        boardref = "peak_compute_mask" in names
        probes, stages = (BOARDREF_PROBES, BOARDREF_STAGES) if boardref else (PROBES, STAGES)
        if boardref and any(name in names for name in ALL_TILE_PROBES):
            probes = {**BOARDREF_PROBES, **ALL_TILE_PROBES}
            stages = BOARDREF_STAGES + ALL_TILE_STAGES
        events = [[] for _ in stages]
        for name in probes:
            if names.count(name) != 1:
                raise ValueError(f"Expected exactly one CSV column for {name}")
        indexes = {name: names.index(name) for name in probes}
        sample_index = next((i for i, name in enumerate(names) if name.lower() == "sample in buffer"), None)
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
            if sample_index is not None and int(row[sample_index]) != sample:
                raise ValueError(f"Missing or reordered Vivado CSV sample {sample}")

            def bus(name):
                i = indexes[name]
                return parse_bus(row[i], radices[i], probes[name])

            for records, (mask_name, *signals) in zip(events, stages, strict=True):
                mask = bus(mask_name)
                if mask == (1 << probes[mask_name]) - 1:
                    records.append((sample, *(bus(signal) for signal in signals)))
                elif mask:
                    raise ValueError(f"{mask_name}=0x{mask:02X} at sample {sample}, expected all 8 Tile bits")
            sample += 1
    return boardref, events


def verify_boardref(issues, writes, folder: Path) -> dict:
    if (len(issues), len(writes)) != (PHASE_COUNT, JOB_COUNT):
        raise ValueError(f"Expected {PHASE_COUNT} issue cycles and {JOB_COUNT} Tile 0 first writes, "
                         f"got {len(issues)} and {len(writes)}")
    issue_start, write_start = issues[0][0], writes[0][0]
    issue_end = issues[-1][0] + 1
    if not issue_start + 1 < write_start < issue_end:
        raise ValueError("Tile 0 writes did not overlap subsequent all-Tile input phases")

    with np.load(folder / "operands.npz") as inputs:
        activation = inputs["act"].astype(np.int32)
        golden = inputs["golden"][:, 0].astype(np.int64)
    for phase, record in enumerate(issues):
        job, low = divmod(phase, 2)
        nibbles = (activation[job, :8] >> (0 if low else 4)) & 15
        packed = sum(int(nibble) << (4 * lane) for lane, nibble in enumerate(nibbles))
        if record != (issue_start + phase, job, low, packed):
            raise ValueError(f"Wrong issue cadence, job, phase or Tile 0 input at phase {phase}")
    for job, (sample, result) in enumerate(writes):
        if (sample, result) != (write_start + 2 * job, int(golden[job]) & 0xffffffff):
            raise ValueError(f"Wrong Tile 0 first-write cadence or result at job {job}")
        if sample <= issues[2 * job + 1][0]:
            raise ValueError(f"Tile 0 job {job} wrote before both input phases")

    return {
        "probe_layout": "boardref",
        "active_tile_mask": "0xFF",
        "issue_sample_start": issue_start,
        "issue_sample_end_exclusive": issue_end,
        "issue_cycles": issue_end - issue_start,
        "first_write_sample": write_start,
        "last_write_sample": writes[-1][0],
        "write_interval_cycles": 2,
        "tile0_results_checked": len(writes),
        "host_words_matched": RESULT_WORDS,
    }


def verify_all_tiles(issues, tile0_writes, columns, writes, folder: Path) -> dict:
    if (len(columns), len(writes)) != (PHASE_COUNT, JOB_COUNT):
        raise ValueError("Expected 128 all-Tile MA events and 64 all-Tile writes")
    ma_start = columns[0][0]
    if not issues[0][0] < ma_start < writes[0][0] < issues[-1][0] + 1:
        raise ValueError("All-Tile issue, MA and write stages did not overlap")
    if [sample for (sample,) in columns] != list(range(ma_start, ma_start + PHASE_COUNT)):
        raise ValueError("All-Tile MA events are not continuous")
    with np.load(folder / "operands.npz") as inputs:
        golden = inputs["golden"].astype(np.int64)
    if golden.shape != (JOB_COUNT, 128):
        raise ValueError(f"Unexpected all-Tile golden shape: {golden.shape}")
    for job, (sample, *values) in enumerate(writes):
        if sample != tile0_writes[job][0] or sample <= columns[2 * job + 1][0]:
            raise ValueError(f"Wrong all-Tile write timing at job {job}")
        for tile, value in enumerate(values):
            expected = int(golden[job, tile * 16]) & 0xffffffff
            if value != expected:
                raise ValueError(f"Tile {tile} channel 0 mismatch at job {job}: "
                                 f"0x{value:08X} != 0x{expected:08X}")
    return {
        "probe_layout": "all_tiles", "ma_active_tile_mask": "0xFF",
        "write_active_tile_mask": "0xFF", "ma_sample_start": ma_start,
        "ma_cycles": len(columns), "write_batch_count": len(writes),
        "tiles_checked": 8, "per_tile_ch0_results_checked": JOB_COUNT,
        "all_tile_ch0_results_checked": 8 * JOB_COUNT,
        "first_tile_results_hex": [f"0x{value:08X}" for value in writes[0][1:]],
    }


def verify_peak(folder: Path, host_report: dict) -> dict:
    checks = host_report.get("host_results", [])
    if (host_report.get("host_correctness_pass") is not True or
            not checks or any(check["pass"] is not True or check["failed"] for check in checks) or
            sum(check["total_words"] for check in checks) != RESULT_WORDS or
            sum(check["passed"] for check in checks) != RESULT_WORDS):
        raise ValueError(f"Host readback must match all {RESULT_WORDS} result words")

    boardref, events = read_events(folder / "peak_int8.csv")
    if boardref:
        result = verify_boardref(*events[:2], folder)
        if len(events) == 4:
            result.update(verify_all_tiles(*events, folder))
        return result
    issues, columns, writes = events
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
        "probe_layout": "pipeline",
        "active_tile_mask": "0xFF",
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
