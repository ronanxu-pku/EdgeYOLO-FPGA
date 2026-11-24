"""Vivado launcher and native waveform views shared by the DCIM board tests."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile


def parse_bus(value, radix, width):
    text = value.strip().replace("_", "")
    match = re.fullmatch(r"(?:\d+)?'([hbdo])([0-9a-fA-F]+)", text)
    if match:
        return int(match[2], {"h": 16, "b": 2, "d": 10, "o": 8}[match[1]])
    base = {"BIN": 2, "BINARY": 2, "OCT": 8, "SIGNED": 10, "UNSIGNED": 10, "DEC": 10}.get(radix, 16)
    prefix = text[:2].lower()
    # In a HEX column, "0b..." can be an ordinary hexadecimal value.
    if prefix == "0x" or (prefix == "0b" and base != 16):
        return int(text, 0)
    return int(text, base) & ((1 << width) - 1)


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


def native_wave_config(capture: Path, destination: Path, probes: tuple[str, ...], last_sample: int) -> None:
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
            if probe in probes:
                rows[probe] = wave
                break
    if len(rows) != len(probes):
        missing = ", ".join(probe for probe in probes if probe not in rows)
        raise ValueError(f"Missing native waveform rows in {capture}: {missing}")

    for wave in config.findall("wvobject"):
        config.remove(wave)
    for probe in probes:
        config.append(rows[probe])
    size = config.find("WVObjectSize")
    if size is not None:
        size.set("size", str(len(probes)))

    # Native ILA wave configs use one fs per sample when the full window ends at 1025 fs.
    zoom_end = config.find("zoom_setting/ZoomEndTime")
    if zoom_end is not None and zoom_end.get("time") in {"1023fs", "1024fs", "1025fs"}:
        for element, sample in (("ZoomStartTime", 28), ("ZoomEndTime", last_sample), ("Cursor1Time", 32)):
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
    options = dict(executable=executable, cwd=cwd, env=env, stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, text=True, errors="replace")
    if mode != "batch":
        return subprocess.run(command, **options)

    lines = []
    with subprocess.Popen(command, **options) as process:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)
            if line.startswith("DCIM_CAPTURE "):
                print(line.removeprefix("DCIM_CAPTURE ").rstrip(), flush=True)
        return subprocess.CompletedProcess(command, process.wait(), "".join(lines))


def resolve_ltx(repo: Path, override: Path | None) -> Path:
    ltx = override if override is not None else repo / "bitstream" / "top.ltx"
    if not ltx.is_file():
        raise FileNotFoundError(ltx)
    return ltx.resolve()


def capture_env(python: str, ltx: Path, server: str, target: str) -> dict[str, str]:
    env = dict(os.environ, DCIM_ILA_PYTHON=python, DCIM_ILA_LTX=str(ltx),
               DCIM_ILA_HW_SERVER=server, DCIM_ILA_HW_TARGET=target)
    for name in ("PATH", "PYTHONHOME", "PYTHONPATH"):
        env[f"DCIM_ILA_HOST_{name}"] = os.environ.get(name, "")
    return env


def open_native_waveforms(vivado: str, captures: dict[str, Path], probes: tuple[str, ...], last_sample: int) -> int:
    with tempfile.TemporaryDirectory(prefix="dcim_ila_view_", ignore_cleanup_errors=True) as scratch:
        work = Path(scratch)
        for name, path in captures.items():
            if not path.is_file():
                raise FileNotFoundError(f"No native ILA archive at {path}")
            native_wave_config(path, work / f"{name}.wcfg", probes, last_sample)
            shutil.copyfile(path, work / f"{name}.ila")
        env = dict(os.environ, DCIM_PRECISION_VIEW_CASES=" ".join(captures), DCIM_PRECISION_VIEW_ROOT=str(work))
        script = Path(__file__).with_name("view.tcl")
        opened = run_vivado(vivado, script, Path(tempfile.gettempdir()), env, "gui")
        error_path = work / "view_error.txt"
        if error_path.is_file():
            raise RuntimeError(error_path.read_text(encoding="utf-8", errors="replace").strip())
        if opened.returncode:
            print("\n".join(opened.stdout.splitlines()[-20:]), file=sys.stderr)
        return opened.returncode
