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

test("cell path parser only accepts .ipynb paths with an optional numeric suffix", () => {
  assert.deepEqual(parseNotebookPath("notebooks/demo.ipynb:5"), {
    notebook: "notebooks/demo.ipynb", index: 5,
  })
  assert.deepEqual(parseNotebookPath("demo.ipynb"), { notebook: "demo.ipynb", index: null })
  assert.equal(parseNotebookPath("demo.ipynb:abc"), null)
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

  const nearMiss = await tools.get("read").execute({ filePath: "demo.ipynb:abc" }, { id: "2", sessionID: "s" })
  assert.equal(nearMiss.output, "read original output")

  await assert.rejects(() => tools.get("write").execute(
    { filePath: "demo.ipynb", content: "x = 1" }, { id: "3", sessionID: "s" }),
    /Add :N to the notebook path/)

  await assert.rejects(() => tools.get("edit").execute(
    { filePath: "demo.ipynb", oldString: "a", newString: "b" }, { id: "4", sessionID: "s" }),
    /Add :N to the notebook path/)

  await assert.rejects(() => tools.get("edit").execute(
    { filePath: "demo.ipynb:5", oldString: "a", newString: "b" }, { id: "5", sessionID: "s" }),
    /Read cell 5 first/)
})
