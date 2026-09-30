import { spawn } from "node:child_process"
import { realpath } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const script = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
  "scripts/jupyter_cells.py")
const python = process.env.JUPYTER_PAIR_PYTHON || "python3"
const viewed = new Map()

function cacheKey(sessionID, notebook) {
  return `${sessionID}\0${notebook}`
}

function view(sessionID, notebook) {
  return viewed.get(cacheKey(sessionID, notebook))
}

function remember(sessionID, notebook, cells) {
  viewed.set(cacheKey(sessionID, notebook), {
    cells: new Map(cells.filter((cell) => cell.id).map((cell) => [cell.id, cell])),
  })
}

function cellView(sessionID, notebook, id) {
  return view(sessionID, notebook)?.cells.get(id)
}

function outputsText(outputs = []) {
  return outputs.map((output) => {
    switch (output.output_type) {
      case "stream": return `\n--- ${output.name || "stdout"} ---\n${output.text}`
      case "execute_result": return `\n--- result ---\n${output.text || ""}`
      case "error": return `\n--- error ---\n${output.ename}: ${output.evalue}\n${(output.traceback || []).join("\n")}`
      case "display_data": return `\n--- display: ${(output.mime_types || []).join(", ")} ---\n${output.text || ""}`
      default: return ""
    }
  }).join("")
}

function bridge(notebook, request, cwd, signal) {
  return new Promise((resolve) => {
    const child = spawn(python, [script, notebook, "rpc"], {
      cwd,
      stdio: ["pipe", "pipe", "pipe"],
      signal,
    })
    let stdout = ""
    let stderr = ""
    child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk })
    child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk })
    child.on("error", (error) => resolve({ ok: false, error: error.message }))
    child.on("close", (code) => {
      let result
      try {
        result = JSON.parse(stdout)
      } catch {
        resolve({ ok: false, error: stderr.trim() || stdout.trim() || `jupyter-pair exited ${code}` })
        return
      }
      if (code !== 0 && result.ok !== false) {
        resolve({ ok: false, error: stderr.trim() || `jupyter-pair exited ${code}` })
        return
      }
      resolve(result)
    })
    child.stdin.end(JSON.stringify(request))
  })
}

export function parseNotebookPath(value) {
  if (typeof value !== "string") return null
  const match = value.match(/^(.*\.ipynb)(?::([A-Za-z0-9_-]+))?$/i)
  if (!match) return null
  const suffix = match[2]
  return {
    notebook: match[1],
    index: suffix !== undefined && /^\d+$/.test(suffix) ? Number(suffix) : null,
    id: suffix !== undefined && !/^\d+$/.test(suffix) ? suffix : null,
  }
}

const PATH_KEYS = ["filePath", "path"]
const NOTEBOOK_HINT = " For .ipynb notebooks: read the bare path for the whole-notebook projection — each cell header is `# %% <type> id=<cell id> [exec=<execution number>]`, and that id is the cell's address. Editing a notebook's raw JSON on disk is the wrong move: `write`/`edit` REQUIRE the `:<cell id>` suffix (e.g. demo.ipynb:9xG7nZqB) and a fresh read of that cell; `jupyter.add_cell`/`run_cell`/`delete_cell` take a separate `id` argument instead of a `:` suffix."

const PROJECTION_HINT = (notebook, count) =>
  `${count} cells. Cell addresses are the ids in the \`%% <type> id=…\` headers — target a cell as ${path.basename(notebook)}:<id> with read/edit/write, and pass jupyter.add_cell/run_cell/delete_cell a separate id argument (after_id for placement). Never patch the raw JSON file on disk. Exec numbers after the id are the kernel execution counts of each code cell.`
const SUFFIX_HINT = "Pass the notebook path and the cell id separately, e.g. path: \"demo.ipynb\", id: \"9xG7nZqB\", instead of a `:<id>` suffix."

function notebookSelector(input) {
  for (const key of PATH_KEYS) {
    const parsed = parseNotebookPath(input?.[key])
    if (parsed) return { ...parsed, key }
  }
  return null
}

function fileEnvelope(notebook, kind, body, cellsNote) {
  const cells = cellsNote ? `<cells>${cellsNote}</cells>\n` : ""
  return `<path>${notebook}</path>\n<type>${kind}</type>\n${cells}<content>\n${body}\n</content>`
}

function paginate(text, offset, limit) {
  const lines = text.split("\n")
  const start = Math.max(0, (offset ?? 1) - 1)
  const slice = lines.slice(start, start + (limit ?? 2000))
  const last = start + slice.length
  const body = slice.map((line, i) => `${start + i + 1}: ${line}`).join("\n")
  const note = last < lines.length
    ? `\n\n(Showing lines ${start + 1}-${last} of ${lines.length}. Use offset=${last + 1} to continue.)`
    : `\n\n(End of file - total ${lines.length} lines)`
  return `${body}${note}`
}

export default {
  id: "jupyter-pair",
  async setup(ctx) {
    const directoryFor = async (sessionID) => {
      if (typeof ctx.session?.get === "function") {
        try {
          return (await ctx.session.get({ sessionID })).location.directory
        } catch {
          // fall through to the plugin location
        }
      }
      return ctx.location.directory
    }
    const resolveNotebook = async (rawPath, sessionID) => {
      const resolved = path.resolve(await directoryFor(sessionID), rawPath)
      try {
        return await realpath(resolved)
      } catch {
        return resolved
      }
    }
    const requireNotebook = async (rawPath, sessionID) => {
      if (typeof rawPath !== "string" || !rawPath.toLowerCase().endsWith(".ipynb")) {
        throw new Error("path must name a .ipynb notebook")
      }
      return resolveNotebook(rawPath, sessionID)
    }
    const suffixError = (rawPath) =>
      typeof rawPath === "string" && /\.ipynb:[A-Za-z0-9_-]+$/i.test(rawPath) ? { content: SUFFIX_HINT } : null

    async function readNotebookOutput(selector, input, context) {
      const notebook = await resolveNotebook(selector.notebook, context.sessionID)
      if (selector.index !== null) {
        return fileOutput(notebook,
          "Notebook cells are addressed by their stable id now, not by index — read the bare path (`" +
          path.basename(selector.notebook) + "`) to get each cell's `%% … id=…` header, then target cells as " +
          path.basename(selector.notebook) + ":<id>.", "text/plain")
      }
      const result = await bridge(notebook,
        selector.id ? { op: "read", cell_id: selector.id } : { op: "snapshot" },
        await directoryFor(context.sessionID), context.signal)
      if (!result.ok) return fileOutput(notebook, result.error, "text/plain")
      if (!selector.id) {
        remember(context.sessionID, notebook, result.cells)
        return fileOutput(notebook,
          fileEnvelope(notebook, "file", paginate(result.text, input.offset, input.limit),
            PROJECTION_HINT(notebook, result.cells.length)),
          "application/x-ipynb+json")
      }
      const key = cacheKey(context.sessionID, notebook)
      const entry = viewed.get(key) || { cells: new Map() }
      entry.cells.set(result.cell.id, result.cell)
      viewed.set(key, entry)
      const cell = result.cell
      const exec = cell.execution_count ?? null
      return fileOutput(notebook,
        fileEnvelope(notebook, "notebook-cell", `${cell.source || ""}${outputsText(cell.outputs)}`,
          `This projection shows cell id=${cell.id}${cell.cell_type === "code" ? ` (exec=${exec})` : " (markdown)"} of ${path.basename(notebook)}; to mutate or run it, address ${path.basename(notebook)}:${cell.id} (read/edit/write) or pass id: "${cell.id}" (jupyter.* tools).`),
        "text/plain")
    }

    // The read/write/edit output schemas require a FileContent object; plain
    // strings fail validation ("Tool returned an invalid value for its output
    // schema"). Wrap every text result we return through these tools.
    function fileOutput(notebook, content, mime) {
      return {
        output: {
          type: "file",
          uri: `file://${notebook}`,
          name: path.basename(notebook),
          content,
          encoding: "utf8",
          mime,
        },
      }
    }

    async function mutateNotebookOutput(selector, operation, input, context) {
      // write/edit only accept { files, replacements } out of the tool output
      // schema; guide the model with thrown errors (native behavior) instead.
      if (!selector.id) {
        throw new Error(`Add the cell id to the notebook path to target a cell, e.g. ${selector.notebook}:9xG7nZqB — the ids are the \`%% … id=…\` headers in the whole-notebook read.`)
      }
      const notebook = await resolveNotebook(selector.notebook, context.sessionID)
      const snapshot = cellView(context.sessionID, notebook, selector.id)
      if (!snapshot) {
        throw new Error(`Read cell ${selector.notebook}:${selector.id} first, then retry.`)
      }
      const request = {
        op: operation,
        cell_id: selector.id,
        expected_hash: snapshot.source_hash,
      }
      if (operation === "write") {
        if (typeof input.content !== "string") {
          throw new Error("write needs string content for a notebook cell.")
        }
        request.source = input.content
      } else {
        if (typeof input.oldString !== "string" || typeof input.newString !== "string") {
          throw new Error("edit needs oldString and newString for a notebook cell.")
        }
        request.old_string = input.oldString
        request.new_string = input.newString
        request.replace_all = input.replaceAll === true
      }
      const result = await bridge(notebook, request, await directoryFor(context.sessionID), context.signal)
      if (!result.ok) throw new Error(result.error)
      view(context.sessionID, notebook)?.cells.set(result.cell.id, result.cell)
      // Each native tool validates its own output schema:
      //   edit → { files: FileDiff[], replacements: number }
      //   write → { operation: "write", target, resource, existed }
      if (operation === "write") {
        return {
          output: { operation: "write", target: notebook, resource: `${notebook}:${selector.id}`, existed: true },
          content: `Updated ${notebook}:${selector.id} live.`,
        }
      }
      return {
        output: {
          files: [{ file: `${notebook}:${selector.id}`, patch: "", additions: 1, deletions: 1, status: "modified" }],
          replacements: 1,
        },
        content: `Updated ${notebook}:${selector.id} live.`,
      }
    }

    async function runCell(input, context) {
      try {
        const bad = suffixError(input.path)
        if (bad) return bad
        const notebook = await requireNotebook(input.path, context.sessionID)
        if (typeof input.id !== "string" || !input.id) {
          return { content: "jupyter.run_cell needs the cell's id argument — the `%% … id=…` values from the notebook read." }
        }
        const snapshot = cellView(context.sessionID, notebook, input.id)
        if (!snapshot) return { content: `Read cell ${path.basename(notebook)}:${input.id} first, then retry.` }
        const result = await bridge(notebook, { op: "run", cell_id: input.id, timeout: input.timeout,
          expected_hash: snapshot.source_hash },
        await directoryFor(context.sessionID), context.signal)
        if (!result.ok) return { content: result.error }
        const updated = { ...snapshot, execution_count: result.execution_count, outputs: result.outputs || [] }
        view(context.sessionID, notebook)?.cells.set(updated.id, updated)
        return { content: `ran cell ${input.id} [exec ${result.execution_count}]${outputsText(result.outputs)}` }
      } catch (error) {
        return { content: `jupyter-pair: ${error.message}` }
      }
    }

    async function addCell(input, context) {
      try {
        const bad = suffixError(input.path)
        if (bad) return bad
        const notebook = await requireNotebook(input.path, context.sessionID)
        if (input.after_id !== undefined && typeof input.after_id !== "string") {
          return { content: "after_id must be a cell id string (from the `%% … id=…` headers)." }
        }
        const result = await bridge(notebook, { op: "add", source: input.source,
          cell_type: input.cell_type || "code", after_id: input.after_id },
        await directoryFor(context.sessionID), context.signal)
        if (!result.ok) return { content: result.error }
        view(context.sessionID, notebook)?.cells.set(result.cell.id, result.cell)
        return { content: `Added ${result.cell.cell_type} cell id=${result.cell.id} live.` }
      } catch (error) {
        return { content: `jupyter-pair: ${error.message}` }
      }
    }

    async function deleteCell(input, context) {
      try {
        const bad = suffixError(input.path)
        if (bad) return bad
        const notebook = await requireNotebook(input.path, context.sessionID)
        if (typeof input.id !== "string" || !input.id) {
          return { content: "jupyter.delete_cell needs the cell's id argument — the `%% … id=…` values from the notebook read." }
        }
        const snapshot = cellView(context.sessionID, notebook, input.id)
        if (!snapshot) return { content: `Read cell ${input.id} first, then retry.` }
        const result = await bridge(notebook, { op: "delete", cell_id: input.id,
          expected_hash: snapshot.source_hash },
        await directoryFor(context.sessionID), context.signal)
        if (!result.ok) return { content: result.error }
        const cells = view(context.sessionID, notebook)?.cells
        if (cells) cells.delete(input.id)
        return { content: `Deleted cell ${input.id} live.` }
      } catch (error) {
        return { content: `jupyter-pair: ${error.message}` }
      }
    }

    // Grep over the notebook projection instead of the raw JSON. Match
    // FileSystem.Match: { entry: { path, type }, line, offset, text,
    // submatches: [{ text, start, end }] }. Line numbers are 1-based lines of
    // the whole-notebook read projection, so they work directly as a
    // follow-up read(offset: line, limit) and each hit's nearest `# %% … id=…`
    // marker above it names the cell id to target.
    async function grepNotebookOutput(selector, input, context) {
      if (typeof input.pattern !== "string" || input.pattern.length === 0) {
        throw new Error("Pattern must not be empty")
      }
      if (selector.index !== null) {
        throw new Error(`Notebook cells are addressed by their stable id now, not by index — read ${path.basename(selector.notebook)} to get the \`%% … id=…\` headers, then grep ${path.basename(selector.notebook)}:<id> or filter by content.`)
      }
      const notebook = await resolveNotebook(selector.notebook, context.sessionID)
      const result = await bridge(notebook, { op: "snapshot" },
        await directoryFor(context.sessionID), context.signal)
      if (!result.ok) throw new Error(result.error)
      remember(context.sessionID, notebook, result.cells)

      const lines = result.text.split("\n")
      // Region boundaries: a cell spans [startLine0, endLine0) in 0-based lines
      const cells = []
      let current = null
      for (let i = 0; i < lines.length; i++) {
        const m = lines[i].match(/^# %% (code|markdown) id=([A-Za-z0-9_-]+)(?: exec=(\S+))?$/)
        if (m) {
          if (current) current.endLine0 = i
          current = { id: m[2], type: m[1], exec: m[3], startLine0: i }
          cells.push(current)
        }
      }
      if (current) current.endLine0 = lines.length
      if (cells.length === 0) throw new Error("Projection contained no cell markers; cannot address results.")

      // include filter on cell language: *.py → code cells, *.md → markdown,
      // anything else (or missing) → all cells
      let typeFilter = null
      if (typeof input.include === "string") {
        const inc = input.include.toLowerCase()
        if (/\.py\b|\.py$/.test(inc) || /\*.py/.test(inc)) typeFilter = "code"
        else if (/\.md\b|\.md$|markdown/.test(inc)) typeFilter = "markdown"
        else if (/ipynb/.test(inc)) typeFilter = null
        else return { output: [] }
      }
      const cellId = selector.id
      if (cellId) {
        const region = cells.find((c) => c.id === cellId)
        if (!region) throw new Error(`Cell id=${cellId} not found in ${notebook}; the notebook may have changed — read it again.`)
        if (typeFilter && region.type !== typeFilter) return { output: [] }
      }

      let re
      try {
        const source = (input.literal === true)
          ? input.pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
          : input.pattern
        re = new RegExp(source, input.caseSensitive === false ? "gimu" : "gmu")
      } catch (error) {
        throw new Error(`Invalid regex pattern: ${error.message}`)
      }
      const limit = typeof input.limit === "number" && input.limit > 0 ? Math.floor(input.limit) : 100

      const matches = []
      let offset = 0
      let truncated = false
      for (let i = 0; i < lines.length && !truncated; i++) {
        const lineText = lines[i]
        const lineNextOffset = offset + lineText.length + 1
        if (lineText.length && (cellId === null || inCellOf(cells, cellId, i, typeFilter))) {
          re.lastIndex = 0
          let m
          while ((m = re.exec(lineText)) !== null) {
            const cell = cells.find((c) => i >= c.startLine0 && i < c.endLine0)
            const cellLine = cell ? i - cell.startLine0 : 0 // marker line itself is line 0's neighborhood ref
            matches.push({
              entry: { path: notebook, type: "file" },
              line: i + 1,
              offset,
              text: lineText,
              submatches: [{ text: m[0], start: m.index, end: m.index + m[0].length }],
              cellId: cell ? cell.id : null,
              cellLine,
            })
            if (matches.length > limit) { truncated = true; break }
          }
        }
        offset = lineNextOffset
      }

      const shown = matches.slice(0, limit)
      const parts = shown.length === 0 ? ["No matches found"] : [`Found ${shown.length} matches in ${notebook}`]
      let lastCell = null
      for (const hit of shown) {
        if (hit.cellId !== lastCell) {
          lastCell = hit.cellId
          const region = cells.find((c) => c.id === lastCell)
          parts.push(`cell id=${lastCell}${region?.exec ? ` (exec=${region.exec})` : ""}:`)
        }
        parts.push(`  Line ${hit.line} (cell id=${hit.cellId}, cell-line ${hit.cellLine}): ${hit.text}`)
      }
      if (truncated) {
        parts.push("", `(Showing first ${shown.length}. Use a more specific pattern or raise limit; line numbers work with read(offset:).)`)
      }
      return {
        output: shown.map(({ cellId, cellLine, ...rest }) => rest),
        content: parts.join("\n"),
        metadata: { matches: shown.length, truncated },
      }
    }

    function inCellOf(cells, notebookId, line0, typeFilter) {
      const cell = cells.find((c) => line0 >= c.startLine0 && line0 < c.endLine0)
      if (!cell) return false
      return !(typeFilter && cell.type !== typeFilter)
    }

    const pathSchema = { type: "string", description: "Path to the .ipynb notebook, absolute or relative to the session directory" }
    const idSchema = { type: "string", description: "The cell's stable id — the `id=…` in the projection's `# %% <type> id=…` header" }
    const NOTEBOOK_GREP_HINT = " For .ipynb notebooks, this searches the live cell projection instead of raw JSON: each cell header shows its `id` and kernel `exec` number, and hits report `Line N (cell id=<id>, cell-line K)` — the id targets edit/run tools and N works as `read(path, offset: N)` pagination."

    await ctx.tool.transform((editor) => {
      const originals = new Map(editor.list().map((tool) => [tool.id, tool.execute]))
      for (const name of ["read", "write", "edit"]) {
        const original = originals.get(name)
        if (typeof original !== "function" || !editor.get(name)) continue
        editor.update(name, (tool) => {
          tool.description += NOTEBOOK_HINT
          tool.execute = async (input, context) => {
            const selector = notebookSelector(input)
            if (!selector) return original(input, context)
            if (name === "read") return await readNotebookOutput(selector, input, context)
            return await mutateNotebookOutput(selector, name, input, context)
          }
        })
      }
      const grepOriginal = originals.get("grep")
      const grepTool = editor.get("grep")
      if (typeof grepOriginal === "function" && grepTool) {
        editor.update("grep", (tool) => {
          tool.description += NOTEBOOK_GREP_HINT
          tool.execute = async (input, context) => {
            const selector = notebookSelector(input)
            if (!selector) return grepOriginal(input, context)
            return await grepNotebookOutput(selector, input, context)
          }
        })
      }

      editor.add({
        name: "run_cell",
        description: "Run one code cell in its live Jupyter kernel and return the outputs. Read the cell first; target it by its stable cell id.",
        input: { type: "object", properties: { path: pathSchema, id: idSchema, timeout: { type: "number" } }, required: ["path", "id"], additionalProperties: false },
        options: { namespace: "jupyter", permission: "edit", codemode: true },
        execute: runCell,
      })
      editor.add({
        name: "add_cell",
        description: "Append a code/markdown cell to a live Jupyter notebook, or insert it after the cell with the given after_id (stable cell id from the notebook read).",
        input: { type: "object", properties: { path: pathSchema, source: { type: "string" }, cell_type: { type: "string", enum: ["code", "markdown"] }, after_id: idSchema }, required: ["path", "source"], additionalProperties: false },
        options: { namespace: "jupyter", permission: "edit", codemode: true },
        execute: addCell,
      })
      editor.add({
        name: "delete_cell",
        description: "Delete one cell from a live Jupyter notebook by its stable cell id. Read the cell first.",
        input: { type: "object", properties: { path: pathSchema, id: idSchema }, required: ["path", "id"], additionalProperties: false },
        options: { namespace: "jupyter", permission: "edit", codemode: true },
        execute: deleteCell,
      })
    })
  },
}
