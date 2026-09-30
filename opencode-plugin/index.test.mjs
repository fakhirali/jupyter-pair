import assert from "node:assert/strict"
import test from "node:test"
import plugin, { parseNotebookPath } from "./index.mjs"

function makeContext() {
  const tools = new Map(["read", "write", "edit"].map((id) => [id, {
    id,
    name: id,
    description: `${id} original`,
    input: { type: "object", properties: {} },
    execute: async () => ({ output: `${id} original output` }),
  }]))
  tools.set("grep", {
    id: "grep",
    name: "grep",
    description: "grep original",
    input: { type: "object", properties: { pattern: { type: "string" }, path: { type: "string" } } },
    execute: async () => ({ output: [], content: "No matches found" }),
  })
  const editor = {
    list: () => [...tools.values()],
    get: (id) => tools.get(id),
    update: (id, change) => change(tools.get(id)),
    add: (tool) => tools.set(`${tool.options.namespace}_${tool.name}`, tool),
  }
  const ctx = {
    location: { directory: "/workspace" },
    session: { get: async () => ({ location: { directory: "/workspace" } }) },
    tool: { transform: async (fn) => fn(editor) },
  }
  return { ctx, tools }
}

test("path parser: numeric suffix stays index-shaped source data, non-numeric is a cell id", () => {
  assert.deepEqual(parseNotebookPath("notebooks/demo.ipynb:9xG7nZqB"), {
    notebook: "notebooks/demo.ipynb", index: null, id: "9xG7nZqB",
  })
  assert.deepEqual(parseNotebookPath("demo.ipynb"), { notebook: "demo.ipynb", index: null, id: null })
  assert.deepEqual(parseNotebookPath("demo.ipynb:5"), { notebook: "demo.ipynb", index: 5, id: null })
  assert.equal(parseNotebookPath("demo.py:5"), null)
})

test("native tools keep their declared output shape and delegate ordinary files", async () => {
  const { ctx, tools } = makeContext()
  await plugin.setup(ctx)

  assert.deepEqual([...tools.keys()].filter((id) => id.startsWith("jupyter_")), [
    "jupyter_run_cell", "jupyter_add_cell", "jupyter_delete_cell",
  ])

  const delegated = await tools.get("read").execute({ filePath: "README.md" }, { id: "1", sessionID: "s" })
  assert.equal(delegated.output, "read original output")

  const nonNotebook = await tools.get("read").execute({ filePath: "demo.py:5" }, { id: "2", sessionID: "s" })
  assert.equal(nonNotebook.output, "read original output")

  const suffixBad = await tools.get("read").execute({ filePath: "demo.ipynb:2" }, { id: "7", sessionID: "s" })
  assert.match(suffixBad.output.content, /addressed by their stable id/)

  const grepDelegated = await tools.get("grep").execute(
    { pattern: "x", path: "src" }, { id: "6", sessionID: "s" })
  assert.deepEqual(grepDelegated.output, [])
  assert.equal(tools.get("grep").description, "grep original For .ipynb notebooks, this searches every cell's FULL source on the live server (not a truncated projection, not raw JSON): hits report `Line N (cell id=<id>, cell-line K)` with a few context lines — the id targets read/edit/run, and N continues deeper into that cell via read(path, offset: N).")

  await assert.rejects(() => tools.get("write").execute(
    { filePath: "demo.ipynb", content: "x = 1" }, { id: "3", sessionID: "s" }),
    /Add the cell id to the notebook path/)

  await assert.rejects(() => tools.get("edit").execute(
    { filePath: "demo.ipynb", oldString: "a", newString: "b" }, { id: "4", sessionID: "s" }),
    /Add the cell id to the notebook path/)

  await assert.rejects(() => tools.get("edit").execute(
    { filePath: "demo.ipynb:9xG7nZqB", oldString: "a", newString: "b" }, { id: "5", sessionID: "s" }),
    /Read cell demo\.ipynb:9xG7nZqB first/)
})
