import importlib.util
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

NETWORK_DIR = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("network_entry", NETWORK_DIR / "run.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class OutputLayoutTest(unittest.TestCase):
    def test_cli_saves_all_image_pairs_in_one_batch_directory(self):
        for count in (1, 20):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                build = root / "build"
                build.mkdir()
                (build / "plan.json").write_text('{"host_io": {"outputs": []}}')
                session = Mock()
                session.prepare_input.return_value = b"test input"
                session.run.side_effect = lambda _input, _output, _features, timing, **kw: (
                    timing.write_text('{}')
                )
                hardware = SimpleNamespace(HardwareSession=Mock(return_value=session))
                modules = {
                    "hw_runner_win": hardware,
                    "compare_one_shot": SimpleNamespace(compare=Mock()),
                    "one_shot_host_head": SimpleNamespace(run_head=Mock(return_value={
                        "network": "yolov5n", "detections": [],
                    })),
                }
                profile = dict(entry.YOLO_ONE_SHOT_PROFILES["coco"], builds={"int8": build})
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.multiple(
                        entry, NETWORK_DIR=root, OUT_DIR=root / "output", CACHE_DIR=root / "cached",
                        YOLO_ONE_SHOT_PROFILES={"coco": profile},
                        _setup_imports=Mock(), _verify_release_bundle=Mock(),
                        _ensure_one_shot_artifact=Mock(),
                    ))
                    stack.enter_context(patch.dict(sys.modules, modules))
                    stack.enter_context(patch.object(sys, "argv", [
                        "run.py", "--model", "yolo", "--precision", "int8", "--num", str(count),
                    ]))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    self.assertEqual(entry.main(), 0)

                folders = list((root / "output").iterdir())
                self.assertEqual(len(folders), 1)
                self.assertTrue(folders[0].name.startswith("yolo_int8_"))
                _, examples = entry._load_examples(entry.COCO_MANIFEST, count)
                self.assertEqual({path.name for path in folders[0].iterdir()}, {
                    f"result_{example['index']}.{suffix}"
                    for example in examples for suffix in ("png", "json")
                })
                for example in examples:
                    result = json.loads((folders[0] / f"result_{example['index']}.json").read_text())
                    self.assertEqual(result["image"], example["file"])
                self.assertEqual(session.run.call_count, count)
                hardware.HardwareSession.assert_called_once()
                session.close.assert_called_once()

    def test_migrate_previous_outputs_and_keep_current_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            cache = root / "cached"
            run_time = "20261121_143025_123456"
            current = output / f"yolo_int8_{run_time}"
            old_run = output / "20261120_090000_000001"
            for folder in (current, old_run, output / "inference",
                           output / "compiled", root / ".cache" / "compiled"):
                folder.mkdir(parents=True)
            (root / ".cache" / "compiled" / "newer.bin").write_bytes(b"cached")
            (output / "compiled" / "older.bin").write_bytes(b"legacy")
            (output / "inference" / "old.jpg").write_bytes(b"old result")

            with patch.multiple(entry, NETWORK_DIR=root, OUT_DIR=output, CACHE_DIR=cache):
                entry._migrate_legacy_outputs(run_time)
                entry._migrate_legacy_outputs(run_time)

            archived = cache / "legacy" / run_time / "output"
            self.assertEqual(list(output.iterdir()), [current])
            self.assertEqual((cache / "compiled" / "newer.bin").read_bytes(), b"cached")
            self.assertEqual((archived / "compiled" / "older.bin").read_bytes(), b"legacy")
            self.assertEqual((archived / "inference" / "old.jpg").read_bytes(), b"old result")
            self.assertTrue((archived / old_run.name).is_dir())


if __name__ == "__main__":
    unittest.main()
