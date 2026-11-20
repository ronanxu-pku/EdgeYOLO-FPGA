#!/usr/bin/env python3
"""Run the streamed INT8 case and open its native Vivado TOPS waveform."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
import time
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ila_runtime import capture_env, find_vivado, open_native_waveforms, resolve_ltx, run_vivado


REPO_ROOT = Path(__file__).resolve().parents[2]
FPGA_DIR = Path(__file__).resolve().parent / "fpga"
DEFAULT_LTX = REPO_ROOT / "bitstream" / "top_tops_boardref.ltx"
GENERATOR = REPO_ROOT / "project" / "rtl" / "tb" / "lite_bd" / "module_tb" / "golden_module_tb.py"
UNIT_TB_DIR = REPO_ROOT / "test" / "network" / "host"
DEFAULT_RUN_DIR = REPO_ROOT / "test" / "tops" / "output" / "fpga_peak_int8"

MATRIX_M = 64
MATRIX_K = 64
MATRIX_N = 128
OPERATIONS_PER_ROUND = 2 * MATRIX_M * MATRIX_K * MATRIX_N
TOPS_REQUIREMENT = 2.0

EXPECTED_ILA_BASE = {
    "compute_mask": "0xff on every counted phase sample (64 jobs/round, II=2)",
    "input_phase0": "0xb92d4ab3",
    "input_phase1": "0xdb342919",
    "result_valid": "1 for the first Tile-0 output word",
    "result_data_when_valid": "0x00001778",
}

REQUIRED_CASE_FILES = (
    "inst.hex",
    "preload.txt",
    "checks.txt",
    "expected.hex",
    "act.hex",
    "weight_tile0.hex",
    "weight_tile7.hex",
    "manifest.txt",
    "operands.npz",
)


def _fmt_w(v) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_case(run_dir: Path, regenerate: bool, repeat_count: int) -> None:
    missing = [name for name in REQUIRED_CASE_FILES if not (run_dir / name).is_file()]
    if not regenerate and not missing:
        manifest = {}
        for line in (run_dir / "manifest.txt").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition(":")
            if sep:
                manifest[key.strip()] = value.strip()
        if (int(manifest.get("matmul_m", 0)) == MATRIX_M and
                int(manifest.get("benchmark_repeat_count", 1)) == repeat_count):
            print(f"[prepare] Reusing {run_dir}")
            return
        print("[prepare] Existing vectors are from the obsolete single-job case; regenerating")

    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(GENERATOR),
        "--module",
        "dcim_matmul",
        "--case",
        "peak_int8_all_tiles",
        "--seed",
        "123",
        "--verify-words",
        "0",
        "--out-dir",
        str(run_dir),
        "--benchmark-repeat",
        str(repeat_count),
    ]
    print("[prepare] Generating streamed M=64, K=64, N=128 INT8 case")
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)

    missing = [name for name in REQUIRED_CASE_FILES if not (run_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"generated case is incomplete: {', '.join(missing)}")


def calculate_tops(active_cycles: int, frequency_mhz: float, operations: int) -> float:
    if active_cycles <= 0:
        raise ValueError("ILA active cycles must be positive")
    if frequency_mhz <= 0:
        raise ValueError("frequency must be positive")
    return operations * frequency_mhz * 1e6 / active_cycles / 1e12


def write_report(report_path: Path, report: dict) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    tops = report.get("calculated_peak_tops")
    cycles = report.get("ila_active_cycles")
    tops_text = "pending ILA cycle entry" if tops is None else f"{tops:.6f} TOPS"
    cycles_text = "pending" if cycles is None else str(cycles)
    host_results = report.get("host_results", [])
    verified = sum(int(item.get("passed", 0)) for item in host_results)
    total = sum(int(item.get("total_words", 0)) for item in host_results)

    md_path = report_path.with_suffix(".md")
    md_lines = [
        "# FPGA peak INT8 report",
        "",
        f"- Status: **{report['status']}**",
        f"- Matrix: M={MATRIX_M}, K={MATRIX_K}, N={MATRIX_N}",
        f"- Repeat rounds: {report['benchmark_repeat_count']}",
        f"- Operations: {report['operations']} (multiply and add counted separately)",
        f"- Host correctness: {verified}/{total} 128-bit words",
        f"- ILA all-Tile active cycles: {cycles_text}",
        f"- Clock: {report['frequency_mhz']:.6f} MHz",
        f"- Peak compute: **{tops_text}**",
        "",
    ]
    power = report.get("power")
    if power:
        md_lines.extend([
            "## Power (idle vs repeat)",
            "",
            f"- Idle VCCINT: {_fmt_w(power.get('idle_vccint_w'))} W",
            f"- Repeat VCCINT: {_fmt_w(power.get('repeat_vccint_w'))} W",
            f"- dP VCCINT: {_fmt_w(power.get('delta_vccint_w'))} W",
            f"- dP VCCINT+VCCBRAM: {_fmt_w(power.get('delta_vccint_vccbram_w'))} W",
            f"- Assumed peak: {_fmt_w(power.get('assumed_peak_tops'))} TOPS",
            f"- TOPS/W VCCINT: {_fmt_w(power.get('tops_per_w_vccint'))}",
            f"- TOPS/W VCCINT+VCCBRAM: {_fmt_w(power.get('tops_per_w_vccint_vccbram'))}",
            "",
        ])
    md_lines.extend([
        "TOPS is valid only when every counted sample has peak_compute_mask == 0xff.",
        "Data transfer, loading and writeback cycles are intentionally excluded.",
        "",
        "## Expected ILA evidence",
        "",
        "| Signal | Expected value |",
        "|---|---|",
    ])
    md_lines.extend(
        "| {} | {} |".format(name, value)
        for name, value in report["expected_ila"].items()
    )
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"[report] {report_path}")
    print(f"[report] {md_path}")


def load_previous_report(report_path: Path) -> dict:
    if not report_path.is_file():
        raise FileNotFoundError(
            f"no previous FPGA report at {report_path}; run without --report-only first"
        )
    return json.loads(report_path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run and report the exact-fit 2.048-TOPS INT8 FPGA case"
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--regenerate", action="store_true", help="regenerate test vectors")
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--prepare-only", action="store_true", help="only generate test files")
    execution.add_argument("--view-only", type=Path, metavar="CAPTURE",
                           help="open a saved TOPS .ila file or run directory")
    execution.add_argument("--xdma-only", action="store_true", help=argparse.SUPPRESS)
    execution.add_argument(
        "--report-only",
        action="store_true",
        help="do not touch FPGA; add the observed ILA cycles to the previous report",
    )
    parser.add_argument(
        "--ila-active-cycles",
        type=int,
        default=None,
        help="number of ILA samples for which peak_compute_mask is exactly 0xff",
    )
    parser.add_argument("--frequency-mhz", type=float, default=250.0)
    parser.add_argument(
        "--repeat-count",
        type=int,
        default=None,
        help="seamless 64-job benchmark rounds; 1 selects normal one-shot mode",
    )
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument("--quiet-xdma", action="store_true")
    parser.add_argument("--ltx", type=Path, help="Matching probe file (default: top_tops_boardref.ltx)")
    parser.add_argument("--vivado", help="Path to vivado_lab.bat or vivado.bat")
    parser.add_argument("--hw-server", default="localhost:3121")
    parser.add_argument("--hw-target", default="*")
    parser.add_argument(
        "--staging",
        choices=("hbm", "preload"),
        default="preload",
        help="hbm=Host->HBM->CDMA->IBUF, then tile_obuf drain to HBM "
             "(tops_ila_hbmfix_260813). preload=Host H2C into each tile IBUF.",
    )
    parser.add_argument(
        "--measure-power",
        action="store_true",
        help="sample VCU128 INA226 rails (SC UART / BoardUI protocol) idle vs "
             "repeat, then report the power difference and TOPS/W",
    )
    parser.add_argument("--power-port", default=None, help="SC UART, default FTDI ...D")
    parser.add_argument("--power-idle-s", type=float, default=3.0)
    parser.add_argument("--power-interval", type=float, default=0.35)
    parser.add_argument(
        "--power-rails",
        default="compute",
        help="all / compute (VCCINT+VCCBRAM) / comma-separated rail names",
    )
    return parser.parse_args()


def _power_rail_names(spec: str) -> tuple[str, ...]:
    from vcu128_sc_power import COMPUTE_RAILS, RAILS
    if spec == "all":
        return tuple(item[0] for item in RAILS)
    if spec == "compute":
        return COMPUTE_RAILS
    return tuple(x.strip() for x in spec.split(",") if x.strip())


class PowerSampler:
    """Background INA226 sampler.  Marks split idle vs compute windows."""

    def __init__(self, port: str | None, rails: tuple[str, ...], interval: float):
        from vcu128_sc_power import SysController, find_sc_port, sample_rails, snapshot_dict
        self._sample_rails = sample_rails
        self._snapshot_dict = snapshot_dict
        self.port = find_sc_port(port)
        self.rails = rails
        self.interval = interval
        self.snaps: list[dict] = []
        self.marks: dict[str, float] = {}
        self._stop = threading.Event()
        self._error: str | None = None
        self.sc = SysController(self.port)
        self.sc.enable_pmbus()
        self._thread = threading.Thread(target=self._run, name="vcu128-power", daemon=True)

    def start(self) -> None:
        print(f"[power] sampling {','.join(self.rails)} on {self.port} every {self.interval:.2f}s")
        self._thread.start()

    def mark(self, name: str) -> None:
        self.marks[name] = time.time()
        print(f"[power] mark {name}", flush=True)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        try:
            self.sc.close()
        except Exception:
            pass
        if self._error:
            print(f"[power] sampler error: {self._error}", file=sys.stderr)

    def _run(self) -> None:
        try:
            first = True
            while not self._stop.is_set():
                samples = self._sample_rails(self.sc, self.rails, calibrate=first)
                first = False
                self.snaps.append(self._snapshot_dict(samples))
                self._stop.wait(self.interval)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"

    def _window(self, start_key: str, end_key: str) -> list[dict]:
        t0 = self.marks.get(start_key)
        t1 = self.marks.get(end_key)
        if t0 is None or t1 is None:
            return []
        return [s for s in self.snaps if t0 <= s["t"] <= t1]

    def report(self, peak_tops: float) -> dict:
        from vcu128_sc_power import mean_watts
        idle = self._window("idle_start", "idle_end")
        compute = self._window("compute_start", "compute_end")
        idle_vccint = mean_watts(idle, "vccint_watts")
        idle_compute = mean_watts(idle, "compute_watts")
        run_vccint = mean_watts(compute, "vccint_watts")
        run_compute = mean_watts(compute, "compute_watts")
        dv_int = None if idle_vccint is None or run_vccint is None else run_vccint - idle_vccint
        dv_comp = None if idle_compute is None or run_compute is None else run_compute - idle_compute

        def tops_w(delta: float | None) -> float | None:
            if delta is None or delta <= 0 or peak_tops <= 0:
                return None
            return peak_tops / delta

        out = {
            "port": self.port,
            "rails": list(self.rails),
            "n_idle_samples": len(idle),
            "n_compute_samples": len(compute),
            "idle_vccint_w": idle_vccint,
            "idle_vccint_vccbram_w": idle_compute,
            "repeat_vccint_w": run_vccint,
            "repeat_vccint_vccbram_w": run_compute,
            "delta_vccint_w": dv_int,
            "delta_vccint_vccbram_w": dv_comp,
            "assumed_peak_tops": peak_tops,
            "tops_per_w_vccint": tops_w(dv_int),
            "tops_per_w_vccint_vccbram": tops_w(dv_comp),
        }
        print("\n[power] idle vs repeat")
        print(f"  idle    VCCINT={_fmt_w(idle_vccint)} W   VCCINT+BRAM={_fmt_w(idle_compute)} W  ({len(idle)} samples)")
        print(f"  repeat  VCCINT={_fmt_w(run_vccint)} W   VCCINT+BRAM={_fmt_w(run_compute)} W  ({len(compute)} samples)")
        print(f"  dP      VCCINT={_fmt_w(dv_int)} W   VCCINT+BRAM={_fmt_w(dv_comp)} W")
        print(f"  assumed peak {peak_tops:.3f} TOPS (128 cycles @ clock, no ILA bubbles)")
        if out["tops_per_w_vccint"] is not None:
            print(
                f"  TOPS/W  VCCINT={out['tops_per_w_vccint']:.3f}   "
                f"VCCINT+BRAM={out['tops_per_w_vccint_vccbram']:.3f}"
            )
        else:
            print("  TOPS/W  n/a (need dP > 0 and compute samples during decoder)")
        return out


def open_peak_waveform(vivado: str, path: Path) -> int:
    from fpga.verify_peak import BOARDREF_PROBES, PROBES

    if path.is_dir():
        path = path / "peak_int8.ila"
    with ZipFile(path) as archive:
        configs = [name for name in archive.namelist() if name.lower().endswith(".wcfg")]
        if len(configs) != 1:
            raise ValueError(f"Expected one native wave configuration in {path}")
        config = archive.read(configs[0])
    probes = tuple(PROBES)
    if b"peak_compute_mask" in config:
        prefix = "dcim_array_0_" if b"dcim_array_0_peak_compute_mask" in config else ""
        probes = tuple(prefix + name for name in BOARDREF_PROBES)
    last_sample = 185
    report_path = path.with_name("fpga_peak_report.json")
    if report_path.is_file():
        report = load_previous_report(report_path)
        last_write = report.get("ila_capture", {}).get("last_write_sample")
        if isinstance(last_write, int):
            last_sample = min(1023, max(last_sample, last_write + 4))
    return open_native_waveforms(vivado, {"peak_int8": path}, probes, last_sample=last_sample)


def capture_peak(args: argparse.Namespace) -> int:
    from fpga.verify_peak import verify_peak

    vivado = find_vivado(args.vivado)
    ltx = resolve_ltx(REPO_ROOT, args.ltx or DEFAULT_LTX)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = (args.run_dir or DEFAULT_RUN_DIR / stamp).resolve()
    if run_dir.exists():
        raise FileExistsError(run_dir)
    env = capture_env(sys.executable, ltx, args.hw_server, args.hw_target)
    env.update(DCIM_TOPS_TIMEOUT=str(args.timeout_s), DCIM_TOPS_QUIET=str(int(args.quiet_xdma)),
               DCIM_TOPS_STAGING=args.staging)
    script = FPGA_DIR / "capture.tcl"
    print("Arming TOPS ILA and running the 64-job INT8 workload...", flush=True)
    with tempfile.TemporaryDirectory(prefix="dcim_tops_capture_", ignore_cleanup_errors=True) as scratch:
        work = Path(scratch) / "run"
        env["DCIM_TOPS_CAPTURE_ROOT"] = str(work)
        batch = run_vivado(vivado, script, Path(tempfile.gettempdir()), env, "batch")
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "vivado.log").write_text(batch.stdout, encoding="utf-8")
        for name in ("peak_int8.ila", "peak_int8.csv", "fpga_peak_report.json"):
            path = work / name
            if path.is_file():
                shutil.copyfile(path, run_dir / name)
        capture = run_dir / "peak_int8.ila"
        if not capture.is_file() or not capture.stat().st_size:
            raise ValueError(f"Vivado did not save a TOPS ILA capture. See {run_dir / 'vivado.log'}")
        report = load_previous_report(run_dir / "fpga_peak_report.json")
        try:
            evidence = verify_peak(work, report)
        except (OSError, ValueError) as error:
            report.update(run_dir=str(run_dir), status="FAIL_EVIDENCE", capture_error=str(error))
            (run_dir / "fpga_peak_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            raise ValueError(f"{error}. View the captured samples with --view-only \"{capture}\"") from error
        if batch.returncode:
            raise ValueError(f"Vivado capture did not complete successfully. See {run_dir / 'vivado.log'}")
        cycles = evidence["issue_cycles"]
        tops = calculate_tops(cycles, args.frequency_mhz, OPERATIONS_PER_ROUND)
        report.update(
            run_dir=str(run_dir), status="PASS" if tops >= TOPS_REQUIREMENT else "FAIL_TOPS",
            frequency_mhz=args.frequency_mhz, ila_active_cycles=cycles, calculated_peak_tops=tops,
            meets_2tops=tops >= TOPS_REQUIREMENT, ila_capture=evidence,
            probes_file=str(ltx),
            ila_cycle_qualification="elapsed from first issue through last issue, inclusive",
        )
        report.pop("expected_ila", None)
        (run_dir / "fpga_peak_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"{report['status']} INT8: {evidence['host_words_matched']} words match, "
          f"{cycles} cycles @ {args.frequency_mhz:g} MHz, {tops:.3f} TOPS")
    print(f"Native capture: {capture}", flush=True)
    view_result = open_peak_waveform(vivado, capture)
    return view_result or int(not report["meets_2tops"])


def main() -> int:
    args = parse_args()
    if args.view_only:
        try:
            return open_peak_waveform(find_vivado(args.vivado), args.view_only.resolve())
        except (OSError, RuntimeError, ValueError, BadZipFile, ET.ParseError) as error:
            print(f"TOPS waveform error: {error}", file=sys.stderr)
            return 1

    if not (args.prepare_only or args.report_only or args.xdma_only or args.measure_power
            or args.ila_active_cycles is not None or args.repeat_count not in (None, 1)):
        if os.name != "nt":
            raise SystemExit("the FPGA runner requires Windows and the XDMA driver")
        try:
            return capture_peak(args)
        except (OSError, RuntimeError, ValueError, BadZipFile, ET.ParseError) as error:
            print(f"TOPS test failed: {error}", file=sys.stderr)
            return 1

    run_dir = (args.run_dir or DEFAULT_RUN_DIR).resolve()
    report_path = run_dir / "fpga_peak_report.json"

    if args.report_only:
        if args.ila_active_cycles is None:
            raise SystemExit("--report-only requires --ila-active-cycles from the ILA capture")
        report = load_previous_report(report_path)
        if "ila_capture" in report:
            raise SystemExit("The native capture report already contains the measured ILA cycles")
        repeat_count = int(report.get("benchmark_repeat_count", 1))
        if args.repeat_count is not None and args.repeat_count != repeat_count:
            raise SystemExit("--repeat-count must match the previously generated FPGA run")
    else:
        repeat_count = 1 if args.repeat_count is None else args.repeat_count
        if repeat_count < 1:
            raise SystemExit("--repeat-count must be >= 1")
        prepare_case(run_dir, args.regenerate, repeat_count)
        if args.prepare_only:
            print(f"Prepared FPGA peak case: {run_dir}")
            return 0
        if os.name != "nt":
            raise SystemExit("the FPGA runner requires Windows and the XDMA driver")

        runtime_dir = REPO_ROOT / "project" / "runtime"
        for extra in (runtime_dir, UNIT_TB_DIR):
            if str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        from xdma_win import ChipRunnerWin  # pylint: disable=import-outside-toplevel

        if not args.xdma_only:
            print("[fpga] Arm the ILA before running in manual mode")
        compute_s = (128 * repeat_count) / (args.frequency_mhz * 1e6)
        timeout_s = max(args.timeout_s, compute_s + 30.0)
        print(f"[fpga] compute window ≈ {compute_s:.3f} s  (repeat={repeat_count}, timeout={timeout_s:.1f}s)")
        if args.staging == "hbm":
            print("[fpga] staging=hbm: Host->HBM->CDMA->IBUF, drain tile_obuf->HBM, check HBM")

        sampler = None
        power_report = None
        if args.measure_power:
            sys.path.insert(0, str(FPGA_DIR / "tools"))
            sampler = PowerSampler(
                args.power_port,
                _power_rail_names(args.power_rails),
                args.power_interval,
            )
            sampler.start()
            time.sleep(max(0.2, args.power_interval))

        def on_before_decoder() -> None:
            if sampler is None:
                return
            sampler.mark("idle_start")
            time.sleep(max(0.5, args.power_idle_s))
            sampler.mark("idle_end")

        def on_decoder_start() -> None:
            if sampler is not None:
                sampler.mark("compute_start")

        def on_decoder_done() -> None:
            if sampler is not None:
                sampler.mark("compute_end")

        try:
            results = ChipRunnerWin(verbose=not args.quiet_xdma).run_case(
                run_dir,
                timeout_s=timeout_s,
                staging=args.staging,
                verify=True,
                on_before_decoder=on_before_decoder,
                on_decoder_start=on_decoder_start,
                on_decoder_done=on_decoder_done,
            )
            host_pass = bool(results) and all(bool(item.get("pass")) for item in results)
            error = None
        except Exception as exc:  # preserve a machine-readable failure report
            results = []
            host_pass = False
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if sampler is not None:
                if "compute_end" not in sampler.marks:
                    sampler.mark("compute_end")
                sampler.stop()
                theoretical_tops = (
                    OPERATIONS_PER_ROUND * args.frequency_mhz * 1e6 / 128.0 / 1e12
                )
                power_report = sampler.report(theoretical_tops)

        report = {
            "schema": "edgeyolo.fpga_peak_int8.v1",
            "generated_at": utc_now(),
            "run_dir": str(run_dir),
            "matrix": {"m": MATRIX_M, "k": MATRIX_K, "n": MATRIX_N},
            "benchmark_repeat_count": repeat_count,
            "operations": OPERATIONS_PER_ROUND * repeat_count,
            "host_correctness_pass": host_pass,
            "host_results": results,
            "error": error,
            "expected_ila": {
                **EXPECTED_ILA_BASE,
                "active_span": f"{128 * repeat_count} consecutive cycles with no gap",
                "round_wrap": "job 63 phase 1 -> job 0 phase 0 on adjacent clocks",
            },
        }
        if power_report is not None:
            report["power"] = power_report

    host_pass = bool(report.get("host_correctness_pass"))
    report["updated_at"] = utc_now()
    report["frequency_mhz"] = args.frequency_mhz
    report["ila_active_cycles"] = args.ila_active_cycles
    report["ila_cycle_qualification"] = "count only samples where peak_compute_mask == 0xff"

    if args.ila_active_cycles is None:
        report["calculated_peak_tops"] = None
        report["meets_2tops"] = None
        report["status"] = "PASS_HOST_PENDING_ILA" if host_pass else "FAIL_HOST"
    else:
        tops = calculate_tops(
            args.ila_active_cycles,
            args.frequency_mhz,
            int(report["operations"]),
        )
        report["calculated_peak_tops"] = tops
        report["meets_2tops"] = tops >= TOPS_REQUIREMENT
        if not host_pass:
            report["status"] = "FAIL_HOST"
        elif tops >= TOPS_REQUIREMENT:
            report["status"] = "PASS"
        else:
            report["status"] = "FAIL_TOPS"

    write_report(report_path, report)

    if report["status"] == "PASS_HOST_PENDING_ILA" and not args.xdma_only:
        print("\nHost result PASS. Export/check the ILA waveform, then run:")
        expected_cycles = 128 * int(report.get("benchmark_repeat_count", 1))
        print(
            f'  python "{Path(__file__).resolve()}" --report-only '
            f'--ila-active-cycles {expected_cycles}'
        )
    elif report.get("calculated_peak_tops") is not None:
        print(f"\nCalculated peak: {report['calculated_peak_tops']:.6f} TOPS")
    if report.get("error"):
        print(f"\nFPGA error: {report['error']}", file=sys.stderr)

    return 0 if report["status"] in ("PASS", "PASS_HOST_PENDING_ILA") else 1


if __name__ == "__main__":
    raise SystemExit(main())
