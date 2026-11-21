import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

NETWORK_DIR = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("network_entry", NETWORK_DIR / "run.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class OutputLayoutTest(unittest.TestCase):
    def test_coco_output_uses_dataset_ids(self):
        _, examples = entry._load_examples(entry.COCO_MANIFEST, 20)
        run_time = "20261121_143025_123456"
        names = {entry._result_dir("yolo", "int8", example, run_time).name for example in examples}
        self.assertEqual(len(names), 20)
        self.assertIn(f"yolo_int8_139_{run_time}", names)
        self.assertIn(f"yolo_int8_1761_{run_time}", names)

    def test_migrate_previous_outputs_and_keep_current_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            cache = root / "cached"
            run_time = "20261121_143025_123456"
            current = output / f"yolo_int8_139_{run_time}"
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
