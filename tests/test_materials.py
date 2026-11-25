"""核对合成资料的字段和业务记录引用。"""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from read_materials import read_collection

DATA_ROOT = ROOT / "data"


class MaterialsTests(unittest.TestCase):
    def test_collections_follow_manifest(self):
        manifest = json.loads((DATA_ROOT / "index.json").read_text(encoding="utf-8"))
        self.assertEqual({path.stem for path in DATA_ROOT.glob("*.jsonl")}, set(manifest))
        for name in manifest:
            with self.subTest(name=name):
                self.assertTrue(read_collection(DATA_ROOT, name))

    def test_source_identities_are_distinct(self):
        rows = read_collection(DATA_ROOT, 'catalogue')
        identities = {tuple(row[key] for key in ('release', 'key')) for row in rows}
        self.assertEqual(len(identities), len(rows))

    def test_relation_points_to_existing_record(self):
        targets = {row['release'] for row in read_collection(DATA_ROOT, 'catalogue')}
        for row in read_collection(DATA_ROOT, 'submissions'):
            identifier = row['release']
            if identifier is not None:
                self.assertIn(identifier, targets)
