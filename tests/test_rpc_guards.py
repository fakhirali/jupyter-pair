import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "opencode-plugin/scripts/jupyter_cells.py"
spec = importlib.util.spec_from_file_location("jupyter_cells", SCRIPT)
jc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jc)


class FakeNotebook:
    def __init__(self, *cells):
        self.ycells = cells

    def get_cell(self, index):
        return self.ycells[index]


def cell(cell_id, source):
    value = {"id": cell_id, "cell_type": "code", "source": source, "metadata": {}}
    value["metadata"][jc.STAMP_KEY] = {"hash": jc.cell_hash(value)}
    return value


class RPCGuardTests(unittest.TestCase):
    def test_allows_same_viewed_cell(self):
        target = cell("stable-id", "x = 1")
        notebook = FakeNotebook(target)
        current, error = jc.rpc_target(notebook, {
            "index": 0,
            "expected_id": "stable-id",
            "expected_hash": jc.cell_hash(target),
        })
        self.assertIs(current, target)
        self.assertIsNone(error)

    def test_refuses_cell_changed_after_view(self):
        target = cell("stable-id", "x = 2")
        notebook = FakeNotebook(target)
        current, error = jc.rpc_target(notebook, {
            "index": 0,
            "expected_id": "stable-id",
            "expected_hash": jc.cell_hash({"source": "x = 1"}),
        })
        self.assertIsNone(current)
        self.assertIn("changed since it was read", error)

    def test_refuses_index_shift_to_another_cell(self):
        notebook = FakeNotebook(cell("inserted", "new"), cell("original", "x = 1"))
        current, error = jc.rpc_target(notebook, {
            "index": 0,
            "expected_id": "original",
            "expected_hash": jc.cell_hash({"source": "x = 1"}),
        })
        self.assertIsNone(current)
        self.assertIn("moved or was replaced", error)


if __name__ == "__main__":
    unittest.main()
