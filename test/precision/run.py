"""Run FPGA precision tests and open the native Vivado ILA waveforms."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile

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


DATA_PROBES = (
    "prec_act_slice",
    "prec_weight_slices",
    "prec_partial_products",
    "prec_column_sums",
    "prec_phase_acc",
    "prec_result_ch0",
)


def native_wave_config(capture: Path, destination: Path) -> None:
    with ZipFile(capture) as archive:
        configs = [name for name in archive.namelist() if name.lower().endswith(".wcfg")]
        if len(configs) != 1:
            raise ValueError(f"Expected one native wave configuration in {capture}")
        config = ET.fromstring(archive.read(configs[0]))

    rows = {}
    for wave in config.iter("wvobject"):
        names = [wave.get("fp_name", "")]
        names.extend(prop.text or "" for prop in wave.findall("obj_property")
                     if prop.get("name") in {"ObjectShortName", "ElementShortName"})
        for name in names:
            probe = name.rsplit("/", 1)[-1].split("[", 1)[0]
            if probe in DATA_PROBES:
                rows[probe] = wave
                break
    if len(rows) != len(DATA_PROBES):
        missing = ", ".join(probe for probe in DATA_PROBES if probe not in rows)
        raise ValueError(f"Missing native waveform rows in {capture}: {missing}")

    for wave in config.findall("wvobject"):
        config.remove(wave)
    for probe in DATA_PROBES:
        config.append(rows[probe])
    size = config.find("WVObjectSize")
    if size is not None:
        size.set("size", str(len(DATA_PROBES)))

    # Native ILA wave configs use one fs per sample when the full window ends at 1025 fs.
    zoom_end = config.find("zoom_setting/ZoomEndTime")
    if zoom_end is not None and zoom_end.get("time") in {"1023fs", "1024fs", "1025fs"}:
        for element, sample in (("ZoomStartTime", 28), ("ZoomEndTime", 54), ("Cursor1Time", 32)):
            node = config.find(f"zoom_setting/{element}")
            if node is not None:
                node.set("time", f"{sample}fs")

    ET.ElementTree(config).write(destination, encoding="utf-8", xml_declaration=True)


def run_vivado(vivado: str, script: Path, cwd: Path, env: dict, mode: str):
    flags = ["-mode", mode, "-source", str(script), "-notrace", "-nolog", "-nojournal"]
    executable = None
    command = [vivado, *flags]
    if Path(vivado).suffix.lower() in {".bat", ".cmd"}:
        env.update(DCIM_PRECISION_VIVADO=vivado, DCIM_PRECISION_SCRIPT=str(script))
        executable = os.environ["COMSPEC"]
        command = (
            f'cmd.exe /d /v:off /s /c ""%DCIM_PRECISION_VIVADO%" -mode {mode} '
            '-source "%DCIM_PRECISION_SCRIPT%" -notrace -nolog -nojournal"'
        )
    return subprocess.run(command, executable=executable, cwd=cwd, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace")


def open_native_waveforms(vivado: str, run_dir: Path, cases: tuple[str, ...]) -> int:
    captures = {}
    for case in cases:
        path = run_dir / f"{case}.ila"
        if not path.is_file():
            path = run_dir / case / "precision.ila"
        if not path.is_file():
            raise FileNotFoundError(f"No native {case} ILA archive under {run_dir}. Run run.py to capture it.")
        captures[case] = path

    with tempfile.TemporaryDirectory(prefix="dcim_ila_view_") as scratch:
        work = Path(scratch)
        for case, path in captures.items():
            native_wave_config(path, work / f"{case}.wcfg")
            shutil.copyfile(path, work / f"{case}.ila")
        env = dict(os.environ, DCIM_PRECISION_VIEW_CASES=" ".join(cases))
        opened = run_vivado(vivado, Path(__file__).with_name("view.tcl").resolve(), work, env, "gui")
        if opened.returncode:
            print("\n".join(opened.stdout.splitlines()[-20:]), file=sys.stderr)
        return opened.returncode


def capture_on_board(args: argparse.Namespace, cases: tuple[str, ...], run_dir: Path) -> int:
    vivado = find_vivado(args.vivado)
    ltx = ROOT / "bitstream/top_precision.ltx"
    if not ltx.is_file():
        raise FileNotFoundError(ltx)
    if run_dir.exists():
        raise FileExistsError(run_dir)

    env = dict(
        os.environ,
        DCIM_PRECISION_PYTHON=sys.executable,
        DCIM_PRECISION_LTX=str(ltx),
        DCIM_PRECISION_CASES=" ".join(cases),
        DCIM_PRECISION_HW_SERVER=args.hw_server,
        DCIM_PRECISION_HW_TARGET=args.hw_target,
        DCIM_PRECISION_TIMEOUT=str(args.timeout_s),
        DCIM_PRECISION_QUIET=str(int(args.quiet_xdma)),
    )
    for name in ("PATH", "PYTHONHOME", "PYTHONPATH"):
        env[f"DCIM_PRECISION_HOST_{name}"] = os.environ.get(name, "")
    script = Path(__file__).with_name("capture.tcl").resolve()
    with tempfile.TemporaryDirectory(prefix="dcim_ila_capture_") as scratch:
        work = Path(scratch)
        completed = run_vivado(vivado, script, work, env, "batch")
        if completed.returncode:
            print("\n".join(completed.stdout.splitlines()[-24:]), file=sys.stderr)
            return completed.returncode

        results = {}
        for case in cases:
            folder = work / case
            result = json.loads((folder / "waveform_check.json").read_text(encoding="utf-8"))
            if result.get("case") != case or result.get("pass") is not True or not (folder / "capture.ila").is_file():
                raise RuntimeError(f"Incomplete or failed FPGA capture for {case}")
            results[case] = result

        run_dir.mkdir(parents=True, exist_ok=False)
        for case, result in results.items():
            path = run_dir / f"{case}.ila"
            shutil.copyfile(work / case / "capture.ila", path)
            print(f"FPGA PASS {case}: {result['actual_hex']}  {path}", flush=True)

    return open_native_waveforms(vivado, run_dir, cases)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", "--mode", choices=(*CASES, "both"), default="both")
    parser.add_argument("--run-dir", type=Path)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--prepare-only", action="store_true")
    execution.add_argument("--view-only", type=Path, metavar="CAPTURE_DIR",
                           help="Open a saved run in the native Vivado waveform viewer")
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
        if args.view_only:
            return open_native_waveforms(find_vivado(args.vivado), args.view_only.resolve(), cases)
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
    except (OSError, RuntimeError, ValueError, BadZipFile, ET.ParseError) as error:
        print(f"Precision test failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
