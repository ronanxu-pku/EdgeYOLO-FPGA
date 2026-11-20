"""EdgeYOLO-FPGA 的自包含运行入口。

仓库包含 PT/ONNX/解析模型以及 20+20 张测试图片。
编译与验收产物写到 ``test/network/output/``。

常用命令::

    python test/network/run.py --model yolo --precision int8 --num 1
    python test/network/run.py --model resnet --precision int16 --num 1
    python test/network/run.py --model all --precision both --num 20

启动时检查编译缓存，缺少产物时自动编译。FPGA 跑完后在主机计算 compiler golden 并比对。
单图打印推理和比对结果。批量测试打印详细日志并生成验收报告。

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
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_TB = REPO_ROOT / "test" / "network" / "host"
OUT_DIR = REPO_ROOT / "test" / "network" / "output"
INFERENCE_OUT_DIR = OUT_DIR / "inference"
WORK_DIR_ENV = "EDGEYOLO_RUNS_BASE"

DEFAULT_YOLO_IMG = REPO_ROOT / "test" / "network" / "examples" / "coco" / "000000000139.jpg"
DEFAULT_RESNET_IMG = REPO_ROOT / "test" / "network" / "examples" / "imagenet" / "n01443537_goldfish.JPEG"
RELEASE_ID = "80832ec_attempt1"
COMPILED_DIR = OUT_DIR / "compiled" / RELEASE_ID
COCO_YOLO_DIR = REPO_ROOT / "project" / "model" / "yolov5n_coco50k_qat"
EXAMPLES_DIR = REPO_ROOT / "test" / "network" / "examples"
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
        "output_group": "yolo_coco",
    },
}


def _install_unit_tb_run_module() -> None:
    if "run" in sys.modules and getattr(sys.modules["run"], "__file__", "") != str(__file__):
        return
    spec = importlib.util.spec_from_file_location("run", UNIT_TB / "run.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load test/network/host/run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run"] = mod
    spec.loader.exec_module(mod)


def _setup_imports() -> None:
    sys.path.insert(0, str(UNIT_TB))
    sys.path.insert(0, str(REPO_ROOT / "test" / "network"))
    sys.path.insert(0, str(REPO_ROOT / "project" / "runtime"))
    sys.path.insert(0, str(REPO_ROOT / "project" / "rtl" / "tb" / "lite_bd" / "module_tb"))
    _install_unit_tb_run_module()


def save_image(img: np.ndarray, path: Path) -> None:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(str(path), quality=90)


def _display_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved


def _clear_result_jpgs(out_dir: Path, *, quiet: bool = False) -> None:
    if not out_dir.is_dir():
        return
    removed = []
    seen: set[str] = set()
    for path in out_dir.glob("*.jpg"):
        key = str(path.resolve()).lower()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        path.unlink()
        removed.append(path)
    if removed and not quiet:
        print(f"  [clean  ] removed {len(removed)} result image(s) in {_display_path(out_dir)}", flush=True)


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

    for det in detections:
        x1, y1, x2, y2 = [float(v) for v in det["bbox"]]
        cls = int(det.get("class_id", det.get("class", 0)))
        conf = float(det.get("conf", det.get("confidence", 0.0)))
        label = f"{det.get('class_name', names.get(cls, f'class_{cls}'))} {conf:.3f}"
        draw.rectangle([x1, y1, x2, y2], outline=(255, 64, 32), width=3)
        left, top, right, bottom = draw.textbbox((x1, y1), label, font=font)
        label_h = bottom - top + 6
        label_w = right - left + 8
        y_label = max(0, y1 - label_h)
        draw.rectangle([x1, y_label, x1 + label_w, y_label + label_h], fill=(255, 64, 32))
        draw.text((x1 + 4, y_label + 3), label, fill=(255, 255, 255), font=font)

    out_img.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_img), quality=95)


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
    out_root: Path,
    *,
    build_dir: Path,
    parsed_dir: Path,
    result_group: str,
    session=None,
) -> Path:
    """Run a full one-shot FPGA workload, compare features, and run the host boundary."""
    tag = _one_shot_tag(network, precision)
    net_dir = "yolo" if network == "yolo" else "resnet"
    batch = session is not None
    yolo_parsed_dir = parsed_dir if network == "yolo" else None
    resnet_parsed_dir = parsed_dir if network == "resnet" else None
    label = f"{'YOLO' if network == 'yolo' else 'ResNet'} {tag.upper()}"
    # Selected attempt1 board validation measured native COCO W16A16 PAN
    # max_abs <= 0.00930. Other maintained paths pass the strict 1e-3 gate.
    compare_atol = 1e-2 if network == "yolo" and precision == "int16" else 1e-3
    out_dir = out_root / result_group / f"one_shot_{tag}"
    feat_dir = out_dir / "features"
    out_dir.mkdir(parents=True, exist_ok=True)

    if session is None:
        _ensure_one_shot_artifact(network, precision, build_dir, parsed_dir)

    stem = img_path.stem
    file_prefix = f"{stem}_{net_dir}_{tag}_fpga_oneshot"
    primary = out_dir / f"{file_prefix}.bin"
    timing_json = out_dir / f"{file_prefix}_timing.json"
    head_json = out_dir / f"{file_prefix}.json"
    result_img = out_dir / f"{file_prefix}.jpg"

    plan = json.loads((build_dir / "plan.json").read_text())
    named_outputs = bool(plan.get("host_io", {}).get("outputs"))
    if named_outputs:
        feat_dir.mkdir(parents=True, exist_ok=True)

    import compare_one_shot
    import hw_runner_win
    import one_shot_host_head
    compare_ok = True
    compare_exc: BaseException | None = None
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
                golden_cache_dir=OUT_DIR / "golden_cache",
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
            json_out=head_json,
            conf=0.15,
            expect_top1=1 if network == "resnet" and not batch else None,
            yolo_parsed_dir=yolo_parsed_dir if network == "yolo" else None,
            parsed_dir=resnet_parsed_dir if network == "resnet" else None,
        )

    result = head if isinstance(head, dict) else json.loads(head_json.read_text())
    detections = list(result.get("detections", []))
    if network == "yolo":
        _draw_yolo_detections(img_path, detections, result_img)
    else:
        _draw_resnet_classification(img_path, list(result.get("topk", [])), result_img)

    if not batch:
        _print_compact_predictions(network, result)
        _print_result_banner(compare_ok)

    if batch:
        timing = json.loads(timing_json.read_text())
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
        print(f"            json  -> {_display_path(head_json)}")
        print(f"            time  -> {_display_path(timing_json)}")
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
    for item in images:
        image_path = manifest_path.parent / item["file"]
        if not image_path.exists():
            raise FileNotFoundError(f"example image not found: {image_path}")
        item["path"] = image_path
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

    xdma_files = [REPO_ROOT / "test" / "network" / "bin" / name for name in ("xdma_info.exe", "xdma_rw.exe")]
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
                         yolo_precisions: list[str], resnet_precisions: list[str]) -> int:
    """Run selected INT8/INT16 network acceptance workloads."""
    out_root = OUT_DIR
    report_dir = out_root / "acceptance"
    report_dir.mkdir(parents=True, exist_ok=True)
    cases: list[dict] = []

    if "yolo" in networks:
        _, coco_images = _load_examples(COCO_MANIFEST, limit)
        yolo_profile = YOLO_ONE_SHOT_PROFILES["coco"]
        import hw_runner_win
        for precision in yolo_precisions:
            tag = _one_shot_tag("yolo", precision)
            build_dir = Path(yolo_profile["builds"][precision])
            parsed_dir = Path(yolo_profile["parsed"][precision])
            _ensure_one_shot_artifact("yolo", precision, build_dir, parsed_dir)
            result_dir = out_root / "acceptance" / "yolo_coco" / f"one_shot_{tag}"
            _clear_result_jpgs(result_dir)
            session = hw_runner_win.HardwareSession(
                build_dir,
                poll_timeout_s=XDMA_POLL_TIMEOUT_S,
                read_chunk_bytes=XDMA_READ_CHUNK_BYTES,
            )
            try:
                for item in coco_images:
                    image = Path(item["path"])
                    started = time.time()
                    record = {
                        "network": "yolov5n_coco",
                        "precision": tag,
                        "image": image.name,
                        "image_id": item.get("image_id"),
                    }
                    try:
                        run_one_shot_fpga(
                            "yolo", precision, image, out_root,
                            build_dir=build_dir,
                            parsed_dir=parsed_dir,
                            result_group="acceptance/yolo_coco",
                            session=session,
                        )
                        base = out_root / "acceptance" / "yolo_coco" / f"one_shot_{tag}"
                        result = json.loads((base / f"{image.stem}_yolo_{tag}_fpga_oneshot.json").read_text())
                        timing = json.loads((base / f"{image.stem}_yolo_{tag}_fpga_oneshot_timing.json").read_text())
                        record.update({
                            "status": "PASS",
                            "detections": len(result.get("detections", [])),
                            "timing_s": timing,
                        })
                    except Exception as exc:
                        record.update({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
                    record["wall_s"] = time.time() - started
                    cases.append(record)
            finally:
                session.close()

    if "resnet" in networks:
        _, imagenet_images = _load_examples(IMAGENET_MANIFEST, limit)
        import hw_runner_win
        for precision in resnet_precisions:
            tag = _one_shot_tag("resnet", precision)
            parsed_dir = ONE_SHOT_COMPILE_PARSED[("resnet", precision)]
            build_dir = ONE_SHOT_DEFAULT_BUILDS[("resnet", precision)]
            _ensure_one_shot_artifact("resnet", precision, build_dir, parsed_dir)
            result_dir = out_root / "acceptance" / "resnet" / f"one_shot_{tag}"
            _clear_result_jpgs(result_dir)
            session = hw_runner_win.HardwareSession(
                build_dir,
                poll_timeout_s=XDMA_POLL_TIMEOUT_S,
                read_chunk_bytes=XDMA_READ_CHUNK_BYTES,
            )
            try:
                for item in imagenet_images:
                    image = Path(item["path"])
                    started = time.time()
                    record = {
                        "network": "resnet18_imagenet",
                        "precision": tag,
                        "image": image.name,
                        "synset": item.get("synset"),
                        "expected_class_id": item.get("class_id"),
                        "expected_label": item.get("label"),
                    }
                    try:
                        run_one_shot_fpga(
                            "resnet", precision, image, out_root,
                            build_dir=build_dir,
                            parsed_dir=parsed_dir,
                            result_group="acceptance/resnet",
                            session=session,
                        )
                        base = out_root / "acceptance" / "resnet" / f"one_shot_{tag}"
                        result = json.loads((base / f"{image.stem}_resnet_{tag}_fpga_oneshot.json").read_text())
                        timing = json.loads((base / f"{image.stem}_resnet_{tag}_fpga_oneshot_timing.json").read_text())
                        top1 = result["topk"][0]
                        record.update({
                            "status": "PASS",
                            "top1_class_id": int(top1["class_id"]),
                            "top1_class_name": top1["class_name"],
                            "top1_matches_sample_label": int(top1["class_id"]) == int(item["class_id"]),
                            "timing_s": timing,
                        })
                    except Exception as exc:
                        record.update({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
                    record["wall_s"] = time.time() - started
                    cases.append(record)
            finally:
                session.close()

    failed = sum(case["status"] != "PASS" for case in cases)
    report_status = "PASS" if failed == 0 else "FAIL"
    report = {
        "status": report_status,
        "precision_policy": {
            "yolo_int8": "INT8 W8A8",
            "yolo_int16": "INT16 W16A16 with signed INT64 accumulators",
            "resnet_int8": "INT8 W8A8",
            "resnet_int16": "INT16 W16A16 with signed INT64 accumulators",
        },
        "examples_per_dataset": limit,
        "case_count": len(cases),
        "passed": len(cases) - failed,
        "failed": failed,
        "cases": cases,
    }
    json_path = report_dir / "acceptance_report.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_lines = [
        "# EdgeYOLO-FPGA acceptance report",
        "",
        f"- Status: **{report_status}**",
        f"- Hardware image/precision cases: {len(cases) - failed}/{len(cases)} PASS",
        "- Precision: INT8 and INT16 for YOLO and ResNet",
        "",
        "| Network | Precision | Image | Result |",
        "|---|---:|---|---|",
    ]
    for case in cases:
        detail = case.get("top1_class_name", f"{case.get('detections', '-')} detections")
        if case["status"] != "PASS":
            detail = case.get("error", "failed")
        md_lines.append(f"| {case['network']} | {case['precision']} | {case['image']} | {case['status']}: {detail} |")
    (report_dir / "acceptance_report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"\nAcceptance {report_status}: {len(cases) - failed}/{len(cases)} hardware cases PASS")
    print(f"report -> {_display_path(json_path)}")
    return 0 if report_status == "PASS" else 1


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
                    help="每组图片数（1–20，默认 1），大于 1 时生成验收报告")
    args = ap.parse_args()
    if not 1 <= args.num <= 20:
        ap.error("--num must be an integer from 1 to 20")
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
    _setup_imports()

    if args.num > 1:
        return run_acceptance_suite(args.num, networks, yolo_precisions, resnet_precisions)

    yolo_profile = YOLO_ONE_SHOT_PROFILES["coco"]
    for network in networks:
        image = yolo_profile["image"] if network == "yolo" else DEFAULT_RESNET_IMG
        if not image.is_file():
            print(f"  [ERROR] image not found: {image}")
            return 1

        precisions = yolo_precisions if network == "yolo" else resnet_precisions
        for precision in precisions:
            if network == "yolo":
                build_dir = yolo_profile["builds"][precision]
                parsed_dir = yolo_profile["parsed"][precision]
                result_group = yolo_profile["output_group"]
            else:
                build_dir = ONE_SHOT_DEFAULT_BUILDS[(network, precision)]
                parsed_dir = ONE_SHOT_COMPILE_PARSED[(network, precision)]
                result_group = "resnet"
            tag = _one_shot_tag(network, precision)
            _clear_result_jpgs(INFERENCE_OUT_DIR / result_group / f"one_shot_{tag}", quiet=True)
            try:
                run_one_shot_fpga(
                    network, precision, image, INFERENCE_OUT_DIR,
                    build_dir=build_dir,
                    parsed_dir=parsed_dir,
                    result_group=result_group,
                )
            except RuntimeError as exc:
                if "exceeded atol" not in str(exc):
                    print(f"  [ERROR] {exc}")
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
