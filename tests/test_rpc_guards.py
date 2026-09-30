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


class ProjectionSearchTests(unittest.TestCase):
    def big_cell(self):
        src = "\n".join("pass" for _ in range(400)) + "\ncan_jump = True"
        return cell("big-cell-id", src)   # beyond the 4000-char projection truncation

    def test_search_finds_match_beyond_projection_truncation(self):
        ynb = FakeNotebook(self.big_cell())
        result = run_op(ynb, {"op": "search", "pattern": "can_jump"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["hits"][0]["id"], "big-cell-id")
        self.assertEqual(result["hits"][0]["cell_line"], 401)
        self.assertIn("can_jump = True", result["hits"][0]["context"])

    def test_search_literal_ignores_regex_syntax(self):
        ynb = FakeNotebook(cell("dot", "a.c + a1c"))
        result = run_op(ynb, {"op": "search", "pattern": "a.c", "literal": True})
        self.assertEqual(len(result["hits"]), 1)
        escaped = run_op(ynb, {"op": "search", "pattern": "a.c"})
        self.assertEqual(len(escaped["hits"]), 2)  # `.` matches any char: a.c and a1c

    def test_search_case_insensitive_by_default(self):
        ynb = FakeNotebook(cell("case", "CAN_JUMP = 1"))
        result = run_op(ynb, {"op": "search", "pattern": "can_jump"})
        self.assertEqual(len(result["hits"]), 1)
        sharp = run_op(ynb, {"op": "search", "pattern": "can_jump", "case_sensitive": True})
        self.assertEqual(sharp["hits"], [])

    def test_search_scoped_to_cell_id(self):
        ynb = FakeNotebook(cell("one", "needle here"), cell("two", "needle there"))
        result = run_op(ynb, {"op": "search", "pattern": "needle", "cell_id": "two"})
        self.assertEqual([h["id"] for h in result["hits"]], ["two"])

    def test_search_unknown_cell_id_refused(self):
        ynb = FakeNotebook(cell("one", "needle"))
        result = run_op(ynb, {"op": "search", "pattern": "needle", "cell_id": "nope"})
        self.assertFalse(result["ok"])
        self.assertIn("No cell with id 'nope'", result["error"])

    def test_read_by_cell_line_pages_long_cell(self):
        ynb = FakeNotebook(self.big_cell())
        cell_data_run = run_op(ynb, {"op": "read", "cell_id": "big-cell-id",
                                     "source_line": 296, "source_limit": 10})
        data = cell_data_run["cell"]
        self.assertEqual(data["total_source_lines"], 401)
        self.assertIn("296: pass", data["source"])
        self.assertNotIn("399:", data["source"])
        self.assertEqual(data["outputs"], [])


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
