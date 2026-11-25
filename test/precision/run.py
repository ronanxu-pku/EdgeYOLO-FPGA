"""Run FPGA precision tests and open the native Vivado ILA waveforms."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.ila.ila_runtime import (
    capture_env, find_vivado, open_native_waveforms, resolve_ltx, run_vivado,
)
from utils.ila.results import load_json, read_text, readback_comparison, start_result, timestamp, write_result

sys.path.insert(0, str(ROOT / "project/rtl/tb/lite_bd/module_tb"))
import golden_module_tb as golden

CASES = {
    "int8": (8, 1, -93, 109),
    "int16": (16, 1, 27341, -18530),
    "int16-high": (16, 4, -32768, -32768),
}


def prepare_case(case: str, run_dir: Path, seed: int | None = None) -> dict:
    bits, lanes, activation, weight = CASES[case]
    if seed is not None:
        if seed < 0:
            raise ValueError("--seed must be a non-negative integer.")
        if case == "int16-high":
            raise ValueError("int16-high is a fixed overflow case; omit --seed.")
        # Key the stream by precision so --mode both and separate runs agree.
        rng = np.random.Generator(np.random.PCG64([seed, bits]))
        operands = rng.integers(1, 1 << bits, size=2)
        activation, weight = (
            int(value) - (1 << bits) if value >= (1 << (bits - 1)) else int(value)
            for value in operands
        )
    native16 = bits == 16
    dtype = np.dtype("<i2") if native16 else np.dtype("i1")
    out_channels = golden.DCIM_INT16_OUT_CH_PER_TILE if native16 else golden.DCIM_INT8_OUT_CH_PER_TILE
    meta = golden.ConvMeta(case, golden.DCIM_CH_IN, out_channels, 1, 1, 1, 1, 0, 0, 0, 0, "")
    activations = np.zeros((1, golden.DCIM_CH_IN), dtype=dtype)
    weights = np.zeros((out_channels, golden.DCIM_CH_IN), dtype=dtype)
    activations[0, :lanes] = activation
    weights[0, :lanes] = weight
    accumulator = activations.astype(np.int64) @ weights.astype(np.int64).T

    if native16:
        packed_weights = golden.pack_weight_tile_int16(weights, 0, 1)
        expected = golden.dcim_accum_words_int16(accumulator, 1)
        words_per_tile = golden.DCIM_INT16_OUT_WORDS_PER_TILE
    else:
        packed_weights = golden.pack_weight_tile(meta, weights, 0)
        expected = golden.dcim_accum_words(accumulator, 1)
        words_per_tile = golden.DCIM_INT8_OUT_WORDS_PER_TILE
    run_dir.mkdir(parents=True, exist_ok=False)
    preload = golden.ibuf_preload_per_tile(
        str(run_dir), golden.bytes_to_128_words(activations.tobytes()),
        [[f"{word:032x}" for word in packed_weights]], 1,
    )
    golden.make_fast_svh(preload, str(run_dir))
    inst = golden.dcim_layer_inst(
        meta, 1, 0, golden.DCIM_OUT_BASE, golden.IBUF_ACT, golden.IBUF_WEI,
        skip_cdma=True, int16=native16, acc_override=1,
    )
    inst.append(golden.header(golden.OP_END, 0, 0))
    golden.write_inst(run_dir / "inst.hex", inst)
    golden.write_hex(run_dir / "expected.hex", expected)
    (run_dir / "checks.txt").write_text(
        f"{case} expected.hex {golden.TILE_OBUF_DST:06x} {len(expected)} 0 {words_per_tile}\n",
        encoding="ascii",
    )
    evidence = {
        "case": case, "operand_bits": bits, "output_bits": 64 if native16 else 32,
        "seed": seed,
        "active_inputs": lanes, "activation": activation, "weight": weight,
        "expected": int(accumulator[0, 0]), "words_per_tile": words_per_tile,
    }
    (run_dir / "case.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="ascii")
    return evidence


def run_xdma(case: str, run_dir: Path, timeout_s: float, quiet_xdma: bool,
             seed: int | None = None) -> int:
    evidence = prepare_case(case, run_dir, seed)
    sys.path.insert(0, str(ROOT / "project/runtime"))
    from xdma_win import ChipRunnerWin, TILE_OBUF_BASE

    runner = ChipRunnerWin(verbose=not quiet_xdma)
    try:
        results = runner.run_case(run_dir, timeout_s=timeout_s, staging="preload", verify=True)
        readback = runner.x.read(TILE_OBUF_BASE, int(evidence["words_per_tile"]) * 16)
    finally:
        runner.x.close()
    (run_dir / "tile0_readback.bin").write_bytes(readback)
    width = evidence["output_bits"] // 8
    evidence["actual"] = int.from_bytes(readback[:width], "little", signed=True)
    evidence["actual_hex"] = f"0x{int.from_bytes(readback[:width], 'little'):0{width * 2}X}"
    evidence["checks"] = results
    evidence["pass"] = bool(results) and all(check["pass"] for check in results)
    evidence["pass"] &= evidence["actual"] == evidence["expected"]
    (run_dir / "readback.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="ascii")
    status = "PASS_HOST_PENDING_ILA" if evidence["pass"] else "FAIL_HOST"
    print(f"{status}: "
          f"actual={evidence['actual']} ({evidence['actual_hex']}) expected={evidence['expected']}")
    return 0 if evidence["pass"] else 1



DATA_PROBES = (
    "prec_act_slice",
    "prec_weight_slices",
    "prec_partial_products",
    "prec_column_sums",
    "prec_phase_acc",
    "prec_result_ch0",
)


def open_precision_waveforms(vivado: str, run_dir: Path, cases: tuple[str, ...]) -> int:
    captures = {}
    for case in cases:
        path = run_dir / f"{case}.ila"
        if not path.is_file():
            path = run_dir / case / "precision.ila"
        if not path.is_file():
            raise FileNotFoundError(f"No native {case} ILA archive under {run_dir}. Run run.py to capture it.")
        captures[case] = path

    return open_native_waveforms(vivado, captures, DATA_PROBES, last_sample=54)


def capture_on_board(args: argparse.Namespace, cases: tuple[str, ...], run_dir: Path) -> int:
    report = start_result(run_dir, "precision", (*[f"{case}.ila" for case in CASES], "vivado.log"))
    report.update(seed=args.seed, cases={})
    try:
        vivado = find_vivado(args.vivado)
        ltx = resolve_ltx(ROOT, args.ltx)
        report["probes_file"] = str(ltx)
        env = capture_env(sys.executable, ltx, args.hw_server, args.hw_target)
        env.update(DCIM_PRECISION_CASES=" ".join(cases), DCIM_PRECISION_TIMEOUT=str(args.timeout_s),
                   DCIM_PRECISION_QUIET=str(int(args.quiet_xdma)))
        env.pop("DCIM_PRECISION_SEED", None)
        if args.seed is not None:
            env["DCIM_PRECISION_SEED"] = str(args.seed)
        script = Path(__file__).with_name("capture.tcl").resolve()
        print("Starting Vivado precision capture for " + ", ".join(cases), flush=True)
        with tempfile.TemporaryDirectory(prefix="dcim_ila_capture_", ignore_cleanup_errors=True) as scratch:
            work = Path(scratch)
            env["DCIM_PRECISION_CAPTURE_ROOT"] = str(work)
            completed = run_vivado(vivado, script, Path(tempfile.gettempdir()), env, "batch")
            report.update(vivado_returncode=completed.returncode, vivado_log=completed.stdout)
            for case in cases:
                folder = work / case
                inputs = load_json(folder / "case.json")
                host = load_json(folder / "readback.json")
                check = load_json(folder / "waveform_check.json")
                words = {
                    "channel0": {
                        "fpga_signed": host.get("actual"),
                        "golden_signed": inputs.get("expected"),
                    },
                    **readback_comparison(folder),
                }
                archive = folder / "capture.ila"
                captured = archive.is_file() and archive.stat().st_size > 0
                if captured:
                    shutil.copyfile(archive, run_dir / f"{case}.ila")
                passed = (captured and host.get("pass") is True and words.get("match") is True
                          and check.get("pass") is True and check.get("case") == case
                          and check.get("seed") == args.seed)
                report["cases"][case] = {
                    "inputs": inputs, "host": host, "comparison": words,
                    "ila_file": f"{case}.ila" if captured else None,
                    "ila_check": check, "samples_csv": read_text(folder / "precision.csv"),
                    "pass": passed,
                }
                if passed:
                    print(f"FPGA PASS {case}: {check['actual_hex']} ({check['actual']}) "
                          f"matches the host and Golden, write cycle {check['write_cycle']}", flush=True)
            passed = not completed.returncode and all(item["pass"] for item in report["cases"].values())
            different = any(item["comparison"].get("match") is False
                            or item["ila_check"].get("pass") is False for item in report["cases"].values())
            report.update(status="PASS" if passed else "FAIL_CAPTURE", **{"pass": bool(passed)},
                          result="same" if passed else "different" if different else "incomplete")
            if not passed:
                report["error"] = "Incomplete or failed precision capture; see cases and vivado_log"
    except (OSError, RuntimeError, ValueError) as error:
        report.update(status="FAIL_CAPTURE", error=f"{type(error).__name__}: {error}")
    finally:
        write_result(run_dir, report)
    if not report["pass"]:
        print(f"Precision test failed: {report['error']}", file=sys.stderr)
        return 1
    return open_precision_waveforms(vivado, run_dir, cases)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", "--mode", choices=(*CASES, "both"), default="both")
    parser.add_argument("--seed", type=int,
                        help="Generate reproducible nonzero random operands for INT8/INT16; "
                             "omit to use the fixed waveform examples")
    parser.add_argument("--run-dir", type=Path, help="Output directory; replace this test's existing result files")
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--prepare-only", action="store_true")
    execution.add_argument("--view-only", type=Path, metavar="CAPTURE_DIR",
                           help="Open a saved run in the native Vivado waveform viewer")
    execution.add_argument("--xdma-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ltx", type=Path, help="Probe file matching the programmed FPGA")
    parser.add_argument("--vivado", help="Path to vivado_lab.bat or vivado.bat")
    parser.add_argument("--hw-server", default="localhost:3121")
    parser.add_argument("--hw-target", default="*", help="Vivado JTAG target name or pattern")
    parser.add_argument("--timeout-s", type=float, default=60.0, help="XDMA decoder timeout in seconds")
    parser.add_argument("--quiet-xdma", action="store_true")
    args = parser.parse_args()

    if args.seed is not None and args.seed < 0:
        parser.error("--seed must be a non-negative integer.")
    if args.seed is not None and args.case == "int16-high":
        parser.error("int16-high is a fixed overflow case; omit --seed.")
    if not args.prepare_only and os.name != "nt":
        parser.error("Board execution requires Windows and the XDMA driver.")
    if args.xdma_only and (args.case == "both" or args.run_dir is None):
        parser.error("The XDMA worker requires one case and a new run directory.")
    cases = ("int8", "int16") if args.case == "both" else (args.case,)
    out = ROOT / "test/precision/output"
    if args.prepare_only:
        out = ROOT / "test/precision/cached/prepared"
    run_dir = (args.run_dir or out / timestamp()).resolve()

    try:
        if args.view_only:
            view_dir = args.view_only.resolve()
            if args.case == "both":
                saved = load_json(view_dir / "result.json")
                available = tuple(case for case in saved.get("cases", {}) if (view_dir / f"{case}.ila").is_file())
                cases = available or cases
            return open_precision_waveforms(find_vivado(args.vivado), view_dir, cases)
        if args.xdma_only:
            return run_xdma(args.case, run_dir, args.timeout_s, args.quiet_xdma, args.seed)
        if args.prepare_only:
            if len(cases) > 1:
                run_dir.mkdir(parents=True, exist_ok=False)
            for case in cases:
                folder = run_dir / case if len(cases) > 1 else run_dir
                prepare_case(case, folder, args.seed)
                print(f"Prepared {case}: {folder}")
            return 0
        return capture_on_board(args, cases, run_dir)
    except (OSError, RuntimeError, ValueError, BadZipFile, ET.ParseError) as error:
        print(f"Precision test failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
