"""Offline capture/export checks; all hardware and GUI entry points are mocked."""
import argparse
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "test"))
sys.path.insert(0, str(ROOT / "test/tops"))
from utils.ila import ila_runtime, results
from fpga import verify_peak


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


precision = module("precision_entry", "test/precision/run.py")
precision_check = module("precision_checker", "test/precision/check_capture.py")
tops = module("tops_entry", "test/tops/run.py")


def dump(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def precision_fixture(folder, *, mismatch=False, archive=True):
    case = precision.prepare_case("int8", folder)
    words = [int(line, 16) for line in (folder / "expected.hex").read_text().splitlines()]
    data = b"".join(word.to_bytes(16, "little") for word in words)
    if mismatch:
        data = bytes([data[0] ^ 1]) + data[1:]
    (folder / "fpga_got.bin").write_bytes(data)
    actual = int.from_bytes(data[:4], "little", signed=True)
    dump(folder / "readback.json", {**case, "actual": actual,
         "actual_hex": f"0x{int.from_bytes(data[:4], 'little'):08X}", "pass": not mismatch})
    names = ["prec_" + name for name in precision_check.PROBES]
    rows = [[0] * len(names) for _ in range(64)]
    # Known -93 * 109 example, independent of the CSV checker implementation.
    rows[32][:3] = [1, 0xA, 0x6D]
    rows[33][:3] = [1, 0x3, 0x6D]
    rows[34][0], rows[34][3] = 2, 0xDCB2
    rows[35][0], rows[35][3] = 2, 0x1227
    rows[40][0], rows[40][4] = 4, (0x3FDC << 14) | 0x3FB2
    rows[41][0], rows[41][4] = 4, (0x12 << 14) | 0x27
    rows[43][0], rows[43][5] = 8, (1 << 48) - 654
    rows[44][0], rows[44][5] = 8, (1 << 48) - 10137
    rows[48][0], rows[48][6] = 16, (1 << 64) - 10137
    with (folder / "precision.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(names)
        writer.writerows([[f"{value:X}" for value in row] for row in rows])
    with patch.object(sys, "argv", ["check_capture.py", str(folder)]):
        precision_check.main()
    if archive:
        (folder / "capture.ila").write_bytes(b"offline precision capture")


def tops_fixture(folder, *, corrupt_tile=False, host_fail=False):
    folder.mkdir(parents=True)
    (folder / "expected.hex").write_text(("0" * 32 + "\n") * 2048)
    raw = bytearray(2048 * 16)
    if host_fail:
        raw[0] = 1
    (folder / "fpga_got.bin").write_bytes(raw)
    np.savez(folder / "operands.npz", act=np.zeros((64, 64), dtype=np.int8),
             weight=np.zeros((128, 64), dtype=np.int8), golden=np.zeros((64, 128), dtype=np.int32))
    dump(folder / "host_report.json", {
        "host_correctness_pass": not host_fail,
        "host_results": [{"pass": not host_fail, "failed": int(host_fail),
                          "passed": 2048 - int(host_fail), "total_words": 2048}],
        "operations": tops.OPERATIONS_PER_ROUND, "benchmark_repeat_count": 1,
    })
    names = [*verify_peak.BOARDREF_PROBES, *verify_peak.ALL_TILE_PROBES]
    with (folder / "peak_int8.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Sample in Buffer", *names])
        for sample in range(192):
            row = dict.fromkeys(names, 0)
            if 32 <= sample < 160:
                row.update(peak_compute_mask=255, peak_job=(sample - 32) // 2, peak_phase=(sample - 32) % 2)
            if 42 <= sample < 170:
                row["peak_ma_fire_mask"] = 255
            if 48 <= sample <= 174 and sample % 2 == 0:
                row.update(peak_result_valid=1, peak_result_valid_mask=255)
                if corrupt_tile and sample == 48:
                    row["peak_tile7_result_ch0"] = 1
            writer.writerow([sample, *[f"{row[name]:X}" for name in names]])
    with ZipFile(folder / "peak_int8.ila", "w") as archive:
        archive.writestr("native.wcfg", '<wave_config><wvobject fp_name="issue_mask"/></wave_config>')
        archive.writestr("waveform.wdb", b"original samples")
        archive.write(folder / "peak_int8.csv", "waveform.csv")


class CaptureResultTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "results with spaces"
        self.args = argparse.Namespace(vivado=None, ltx=None, hw_server="localhost:3121", hw_target="*",
                                      timeout_s=60, quiet_xdma=True, seed=None, run_dir=self.output,
                                      staging="preload", frequency_mhz=250)
        self.stdout = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.stdout)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def precision_run(self, *, mismatch=False, archive=True, returncode=0, cases=("int8",)):
        def capture(vivado, script, cwd, env, mode):
            self.assertTrue((script.parent.parent / "utils/ila/ila_common.tcl").is_file())
            precision_fixture(Path(env["DCIM_PRECISION_CAPTURE_ROOT"]) / "int8", mismatch=mismatch, archive=archive)
            return subprocess.CompletedProcess([], returncode, "offline precision log")
        with patch.object(precision, "find_vivado", return_value="mock"), \
             patch.object(precision, "run_vivado", side_effect=capture), \
             patch.object(precision, "open_precision_waveforms", return_value=0):
            return precision.capture_on_board(self.args, cases, self.output)

    def tops_run(self, *, corrupt_tile=False, host_fail=False, returncode=0):
        def capture(vivado, script, cwd, env, mode):
            if script.name == "view.tcl":
                self.assertTrue((self.output / "result.json").is_file())
                self.assertEqual(env["DCIM_TOPS_VIEW_WINDOW"], "48 80")
                (cwd / "marked.wcfg").write_text(
                    '<wave_config><wave_markers><marker time="48" label="OUT16_START"/>'
                    '<marker time="80" label="OUT16_END"/></wave_markers></wave_config>')
                return subprocess.CompletedProcess([], 0, "offline marker log")
            tops_fixture(Path(env["DCIM_TOPS_CAPTURE_ROOT"]), corrupt_tile=corrupt_tile, host_fail=host_fail)
            return subprocess.CompletedProcess([], returncode, "offline tops log")
        with patch.object(tops, "find_vivado", return_value="mock"), \
             patch.object(tops, "run_vivado", side_effect=capture), \
             patch.object(tops, "open_peak_waveform", return_value=0):
            return tops.capture_peak(self.args)

    def report(self):
        return results.load_json(self.output / "result.json")

    def test_precision_exports_complete_results_and_absolute_path(self):
        self.assertEqual(self.precision_run(), 0)
        report = self.report()
        item = report["cases"]["int8"]
        self.assertTrue(report["pass"])
        self.assertEqual(item["ila_check"]["actual"], -10137)
        self.assertEqual(item["comparison"]["channel0"], {"fpga_signed": -10137, "golden_signed": -10137})
        self.assertEqual(item["comparison"]["fpga_words_hex"], item["comparison"]["golden_words_hex"])
        self.assertTrue(item["samples_csv"])
        self.assertEqual({p.name for p in self.output.iterdir()}, {"int8.ila", "result.json"})
        self.assertIn(f"result dir: {self.output.resolve()}", self.stdout.getvalue())

    def test_precision_overwrite_failure_removes_previous_waveforms(self):
        self.precision_run()
        (self.output / "int16.ila").write_bytes(b"stale")
        (self.output / "note.txt").write_text("keep")
        self.assertEqual(self.precision_run(archive=False, returncode=1), 1)
        self.assertFalse(self.report()["pass"])
        self.assertEqual({p.name for p in self.output.iterdir()}, {"result.json", "note.txt"})

    def test_precision_mismatch_keeps_actual_golden_and_failed_ila_check(self):
        self.assertEqual(self.precision_run(mismatch=True, returncode=1), 1)
        report = self.report()
        item = report["cases"]["int8"]
        self.assertEqual(report["result"], "different")
        self.assertEqual(item["comparison"]["mismatched_word_indices"], [0])
        self.assertNotEqual(item["comparison"]["channel0"]["fpga_signed"],
                            item["comparison"]["channel0"]["golden_signed"])
        self.assertFalse(item["ila_check"]["pass"])
        self.assertTrue((self.output / "int8.ila").is_file())

    def test_precision_missing_requested_case_is_not_pass(self):
        self.assertEqual(self.precision_run(cases=("int8", "int16")), 1)
        self.assertFalse(self.report()["cases"]["int16"]["pass"])

    def test_tops_exports_all_2048_words_and_512_ila_comparisons(self):
        self.assertEqual(self.tops_run(), 0)
        report = self.report()
        self.assertTrue(report["pass"])
        self.assertEqual(len(report["comparison"]["fpga_words_hex"]), 2048)
        self.assertEqual(report["ila_capture"]["all_tile_ch0_results_checked"], 512)
        self.assertEqual(report["calculated_peak_tops"], 2.048)
        self.assertNotIn("output_window", report)
        self.assertNotIn("output_window", report["ila_capture"])
        self.assertEqual(report["operations"], 1048576)
        self.assertEqual(report["ila_active_cycles"], 128)
        self.assertEqual(report["vivado_log"], "offline tops log")
        self.assertIn("262,144 OP in 32 cycles (128 ns at 250 MHz), 2.048 TOPS", self.stdout.getvalue())
        with ZipFile(self.output / "peak_int8.ila") as archive:
            self.assertEqual(archive.read("waveform.wdb"), b"original samples")
            markers = ET.fromstring(archive.read("native.wcfg")).findall("wave_markers/marker")
            self.assertEqual([(m.get("label"), m.get("time")) for m in markers],
                             [("OUT16_START", "48"), ("OUT16_END", "80")])
        self.assertEqual({p.name for p in self.output.iterdir()}, {"result.json", "peak_int8.ila"})
        self.assertIn(f"result dir: {self.output.resolve()}", self.stdout.getvalue())

    def test_tops_ila_corruption_does_not_pass_even_when_host_matches(self):
        self.assertEqual(self.tops_run(corrupt_tile=True), 1)
        report = self.report()
        self.assertTrue(report["comparison"]["match"])
        self.assertFalse(report["pass"])
        self.assertIn("Tile 7", report["error"])
        self.assertTrue(report["samples_csv"])

    def test_output_window_needs_both_boundaries_and_full_pipeline_overlap(self):
        issues = [(s,) for s in range(100)]
        ma = [(s,) for s in range(100) if s != 13]
        writes = [(s,) for s in range(10, 66, 2)]
        window = verify_peak.output_window(False, (issues, ma, writes))
        self.assertEqual((window["start_sample"], window["end_sample_exclusive"]), (14, 46))
        with self.assertRaisesRegex(ValueError, "16 consecutive"):
            verify_peak.output_window(False, (issues, ma, writes[-16:]))
        with self.assertRaisesRegex(ValueError, "Tile 0 alone"):
            verify_peak.output_window(True, (issues, writes))

    def test_tops_host_mismatch_is_saved_in_json(self):
        self.assertEqual(self.tops_run(host_fail=True), 1)
        self.assertEqual(self.report()["comparison"]["mismatched_word_indices"], [0])

    def test_tops_failed_capture_overwrites_previous_pass(self):
        self.tops_run()
        with patch.object(tops, "find_vivado", side_effect=FileNotFoundError("no Vivado")):
            self.assertEqual(tops.capture_peak(self.args), 1)
        self.assertEqual(self.report()["status"], "FAIL_CAPTURE")
        self.assertEqual({p.name for p in self.output.iterdir()}, {"result.json"})

    def test_tops_nonzero_vivado_exit_cannot_pass(self):
        self.assertEqual(self.tops_run(returncode=1), 1)
        self.assertFalse(self.report()["pass"])
        self.assertEqual(self.report()["vivado_returncode"], 1)

    def test_tops_correct_data_below_threshold_is_not_overall_pass(self):
        self.args.frequency_mhz = 100
        self.assertEqual(self.tops_run(), 1)
        self.assertEqual(self.report()["result"], "same")
        self.assertEqual(self.report()["status"], "FAIL_TOPS")

    def test_default_precision_output_is_timestamp_subdirectory(self):
        seen = []
        def capture(args, cases, folder):
            seen.append(folder)
            return 0
        with patch.object(sys, "argv", ["run.py"]), \
             patch.object(precision, "capture_on_board", side_effect=capture):
            self.assertEqual(precision.main(), 0)
        self.assertEqual(seen[0].parent, ROOT / "test/precision/output")
        self.assertRegex(seen[0].name, r"^\d{8}_\d{6}_\d{6}$")

    def test_view_only_detects_single_saved_precision(self):
        self.precision_run()
        with patch.object(sys, "argv", ["run.py", "--view-only", str(self.output)]), \
             patch.object(precision, "find_vivado", return_value="mock"), \
             patch.object(precision, "open_precision_waveforms", return_value=0) as view:
            self.assertEqual(precision.main(), 0)
        self.assertEqual(view.call_args.args[-1], ("int8",))

    def test_tops_manual_run_exports_generated_golden_and_actual_readback(self):
        class Runner:
            def __init__(self, **kwargs):
                self.x = SimpleNamespace(close=lambda: None)

            def run_case(self, folder, **kwargs):
                expected = [int(line, 16) for line in (folder / "expected.hex").read_text().splitlines()]
                (folder / "fpga_got.bin").write_bytes(b"".join(word.to_bytes(16, "little") for word in expected))
                return [{"pass": True, "failed": 0, "passed": len(expected), "total_words": len(expected)}]

        with patch.object(sys, "argv", ["run.py", "--repeat-count", "2", "--run-dir", str(self.output)]), \
             patch.dict(sys.modules, {"xdma_win": SimpleNamespace(ChipRunnerWin=Runner)}):
            self.assertEqual(tops.main(), 0)
        report = self.report()
        self.assertEqual(report["status"], "PASS_HOST_PENDING_ILA")
        self.assertFalse(report["pass"])
        self.assertEqual(len(report["comparison"]["fpga_words_hex"]), 2048)
        self.assertEqual({p.name for p in self.output.iterdir()}, {"result.json"})
        with patch.object(sys, "argv", ["run.py", "--report-only", "--run-dir", str(self.output),
                                        "--ila-active-cycles", "256"]):
            self.assertEqual(tops.main(), 0)
        self.assertEqual(self.report()["calculated_peak_tops"], 2.048)
        self.assertIn("manual input", self.report()["ila_cycle_qualification"])

    def test_short_zero_readback_cannot_match_zero_golden(self):
        self.output.mkdir()
        (self.output / "expected.hex").write_text("0" * 32 + "\n")
        (self.output / "fpga_got.bin").write_bytes(b"\0" * 15)
        self.assertFalse(results.readback_comparison(self.output)["match"])

    def test_native_view_uses_relocated_tcl(self):
        self.output.mkdir()
        archive = self.output / "sample.ila"
        with ZipFile(archive, "w") as z:
            z.writestr("sample.wcfg", '<wave_config><wvobject fp_name="prec_act_slice"/></wave_config>')
        def view(vivado, script, cwd, env, mode):
            self.assertEqual(script, ROOT / "test/utils/ila/view.tcl")
            self.assertTrue(script.is_file())
            self.assertEqual(mode, "gui")
            return subprocess.CompletedProcess([], 0, "")
        with patch.object(ila_runtime, "run_vivado", side_effect=view):
            self.assertEqual(ila_runtime.open_native_waveforms("mock", {"int8": archive}, ("prec_act_slice",), 54), 0)


if __name__ == "__main__":
    unittest.main()
