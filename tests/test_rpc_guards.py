import importlib.util
import contextlib
import io
import json
import unittest
import asyncio
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "opencode-plugin/scripts/jupyter_cells.py"
spec = importlib.util.spec_from_file_location("jupyter_cells", SCRIPT)
jc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jc)


def run_op(ynb, request):
    """Drive one RPC op to completion and return its parsed response."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        asyncio.run(jc.yjson(ynb, request, None, None, "nb.ipynb"))
    return json.loads(buffer.getvalue())


class FakeNotebook:
    def __init__(self, *cells):
        self.ycells = list(cells)

    def get_cell(self, index):
        return self.ycells[index]

    def append_cell(self, cell):
        self.ycells.append(cell)

    def create_ycell(self, cell):
        value = dict(cell)
        value.setdefault("id", f"gen-{len(self.ycells)}")
        value.setdefault("metadata", {})
        return value


def cell(cell_id, source):
    value = {"id": cell_id, "cell_type": "code", "source": source, "metadata": {}}
    value["metadata"][jc.STAMP_KEY] = {"hash": jc.cell_hash(value)}
    return value


def target_request(cell_id, expected_hash):
    return {"cell_id": cell_id, "expected_hash": expected_hash}


class RPCAddressTests(unittest.TestCase):
    def test_resolves_viewed_cell_by_id(self):
        target = cell("stable-id", "x = 1")
        notebook = FakeNotebook(target)
        index, current, error = jc.rpc_target(notebook,
            target_request("stable-id", jc.cell_hash(target)))
        self.assertEqual(index, 0)
        self.assertIs(current, target)
        self.assertIsNone(error)

    def test_refuses_unknown_id(self):
        notebook = FakeNotebook(cell("present", "x = 1"))
        index, current, error = jc.rpc_target(notebook,
            target_request("missing", jc.cell_hash(cell("missing", "x = 1"))))
        self.assertIsNone(index)
        self.assertIsNone(current)
        self.assertIn("No cell with id 'missing'", error)

    def test_requires_hash_guard(self):
        target = cell("stable-id", "x = 1")
        notebook = FakeNotebook(target)
        index, current, error = jc.rpc_target(notebook, {"cell_id": "stable-id"})
        self.assertIsNone(index)
        self.assertIn("has not been read in this session", error)

    def test_refuses_cell_changed_after_view(self):
        target = cell("stable-id", "x = 2")
        notebook = FakeNotebook(target)
        index, current, error = jc.rpc_target(notebook,
            target_request("stable-id", jc.cell_hash({"source": "x = 1"})))
        self.assertIsNone(index)
        self.assertIn("changed since it was read", error)


class RPCInsertTests(unittest.TestCase):
    """The point of #1: concurrent user inserts above the target no longer
    break anything, and placement can be expressed by neighbour id alone."""

    def test_mutation_by_id_survives_insert_above(self):
        original = cell("original", "x = 1")
        # user inserted two cells above the target since it was read
        notebook = FakeNotebook(cell("first", "pass"), cell("second", "pass"), original)
        index, current, error = jc.rpc_target(notebook,
            target_request("original", jc.cell_hash({"source": "x = 1"})))
        self.assertIsNone(error)
        self.assertEqual(index, 2)
        self.assertIs(current, original)

    def test_add_after_id_without_prior_read(self):
        ynb = FakeNotebook(cell("head", "pass"), cell("tail", "pass"))
        result = run_op(ynb, {"op": "add", "source": "y = 2", "cell_type": "code",
                              "after_id": "head"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(ynb.ycells[1]["source"], "y = 2")
        self.assertEqual(ynb.ycells[2]["id"], "tail")

    def test_add_after_unknown_id_is_refused(self):
        ynb = FakeNotebook(cell("head", "pass"))
        result = run_op(ynb, {"op": "add", "source": "y = 2", "after_id": "nope"})
        self.assertFalse(result["ok"])
        self.assertIn("No cell with id 'nope'", result["error"])
        self.assertEqual(len(ynb.ycells), 1)

    def test_delete_by_id_after_neighbour_change(self):
        target = cell("doomed", "x = 1")
        ynb = FakeNotebook(target)
        result = run_op(ynb, {"op": "delete", "cell_id": "doomed",
                              "expected_hash": jc.cell_hash(target)})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(ynb.ycells), 0)


class ProjectionTests(unittest.TestCase):
    def test_header_shows_id_and_execution_number(self):
        code = cell("h1E2klD3", "x = 1")
        code["execution_count"] = 7
        markdown = {"id": "Md-1", "cell_type": "markdown", "source": "note", "metadata": {}}
        text = jc.render_projection(FakeNotebook(code, markdown))
        self.assertIn("# %% code id=h1E2klD3 exec=7", text)
        self.assertIn("# %% markdown id=Md-1", text)
        self.assertNotIn("# %% [", text)

    def test_unrun_cell_shows_none_execution(self):
        code = cell("fresh", "x = 1")
        code["execution_count"] = None
        text = jc.render_projection(FakeNotebook(code))
        self.assertIn("id=fresh exec=None", text)


if __name__ == "__main__":
    unittest.main()
