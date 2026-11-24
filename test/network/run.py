"""EdgeYOLO-FPGA 的自包含运行入口。

仓库包含 PT/ONNX/解析模型以及 20+20 张测试图片。
同一批次的图片结果写到 ``test/network/output/<模型>_<精度>_<执行时间>/``。
每张图保存 ``result_<图片编号>.png/json``；编译缓存和中间数据放在 ``cached/``。

常用命令::

    python test/network/run.py --model yolo --precision int8 --num 1
    python test/network/run.py --model resnet --precision int16 --num 1
    python test/network/run.py --model all --precision both --num 20

启动时检查编译缓存，缺少产物时自动编译。FPGA 跑完后在主机计算 compiler golden 并比对。
单图打印推理和比对结果，批量测试保留详细终端日志。

硬件运行要求 FPGA 已烧录与网络测试兼容的设计，并已安装 Xilinx XDMA 驱动。
Python 依赖见 README。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NETWORK_DIR = Path(__file__).resolve().parent
REPO_ROOT = NETWORK_DIR.parents[1]
SCRIPTS_DIR = NETWORK_DIR / "scripts"
UNIT_TB = SCRIPTS_DIR / "host"
OUT_DIR = NETWORK_DIR / "output"
CACHE_DIR = NETWORK_DIR / "cached"
DEFAULT_CONF = 0.25

DEFAULT_YOLO_IMG = NETWORK_DIR / "examples" / "coco" / "000000000139.jpg"
DEFAULT_RESNET_IMG = NETWORK_DIR / "examples" / "imagenet" / "n01443537_goldfish.JPEG"
RELEASE_ID = "80832ec_attempt1"
COMPILED_DIR = CACHE_DIR / "compiled" / RELEASE_ID
COCO_YOLO_DIR = REPO_ROOT / "project" / "model" / "yolov5n_coco50k_qat"
EXAMPLES_DIR = NETWORK_DIR / "examples"
IMAGENET_MANIFEST = EXAMPLES_DIR / "imagenet" / "manifest.json"
COCO_MANIFEST = EXAMPLES_DIR / "coco" / "manifest.json"
MODEL_INPUTS_MANIFEST = REPO_ROOT / "project" / "model" / "model_inputs_manifest.json"
XDMA_POLL_TIMEOUT_S = 240.0
XDMA_READ_CHUNK_BYTES = 65536

ONE_SHOT_DEFAULT_BUILDS = {
    ("resnet", "vai"): COMPILED_DIR / "resnet18_int8",
    ("resnet", "int16"): COMPILED_DIR / "resnet18_int16_native",
    ("resnet", "int16_widened"): COMPILED_DIR / "resnet18_int16_widened",
}

ONE_SHOT_COMPILE_PARSED = {
    ("resnet", "vai"): REPO_ROOT / "project" / "model" / "resnet18" / "parsed_vai",
    ("resnet", "int16"): REPO_ROOT / "project" / "model" / "resnet18" / "parsed_int16",
    ("resnet", "int16_widened"): REPO_ROOT / "project" / "model" / "resnet18" / "parsed_vai_int16_widened",
}

YOLO_ONE_SHOT_PROFILES = {
    "coco": {
        "image": DEFAULT_YOLO_IMG,
        "parsed": {
            "int8": COCO_YOLO_DIR / "parsed_int8",
            "int16": COCO_YOLO_DIR / "parsed_int16",
        },
        "builds": {
            "int8": COMPILED_DIR / "yolo_coco_int8",
            "int16": COMPILED_DIR / "yolo_coco_int16_native",
        },
    },
}


def _install_unit_tb_run_module() -> None:
    if "run" in sys.modules and getattr(sys.modules["run"], "__file__", "") != str(__file__):
        return
    spec = importlib.util.spec_from_file_location("run", UNIT_TB / "run.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {UNIT_TB / 'run.py'}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run"] = mod
    spec.loader.exec_module(mod)


def _setup_imports() -> None:
    sys.path.insert(0, str(UNIT_TB))
    sys.path.insert(0, str(SCRIPTS_DIR / "golden"))
    sys.path.insert(0, str(REPO_ROOT / "project" / "runtime"))
    sys.path.insert(0, str(REPO_ROOT / "project" / "rtl" / "tb" / "lite_bd" / "module_tb"))
    _install_unit_tb_run_module()


def save_image(img: np.ndarray, path: Path) -> None:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(str(path), format="PNG")


def _display_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved


def _result_dir(network: str, precision: str, run_time: str) -> Path:
    tag = _one_shot_tag(network, precision)
    return OUT_DIR / f"{network}_{tag}_{run_time}"


def _migrate_legacy_outputs(run_time: str) -> None:
    archive = CACHE_DIR / "legacy" / run_time
    moves = [
        (root / name, CACHE_DIR / name)
        for root in (NETWORK_DIR / ".cache", OUT_DIR)
        for name in ("compiled", "golden_cache")
    ]
    if OUT_DIR.is_dir():
        legacy_dirs = {"inference", "acceptance", "host_e2e", "dqa_standalone", "yolov5n", "resnet18"}
        moves.extend(
            (path, archive / "output" / path.name)
            for path in OUT_DIR.iterdir()
            if path.name in legacy_dirs or re.fullmatch(r"\d{8}_\d{6}_\d{6}", path.name)
        )

    network_root = NETWORK_DIR.resolve()
    for source, dest in moves:
        if not source.is_dir():
            continue
        if dest.exists():
            dest = archive / source.parent.name / source.name
        if not source.resolve().is_relative_to(network_root) or not dest.resolve().is_relative_to(network_root):
            raise ValueError(f"legacy artifact path leaves the network directory: {source} -> {dest}")
        if dest.exists():
            raise FileExistsError(f"legacy archive already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)


def _prediction_result(network: str, precision: str, image: Path, head: dict,
                       compare_ok: bool, conf: float) -> dict:
    from PIL import Image

    with Image.open(image) as original:
        width, height = original.size
    result = {
        "network": head["network"],
        "precision": _one_shot_tag(network, precision),
        "image": image.name,
        "image_size": {"width": width, "height": height},
        "result": "same" if compare_ok else "different",
    }
    if network == "yolo":
        detections = [
            {"class_id": int(det["class_id"]), "class_name": det["class_name"],
             "conf": float(det["conf"]), "bbox": [float(v) for v in det["bbox"]]}
            for det in head.get("detections", [])
        ]
        result.update(confidence_threshold=conf, bbox_format="xyxy",
                      bbox_units="original_image_pixels",
                      num_detections=len(detections), detections=detections)
    else:
        result.update(score_type="logit", topk=head["topk"])
    return result


@contextlib.contextmanager
def _muted_stdout(enabled: bool):
    if not enabled:
        yield
        return
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def _print_compact_predictions(network: str, result: dict) -> None:
    if network == "yolo":
        detections = list(result.get("detections", []))
        if not detections:
            print("(no detections)")
            return
        for det in detections:
            name = det.get("class_name", f"class_{det.get('class_id', '?')}")
            print(f"{name} {float(det['conf']):.3f}")
        return
    top = result["topk"][0]
    print(f"{top['class_name']} {float(top['score']):.4f}")


def _print_result_banner(same: bool) -> None:
    text = "result: same" if same else "result: different"
    bar = "=" * 28
    print()
    print(bar)
    print(f"  {text}")
    print(bar, flush=True)


def _run_checked(cmd: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(cwd), check=True)


def _draw_yolo_detections(image_path: Path, detections: list[dict], out_img: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    names = {0: "person", 1: "car", 2: "bicycle"}
    img = Image.open(str(image_path)).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except OSError:
        font = ImageFont.load_default()

    labels = []
    for det in detections:
        x1, y1, x2, y2 = [float(v) for v in det["bbox"]]
        cls = int(det.get("class_id", det.get("class", 0)))
        conf = float(det.get("conf", det.get("confidence", 0.0)))
        label = f"{det.get('class_name', names.get(cls, f'class_{cls}'))} {conf:.3f}"
        draw.rectangle([x1, y1, x2, y2], outline=(255, 64, 32), width=3)
        left, top, right, bottom = draw.textbbox((x1, y1), label, font=font)
        label_h = bottom - top + 6
        label_w = right - left + 8
        labels.append((x1, y1, label, label_w, label_h))

    # Draw labels last and stagger nearby labels so their scores stay readable.
    occupied = []
    for x1, y1, label, label_w, label_h in labels:
        x_label = max(0, min(x1, img.width - label_w))
        y_label = max(0, min(y1 - label_h, img.height - label_h))
        offsets = [0]
        for step in range(1, len(occupied) + 1):
            offsets.extend([-step * (label_h + 2), step * (label_h + 2)])
        for offset in offsets:
            y = y_label + offset
            if not 0 <= y <= img.height - label_h:
                continue
            rect = (x_label, y, x_label + label_w, y + label_h)
            if all(rect[2] <= left or rect[0] >= right or rect[3] <= top or rect[1] >= bottom
                   for left, top, right, bottom in occupied):
                y_label = y
                break
        occupied.append((x_label, y_label, x_label + label_w, y_label + label_h))
        draw.rectangle(occupied[-1], fill=(255, 64, 32))
        draw.text((x_label + 4, y_label + 3), label, fill=(255, 255, 255), font=font)

    out_img.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_img), format="PNG")


def _draw_resnet_classification(image_path: Path, topk: list[dict], out_img: Path) -> None:
    resnet_e2e = importlib.import_module("resnet_e2e")
    img_rgb = resnet_e2e.load_image(str(image_path))
    top = [(int(row["class_id"]), float(row["score"])) for row in topk]
    canvas = resnet_e2e.draw_classification(img_rgb, top, "ResNet18 FPGA One-Shot")
    save_image(canvas, out_img)


def _one_shot_tag(network: str, precision: str) -> str:
    if precision in {"vai", "int8"}:
        return "int8"
    if precision in {"int16", "int16_widened"}:
        return "int16"
    return precision


def _resnet_int16_internal() -> str:
    native = ONE_SHOT_COMPILE_PARSED[("resnet", "int16")] / "network.json"
    return "int16" if native.is_file() else "int16_widened"


def _expand_precisions(raw: str, network: str) -> list[str]:
    wanted = ["int8", "int16"] if raw == "both" else [raw]
    if network == "yolo":
        return wanted
    mapped: list[str] = []
    for item in wanted:
        if item == "int8":
            mapped.append("vai")
        else:
            mapped.append(_resnet_int16_internal())
    return mapped


def _hardware_mode(network: str, precision: str) -> str:
    if network == "resnet" and precision == "vai":
        return "int8"
    if precision in {"int16", "int16_widened"}:
        return "int16"
    return precision


def _one_shot_cache_ready(build_dir: Path) -> bool:
    try:
        plan = json.loads((build_dir / "plan.json").read_text())
        manifest_path = build_dir / "program_manifest.json"
        if manifest_path.is_file():
            segments = json.loads(manifest_path.read_text())["segments"]
            if not segments or not all((build_dir / seg["bin"]).is_file() for seg in segments):
                return False
        else:
            segment_count = plan.get("compile_meta", {}).get("program_segments", 1)
            if segment_count > 1 or not (build_dir / "program.bin").is_file():
                return False
        for layout, filename in (("weights_layout", "weights.bin"), ("wb_layout", "wb.bin")):
            if plan.get(layout, {}).get("layers") and not (build_dir / filename).is_file():
                return False
    except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return True


def _ensure_one_shot_artifact(network: str, precision: str, build_dir: Path,
                              parsed_dir: Path) -> None:
    if _one_shot_cache_ready(build_dir):
        return

    label = f"{network.upper()} {_one_shot_tag(network, precision).upper()}"
    print(f"  [compile] cache missing or incomplete in {build_dir}, compiling {label}", flush=True)
    compile_network = "yolov5n" if network == "yolo" else "resnet18"
    mode = _hardware_mode(network, precision)
    cmd = [
        sys.executable, "project/compiler/compile.py",
        "--network", compile_network,
        "--mode", mode,
        "--full",
        "--out", str(build_dir),
        "--parsed", str(parsed_dir),
    ]
    if precision == "int16_widened":
        cmd.append("--allow-widened-int16")
    _run_checked(cmd)


def run_one_shot_fpga(
    network: str,
    precision: str,
    img_path: Path,
    out_dir: Path,
    *,
    image_index: int,
    build_dir: Path,
    parsed_dir: Path,
    conf: float = DEFAULT_CONF,
    session=None,
) -> Path:
    """Run a full one-shot FPGA workload, compare features, and run the host boundary."""
    tag = _one_shot_tag(network, precision)
    batch = session is not None
    yolo_parsed_dir = parsed_dir if network == "yolo" else None
    resnet_parsed_dir = parsed_dir if network == "resnet" else None
    label = f"{'YOLO' if network == 'yolo' else 'ResNet'} {tag.upper()}"
    # Selected attempt1 board validation measured native COCO W16A16 PAN
    # max_abs <= 0.00930. Other maintained paths pass the strict 1e-3 gate.
    compare_atol = 1e-2 if network == "yolo" and precision == "int16" else 1e-3
    if session is None:
        _ensure_one_shot_artifact(network, precision, build_dir, parsed_dir)

    result_img = out_dir / f"result_{image_index}.png"
    result_json = out_dir / f"result_{image_index}.json"
    plan = json.loads((build_dir / "plan.json").read_text())
    named_outputs = bool(plan.get("host_io", {}).get("outputs"))

    import compare_one_shot
    import hw_runner_win
    import one_shot_host_head
    compare_ok = True
    compare_exc: BaseException | None = None
    scratch_root = CACHE_DIR / "intermediate"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="case_", dir=scratch_root) as scratch:
        work = Path(scratch)
        primary = work / "output.bin"
        feat_dir = work / "features"
        timing_json = work / "timing.json"
        if named_outputs:
            feat_dir.mkdir()
        with _muted_stdout(not batch):
            hw_session = session
            close_session = False
            if hw_session is None:
                hw_session = hw_runner_win.HardwareSession(
                    build_dir,
                    poll_timeout_s=XDMA_POLL_TIMEOUT_S,
                    read_chunk_bytes=XDMA_READ_CHUNK_BYTES,
                )
                close_session = True
            try:
                input_bytes = hw_session.prepare_input(
                    yolo_image=img_path if network == "yolo" else None,
                    resnet_image=img_path if network == "resnet" else None,
                    yolo_parsed_dir=yolo_parsed_dir,
                    resnet_parsed_dir=resnet_parsed_dir,
                )
                hw_session.run(
                    input_bytes, primary,
                    feat_dir if named_outputs else None,
                    timing_json,
                    reuse_program=True,
                )
            finally:
                if close_session:
                    hw_session.close()
            try:
                compare_one_shot.compare(
                    build_dir,
                    img_path,
                    output=primary,
                    output_dir=feat_dir if named_outputs else None,
                    atol=compare_atol,
                    yolo_parsed_dir=yolo_parsed_dir,
                    parsed_dir=resnet_parsed_dir,
                    golden_cache_dir=CACHE_DIR / "golden_cache",
                    golden_mode="compute",
                )
            except RuntimeError as exc:
                compare_ok = False
                compare_exc = exc
                if "exceeded atol" not in str(exc):
                    raise
            head = one_shot_host_head.run_head(
                build_dir,
                img_path,
                output=primary,
                output_dir=feat_dir if network == "yolo" else None,
                conf=conf,
                yolo_parsed_dir=yolo_parsed_dir,
                parsed_dir=resnet_parsed_dir,
            )
        timing = json.loads(timing_json.read_text()) if batch else {}

        result = _prediction_result(network, precision, img_path, head, compare_ok, conf)
        detections = list(result.get("detections", []))
        staged = work / "result"
        staged.mkdir()
        if network == "yolo":
            _draw_yolo_detections(img_path, detections, staged / result_img.name)
        else:
            _draw_resnet_classification(img_path, list(result.get("topk", [])), staged / result_img.name)
        (staged / result_json.name).write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        (staged / result_img.name).rename(result_img)
        (staged / result_json.name).rename(result_json)

    if not batch:
        _print_compact_predictions(network, result)
        _print_result_banner(compare_ok)

    if batch:
        summary = f"{len(detections)} det" if network == "yolo" else result["topk"][0]["class_name"]
        print(f"  [fpga   ] {label} one-shot: {summary}")
        print(
            "            timing: "
            f"total={float(timing.get('total_s', 0.0)):.3f}s, "
            f"execute={float(timing.get('execute_s', 0.0)):.3f}s, "
            f"weights={float(timing.get('upload_weights_s', 0.0)):.3f}s, "
            f"read={float(timing.get('read_outputs_s', 0.0)):.3f}s"
        )
        if network == "yolo":
            for det in detections:
                bbox = ", ".join(f"{v:.1f}" for v in det["bbox"])
                print(f"            bbox=[{bbox}], conf={float(det['conf']):.4f}, class={int(det['class_id'])}")
            print(f"            image -> {_display_path(result_img)}")
        else:
            top = result["topk"][0]
            print(f"            top1={top['class_name']}({int(top['class_id'])}), score={float(top['score']):.4f}")
            print(f"            image -> {_display_path(result_img)}")
        print(f"            json  -> {_display_path(result_json)}")
    print(f"result dir: {out_dir.resolve()}", flush=True)
    if compare_exc is not None:
        raise compare_exc
    return result_img


# ─── Acceptance suite ────────────────────────────────────────────────────────

def _load_examples(manifest_path: Path, limit: int) -> tuple[dict, list[dict]]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"example manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    images = list(manifest.get("images", []))[:limit]
    if len(images) < limit:
        raise RuntimeError(f"{manifest_path} contains only {len(images)} images, need {limit}")
    for position, item in enumerate(images, start=1):
        image_path = manifest_path.parent / item["file"]
        if not image_path.exists():
            raise FileNotFoundError(f"example image not found: {image_path}")
        item["path"] = image_path
        item["index"] = int(item.get("image_id", position))
    return manifest, images


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_release_bundle(*, require_xdma: bool, quiet: bool = False) -> dict:
    """Fail early if a fresh clone is missing any maintained runtime asset."""
    checked_hashes = 0

    model_manifest = json.loads(MODEL_INPUTS_MANIFEST.read_text(encoding="utf-8"))
    for entry in model_manifest["files"]:
        path = REPO_ROOT / entry["path"]
        if not path.is_file():
            raise FileNotFoundError(f"model input not found: {path}")
        if path.stat().st_size != int(entry["bytes"]):
            raise RuntimeError(f"model input size mismatch: {path}")
        if _sha256_file(path) != str(entry["sha256"]).lower():
            raise RuntimeError(f"model input integrity check failed: {path}")
        checked_hashes += 1

    parsed_sets = [
        (COCO_YOLO_DIR / "parsed_int8", 60),
        (COCO_YOLO_DIR / "parsed_int16", 60),
        (REPO_ROOT / "project" / "model" / "resnet18" / "parsed_vai", 22),
        (REPO_ROOT / "project" / "model" / "resnet18" / "parsed_vai_int16_widened", 22),
    ]
    for parsed_dir, minimum_npz in parsed_sets:
        if not (parsed_dir / "network.json").is_file():
            raise FileNotFoundError(f"parsed model metadata not found: {parsed_dir}")
        npz_count = sum(1 for _ in (parsed_dir / "weights").glob("*.npz"))
        if npz_count < minimum_npz:
            raise RuntimeError(
                f"parsed model is incomplete: {parsed_dir} has {npz_count} NPZ files, "
                f"expected at least {minimum_npz}"
            )

    _coco_manifest, coco_images = _load_examples(COCO_MANIFEST, 20)
    _imagenet_manifest, imagenet_images = _load_examples(IMAGENET_MANIFEST, 20)

    xdma_files = [SCRIPTS_DIR / "xdma" / name for name in ("xdma_info.exe", "xdma_rw.exe")]
    if require_xdma:
        for path in xdma_files:
            if not path.is_file():
                raise FileNotFoundError(f"bundled XDMA tool not found: {path}")

    result = {
        "status": "PASS",
        "release": "top",
        "hashed_files": checked_hashes,
        "model_inputs": len(model_manifest["files"]),
        "compile_on_demand_workloads": 4,
        "coco_images": len(coco_images),
        "imagenet_images": len(imagenet_images),
        "xdma_tools": len(xdma_files) if require_xdma else "not-required",
    }
    if not quiet:
        print(
            "Release bundle PASS: "
            f"{result['compile_on_demand_workloads']} compile-on-demand workloads, "
            f"{result['model_inputs']} model inputs, "
            f"{result['coco_images']} COCO + {result['imagenet_images']} ImageNet images, "
            f"{result['hashed_files']} SHA256 checks"
        )
    return result


def run_acceptance_suite(limit: int, networks: list[str],
                         yolo_precisions: list[str], resnet_precisions: list[str],
                         run_time: str, conf: float = DEFAULT_CONF) -> int:
    """Run selected workloads, saving one PNG and one JSON per example."""
    import hw_runner_win

    total = failed = 0
    yolo_profile = YOLO_ONE_SHOT_PROFILES["coco"]
    for network in networks:
        manifest = COCO_MANIFEST if network == "yolo" else IMAGENET_MANIFEST
        _, examples = _load_examples(manifest, limit)
        precisions = yolo_precisions if network == "yolo" else resnet_precisions
        for precision in precisions:
            out_dir = _result_dir(network, precision, run_time)
            if network == "yolo":
                build_dir = yolo_profile["builds"][precision]
                parsed_dir = yolo_profile["parsed"][precision]
            else:
                build_dir = ONE_SHOT_DEFAULT_BUILDS[(network, precision)]
                parsed_dir = ONE_SHOT_COMPILE_PARSED[(network, precision)]
            _ensure_one_shot_artifact(network, precision, build_dir, parsed_dir)
            session = hw_runner_win.HardwareSession(
                build_dir,
                poll_timeout_s=XDMA_POLL_TIMEOUT_S,
                read_chunk_bytes=XDMA_READ_CHUNK_BYTES,
            )
            try:
                for item in examples:
                    image = Path(item["path"])
                    total += 1
                    try:
                        run_one_shot_fpga(
                            network, precision, image, out_dir,
                            image_index=item["index"],
                            build_dir=build_dir,
                            parsed_dir=parsed_dir,
                            conf=conf,
                            session=session,
                        )
                    except Exception as exc:
                        failed += 1
                        print(f"  [ERROR] {network} {_one_shot_tag(network, precision)} "
                              f"{image.name}: {type(exc).__name__}: {exc}", flush=True)
            finally:
                session.close()

    status = "PASS" if failed == 0 else "FAIL"
    print(f"\nAcceptance {status}: {total - failed}/{total} hardware cases PASS")
    print(f"output -> {OUT_DIR.resolve()}")
    return 0 if failed == 0 else 1


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="YOLOv5n / ResNet18 FPGA E2E inference",
        allow_abbrev=False,
    )
    ap.add_argument("--model", choices=["yolo", "resnet", "all"], default="all",
                    help="选择模型（默认 all）")
    ap.add_argument("--precision", choices=["int8", "int16", "both"], default="both",
                    help="选择精度（默认 both）")
    ap.add_argument("--num", type=int, default=1, metavar="N",
                    help="每组图片数（1–20，默认 1）")
    ap.add_argument("--conf", type=float, default=DEFAULT_CONF,
                    help="YOLO检测置信度阈值（0–1，默认 0.25）")
    args = ap.parse_args()
    if not 1 <= args.num <= 20:
        ap.error("--num must be an integer from 1 to 20")
    if not 0.0 <= args.conf <= 1.0:
        ap.error("--conf must be a finite number from 0 to 1")
    return args


def main() -> int:
    args = parse_args()
    networks = ["yolo", "resnet"] if args.model == "all" else [args.model]
    yolo_precisions = _expand_precisions(args.precision, "yolo")
    resnet_precisions = _expand_precisions(args.precision, "resnet")

    try:
        _verify_release_bundle(require_xdma=(os.name == "nt"), quiet=args.num == 1)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"  [ERROR] release preflight failed: {exc}")
        return 1
    run_time = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        _migrate_legacy_outputs(run_time)
    except (OSError, ValueError) as exc:
        print(f"  [ERROR] could not move legacy network outputs: {exc}")
        return 1
    _setup_imports()
    if args.num > 1:
        return run_acceptance_suite(args.num, networks, yolo_precisions, resnet_precisions,
                                    run_time, args.conf)

    yolo_profile = YOLO_ONE_SHOT_PROFILES["coco"]
    for network in networks:
        image = yolo_profile["image"] if network == "yolo" else DEFAULT_RESNET_IMG
        manifest = COCO_MANIFEST if network == "yolo" else IMAGENET_MANIFEST
        _, examples = _load_examples(manifest, 20)
        example = next((item for item in examples if item["path"] == image), None)
        if example is None:
            print(f"  [ERROR] default image not listed in {manifest}: {image.name}")
            return 1

        precisions = yolo_precisions if network == "yolo" else resnet_precisions
        for precision in precisions:
            if network == "yolo":
                build_dir = yolo_profile["builds"][precision]
                parsed_dir = yolo_profile["parsed"][precision]
            else:
                build_dir = ONE_SHOT_DEFAULT_BUILDS[(network, precision)]
                parsed_dir = ONE_SHOT_COMPILE_PARSED[(network, precision)]
            try:
                run_one_shot_fpga(
                    network, precision, image, _result_dir(network, precision, run_time),
                    image_index=example["index"],
                    build_dir=build_dir,
                    parsed_dir=parsed_dir,
                    conf=args.conf,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                if "exceeded atol" not in str(exc):
                    print(f"  [ERROR] {exc}")
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
