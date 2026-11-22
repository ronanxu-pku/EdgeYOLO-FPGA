"""Prepare an isolated TOPS or Precision build from test-top/project."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess


HERE = Path(__file__).resolve().parent
SOURCE_PATHS = ("rtl", "scripts", "xdc", "Makefile")
ALLOWED = {
    "rtl/chip/DCIM_Array.sv", "rtl/chip/DCIM_Array_bd.v", "rtl/chip/DCIM_Tile.sv",
    "rtl/ref/DCIM/src/dcim/calculate_core.v", "rtl/ref/DCIM/src/dcim/maArray.v",
    "rtl/ref/DCIM/src/dcim/mergeArray.v", "rtl/ref/DCIM/src/dcim/postProcess.v",
    "scripts/chip-lite/2_bd.tcl", "scripts/ip/bd/lite/connect.tcl",
    "scripts/ip/bd/lite/peak_ila.tcl", "scripts/ip/bd/lite/precision_ila.tcl",
}


def content(path):
    return path.read_bytes().replace(b"\r\n", b"\n")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(project, *args, env=None, data=None):
    return subprocess.check_output(["git", "-C", str(project), *args], env=env, input=data)


def audit_connections(before, after):
    for line in difflib.unified_diff(before.splitlines(), after.splitlines()):
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        command = line[1:].strip()
        if not command or command.startswith("#"):
            continue
        if command in ("if {!$precisionIla} {", "if {$precisionIla} {", "} else {", "}"):
            continue
        if command.startswith("connect_bd_net ") and "_ila/" in command:
            continue
        raise ValueError(f"Patch changes a non-ILA BD connection: {command}")


def prepare(profile, project, output):
    project, output = project.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f"Build destination must not exist: {output}")
    if output == project or project in output.parents:
        raise ValueError("Use a build destination outside project/.")
    repo = Path(git(project, "rev-parse", "--show-toplevel").decode().strip())
    prefix = project.relative_to(repo).as_posix()
    paths = [f"{prefix}/{p}" for p in SOURCE_PATHS]
    if git(repo, "status", "--porcelain", "--untracked-files=normal", "--", *paths).strip():
        raise ValueError("Commit or isolate edits in the project source paths before preparing a build.")
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    config = json.loads((HERE / profile / "profile.json").read_text(encoding="utf-8"))
    patch = HERE / profile / "ila.patch"
    touched = set(config["base_sha256"])
    if not touched <= ALLOWED:
        raise ValueError("ILA patch includes a file outside the approved instrumentation paths.")
    for name, expected in config["base_sha256"].items():
        actual = digest(content(project / name)) if (project / name).is_file() else None
        if actual != expected:
            raise ValueError(f"Patch baseline has changed: {name}")
    files = git(repo, "ls-files", "-z", "--", *paths).decode().split("\0")
    original = {}
    for name in filter(None, files):
        rel = Path(name).relative_to(prefix).as_posix()
        original[rel] = content(repo / name)
    output.mkdir(parents=True)
    for name, data in original.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(output.parent))
    patch_data = content(patch)
    stats = git(output, "apply", "--numstat", "-", env=env, data=patch_data).decode().splitlines()
    if {line.split("\t", 2)[2] for line in stats} != touched:
        raise ValueError("Patch file list differs from its profile manifest.")
    git(output, "apply", "--check", "-", env=env, data=patch_data)
    git(output, "apply", "-", env=env, data=patch_data)
    for name, data in original.items():
        if name not in touched and content(output / name) != data:
            raise ValueError(f"Unexpected change outside ILA instrumentation: {name}")
    connection = "scripts/ip/bd/lite/connect.tcl"
    audit_connections(original[connection].decode("utf-8-sig"),
                      content(output / connection).decode("utf-8-sig"))
    hashes = {name: digest(content(output / name)) for name in sorted(set(original) | touched)}
    manifest = {"profile": profile, "source_commit": commit, "source_directory": prefix,
                "patch_sha256": digest(content(patch)), "files_sha256": hashes,
                "ila_core": config["ila_core"], "probe_widths": config["probe_widths"],
                "board_validation": "pending"}
    (output / "ila_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    launcher = (HERE / "build.sh").read_text(encoding="utf-8")
    launcher = launcher.replace("@PROFILE@", profile).replace("@COMMIT@", commit)
    (output / "build_ila.sh").write_text(launcher, encoding="utf-8", newline="\n")
    print(f"Prepared {profile}: {output}")
    print(f"Source {commit}; patch SHA256 {manifest['patch_sha256']}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("tops", "precision"))
    parser.add_argument("--project", type=Path, default=HERE.parent)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.profile, args.project, args.output)


if __name__ == "__main__":
    main()
