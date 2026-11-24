"""Persist one capture's ILA archives and its complete comparison report."""
from datetime import datetime
import json
from pathlib import Path


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace") if path.is_file() else ""


def readback_comparison(folder: Path) -> dict:
    """Use the exact bytes saved by ChipRunnerWin.read_check, in golden word order."""
    expected_path, actual_path = folder / "expected.hex", folder / "fpga_got.bin"
    if not expected_path.is_file() or not actual_path.is_file():
        return {"match": None, "error": "Golden or FPGA readback is unavailable"}
    expected = [int(line.strip(), 16) for line in expected_path.read_text().splitlines()
                if line.strip() and not line.strip().startswith(("//", "@"))]
    raw = actual_path.read_bytes()
    actual = [int.from_bytes(raw[i:i + 16], "little") for i in range(0, len(raw), 16)]
    mismatches = [i for i in range(max(len(expected), len(actual)))
                  if i >= len(expected) or i >= len(actual) or expected[i] != actual[i]]
    complete = bool(expected) and len(raw) == len(expected) * 16
    return {
        "word_bits": 128,
        "byte_order": "little",
        "word_order": "checks.txt / expected.hex order (pixel, tile, intra-tile word)",
        "golden_words_hex": [f"0x{value:032X}" for value in expected],
        "fpga_words_hex": [f"0x{value:032X}" for value in actual],
        "golden_word_count": len(expected),
        "fpga_byte_count": len(raw),
        "mismatched_word_indices": mismatches,
        "complete": complete,
        "match": complete and not mismatches,
    }


def write_result(run_dir: Path, report: dict, *, announce: bool = True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    report["run_dir"] = str(run_dir.resolve())
    temporary = run_dir / "result.json.tmp"
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(run_dir / "result.json")
    if announce:
        print(f"result dir: {run_dir.resolve()}", flush=True)


def start_result(run_dir: Path, task: str, artifacts: tuple[str, ...]) -> dict:
    """Replace this test's known files; leave unrelated files and subdirectories alone."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in artifacts:
        if Path(name).name != name:
            raise ValueError(f"Expected an output filename, got {name}")
        path = run_dir / name
        if path.is_file() or path.is_symlink():
            path.unlink()
    report = {
        "schema": f"edgeyolo.{task}.v2",
        "task": task,
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": "RUNNING", "pass": False, "result": "incomplete",
    }
    write_result(run_dir, report, announce=False)
    return report
