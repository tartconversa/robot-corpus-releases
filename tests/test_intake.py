"""入湖校验必须能识别结构、引用与资产缺陷，而不是只在好数据上空转。"""
import json
import tempfile
import unittest
from pathlib import Path

from corpus.intake import load_store

ROOT = Path(__file__).resolve().parents[1]


class IntakeValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sources = self.tmp / "sources"
        self.assets = self.tmp / "assets"
        self.sources.mkdir()
        self.assets.mkdir()
        self.schema = json.loads((ROOT / "source-fields.json").read_text(encoding="utf-8"))

    def _write(self, name, table, records, intake=1):
        (self.sources / name).write_text(
            json.dumps({"intake": intake, "table": table, "records": records}),
            encoding="utf-8",
        )

    def test_real_delivery_is_clean(self):
        _, problems = load_store(ROOT / "sources", ROOT / "assets", self.schema)
        self.assertEqual(problems, [])

    def test_malformed_delivery_reports_every_defect(self):
        self._write("01-segments.json", "segments", [
            {"id": "cam-x", "episode": "X", "channel": "cam",
             "ticks": [5, 5], "file": "missing.csv", "site": "east"},
        ])
        self._write("01-labels.json", "labels", [
            {"id": "lx", "episode": "X", "cam_ticks": [1, 2], "tag": "grip",
             "state": "reviewed", "label_rule": "v1", "applies": "ghost"},
        ])
        self._write("01-exposures.json", "exposures", [
            {"id": "ex1", "member": "ghost-member", "recipient": "t1", "role": "train"},
        ])
        self._write("01-scores.json", "scores", [
            {"id": "sc1", "member": "ghost-member", "evaluator": "t1", "score": 150},
        ])
        _, problems = load_store(self.sources, self.assets, self.schema)
        joined = "\n".join(problems)
        self.assertIn("递增两点刻度", joined)
        self.assertIn("资产文件缺失", joined)
        self.assertIn("applies 未知记录", joined)
        self.assertIn("回执 ex1", joined)
        self.assertIn("成绩 sc1 越界", joined)

    def test_duplicate_intake_table_rejected(self):
        self._write("01-labels.json", "labels", [])
        self._write("01-labels-copy.json", "labels", [])
        with self.assertRaises(ValueError):
            load_store(self.sources, self.assets, self.schema)

    def test_unknown_table_named(self):
        self._write("01-ghost.json", "ghost_table", [])
        _, problems = load_store(self.sources, self.assets, self.schema)
        self.assertTrue(any("未知表" in p for p in problems))

    def test_missing_sources_dir(self):
        _, problems = load_store(self.tmp / "nope", self.assets, self.schema)
        self.assertTrue(any("没有交付文件" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
