"""Run FPGA precision tests and export hardware ILA waveforms."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "project/rtl/tb/lite_bd/module_tb"))
import golden_module_tb as golden

CASES = {
    "int8": (8, 1, -93, 109),
    "int16": (16, 1, 27341, -18530),
    "int16-high": (16, 4, -32768, -32768),
}


def prepare_case(case: str, run_dir: Path) -> dict:
    bits, lanes, activation, weight = CASES[case]
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
        "active_inputs": lanes, "activation": activation, "weight": weight,
        "expected": int(accumulator[0, 0]), "words_per_tile": words_per_tile,
    }
    (run_dir / "case.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="ascii")
    return evidence


def run_xdma(case: str, run_dir: Path, timeout_s: float, quiet_xdma: bool) -> int:
    evidence = prepare_case(case, run_dir)
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



def find_vivado(override: str | None) -> str:
    if override:
        executable = shutil.which(override)
        if executable:
            return executable
        raise FileNotFoundError(f"Vivado launcher not found: {override}")

    drive = os.environ.get("SystemDrive", "C:")
    for product, launcher, variable in (
        ("Vivado_Lab", "vivado_lab.bat", "XILINX_VIVADO_LAB"),
        ("Vivado", "vivado.bat", "XILINX_VIVADO"),
    ):
        executable = shutil.which(launcher)
        if executable:
            return executable
        install = os.environ.get(variable)
        if install:
            candidate = Path(install) / "bin" / launcher
            if candidate.is_file():
                return str(candidate)
        for vendor in ("AMDDesignTools", "Xilinx"):
            root = Path(f"{drive}/{vendor}")
            for pattern in (f"*/{product}/bin/{launcher}", f"{product}/*/bin/{launcher}"):
                matches = sorted(root.glob(pattern), reverse=True)
                if matches:
                    return str(matches[0])
    raise FileNotFoundError("Vivado Lab or Vivado was not found. Use --vivado with the launcher path.")


def capture_on_board(args: argparse.Namespace, cases: tuple[str, ...], run_dir: Path) -> int:
    vivado = find_vivado(args.vivado)
    script = Path(__file__).with_name("capture.tcl").resolve()
    ltx = ROOT / "bitstream/top_precision.ltx"
    if not ltx.is_file():
        raise FileNotFoundError(ltx)

    run_dir.mkdir(parents=True, exist_ok=False)
    print(f"FPGA capture directory: {run_dir}", flush=True)
    env = dict(
        os.environ,
        DCIM_PRECISION_VIVADO=vivado,
        DCIM_PRECISION_SCRIPT=str(script),
        DCIM_PRECISION_PYTHON=sys.executable,
        DCIM_PRECISION_LTX=str(ltx),
        DCIM_PRECISION_CASES=" ".join(cases),
        DCIM_PRECISION_HW_SERVER=args.hw_server,
        DCIM_PRECISION_HW_TARGET=args.hw_target,
        DCIM_PRECISION_TIMEOUT=str(args.timeout_s),
        DCIM_PRECISION_QUIET=str(int(args.quiet_xdma)),
    )
    flags = ["-mode", "batch", "-source", str(script), "-notrace", "-nojournal", "-log", "vivado.log"]
    if Path(vivado).suffix.lower() in {".bat", ".cmd"}:
        # Keep paths in quoted environment expansions and pass only fixed shell text.
        command = (
            'cmd.exe /d /v:off /s /c ""%DCIM_PRECISION_VIVADO%" -mode batch '
            '-source "%DCIM_PRECISION_SCRIPT%" -notrace -nojournal -log vivado.log"'
        )
        completed = subprocess.run(command, executable=os.environ["COMSPEC"], cwd=run_dir, env=env)
    else:
        completed = subprocess.run([vivado, *flags], cwd=run_dir, env=env)

    if completed.returncode:
        print(f"FPGA capture failed. See {run_dir / 'vivado.log'} and the case host.log.", file=sys.stderr)
        return completed.returncode

    for case in cases:
        folder = run_dir / case
        result = json.loads((folder / "waveform_check.json").read_text(encoding="utf-8"))
        waveform = folder / "waveform.svg"
        if result.get("case") != case or result.get("pass") is not True or not waveform.is_file():
            raise RuntimeError(f"Incomplete or failed FPGA capture for {case}: {folder}")
        print(f"FPGA PASS {case}: {waveform}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", "--mode", choices=(*CASES, "both"), default="both")
    parser.add_argument("--run-dir", type=Path)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--prepare-only", action="store_true")
    execution.add_argument("--xdma-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--vivado", help="Path to vivado_lab.bat or vivado.bat")
    parser.add_argument("--hw-server", default="localhost:3121")
    parser.add_argument("--hw-target", default="*", help="Vivado JTAG target name or pattern")
    parser.add_argument("--timeout-s", type=float, default=60.0, help="XDMA decoder timeout in seconds")
    parser.add_argument("--quiet-xdma", action="store_true")
    args = parser.parse_args()

    if not args.prepare_only and os.name != "nt":
        parser.error("Board execution requires Windows and the XDMA driver.")
    if args.xdma_only and (args.case == "both" or args.run_dir is None):
        parser.error("The XDMA worker requires one case and a new run directory.")
    cases = ("int8", "int16") if args.case == "both" else (args.case,)
    out = ROOT / "test/precision/output"
    run_dir = (args.run_dir or out / f"{args.case}_{datetime.now():%Y%m%d_%H%M%S_%f}").resolve()

    try:
        if args.xdma_only:
            return run_xdma(args.case, run_dir, args.timeout_s, args.quiet_xdma)
        if args.prepare_only:
            if len(cases) > 1:
                run_dir.mkdir(parents=True, exist_ok=False)
            for case in cases:
                folder = run_dir / case if len(cases) > 1 else run_dir
                prepare_case(case, folder)
                print(f"Prepared {case}: {folder}")
            return 0
        return capture_on_board(args, cases, run_dir)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Precision test failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
