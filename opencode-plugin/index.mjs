import { spawn } from "node:child_process"
import { realpath } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const script = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
  "../scripts/jupyter_cells.py")
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
    cells: new Map(cells.map((cell) => [cell.index, cell])),
    count: cells.length,
  })
}

function cellView(sessionID, notebook, index) {
  return view(sessionID, notebook)?.cells.get(index)
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
  const match = value.match(/^(.*\.ipynb)(?::(\d+))?$/i)
  return match ? { notebook: match[1], index: match[2] === undefined ? null : Number(match[2]) } : null
}

const PATH_KEYS = ["filePath", "path"]
const NOTEBOOK_HINT = " For .ipynb notebooks, append :N to the file path to target zero-based cell N (e.g. demo.ipynb:5). Reading a .ipynb without :N returns the whole notebook; writing or editing one requires :N."
const SUFFIX_HINT = "Pass the notebook path and index separately, e.g. path: \"demo.ipynb\", index: 5 (zero-based), instead of a `:5` suffix."

function notebookSelector(input) {
  for (const key of PATH_KEYS) {
    const parsed = parseNotebookPath(input?.[key])
    if (parsed) return { ...parsed, key }
  }
  return null
}

function fileEnvelope(notebook, kind, body) {
  return `<path>${notebook}</path>\n<type>${kind}</type>\n<content>\n${body}\n</content>`
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
      typeof rawPath === "string" && /\.ipynb:\d+$/i.test(rawPath) ? { content: SUFFIX_HINT } : null

    async function readNotebookOutput(selector, input, context) {
      const notebook = await resolveNotebook(selector.notebook, context.sessionID)
      const result = await bridge(notebook,
        selector.index === null ? { op: "snapshot" } : { op: "read", index: selector.index },
        await directoryFor(context.sessionID), context.signal)
      if (!result.ok) return fileOutput(notebook, result.error, "text/plain")
      if (selector.index === null) {
        remember(context.sessionID, notebook, result.cells)
        return fileOutput(notebook, fileEnvelope(notebook, "file", paginate(result.text, input.offset, input.limit)),
          "application/x-ipynb+json")
      }
      const key = cacheKey(context.sessionID, notebook)
      const entry = viewed.get(key) || { cells: new Map(), count: undefined }
      entry.cells.set(selector.index, result.cell)
      viewed.set(key, entry)
      return fileOutput(notebook,
        fileEnvelope(notebook, "notebook-cell", `${result.cell.source || ""}${outputsText(result.cell.outputs)}`),
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
      if (selector.index === null) {
        throw new Error(`Add :N to the notebook path to target a cell, e.g. ${selector.notebook}:5 (zero-based).`)
      }
      const notebook = await resolveNotebook(selector.notebook, context.sessionID)
      const snapshot = cellView(context.sessionID, notebook, selector.index)
      if (!snapshot) {
        throw new Error(`Read cell ${selector.index} first (e.g. read ${selector.notebook}:${selector.index}), then retry.`)
      }
      const request = {
        op: operation,
        index: selector.index,
        expected_id: snapshot.id,
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
      view(context.sessionID, notebook)?.cells.set(selector.index, result.cell)
      return {
        output: {
          files: [{ file: `${notebook}:${selector.index}`, patch: "", additions: 1, deletions: 1, status: "modified" }],
          replacements: 1,
        },
        content: `Updated ${notebook}:${selector.index} live.`,
      }
    }

    async function runCell(input, context) {
      try {
        const bad = suffixError(input.path)
        if (bad) return bad
        const notebook = await requireNotebook(input.path, context.sessionID)
        const snapshot = cellView(context.sessionID, notebook, input.index)
        if (!snapshot) return { content: `Read cell ${input.index} first, then retry.` }
        const result = await bridge(notebook, { op: "run", index: input.index, timeout: input.timeout,
          expected_id: snapshot.id, expected_hash: snapshot.source_hash },
        await directoryFor(context.sessionID), context.signal)
        if (!result.ok) return { content: result.error }
        const updated = { ...snapshot, execution_count: result.execution_count, outputs: result.outputs || [] }
        view(context.sessionID, notebook)?.cells.set(input.index, updated)
        return { content: `ran cell ${input.index} [${result.execution_count}]${outputsText(result.outputs)}` }
      } catch (error) {
        return { content: `jupyter-pair: ${error.message}` }
      }
    }

    async function addCell(input, context) {
      try {
        const bad = suffixError(input.path)
        if (bad) return bad
        const notebook = await requireNotebook(input.path, context.sessionID)
        const key = cacheKey(context.sessionID, notebook)
        const entry = view(context.sessionID, notebook)
        const index = input.index
        const expected = index === undefined ? {} : {
          expected_count: entry?.count,
          before_id: index > 0 ? entry?.cells.get(index - 1)?.id ?? null : null,
          after_id: entry?.cells.get(index)?.id ?? null,
        }
        if (index !== undefined && entry?.count === undefined) {
          return { content: "Read the whole notebook (read without :N) before inserting at an index." }
        }
        const result = await bridge(notebook, { op: "add", source: input.source,
          cell_type: input.cell_type || "code", index, ...expected },
        await directoryFor(context.sessionID), context.signal)
        if (!result.ok) return { content: result.error }
        viewed.delete(key)
        return { content: `Added ${result.cell.cell_type} cell ${result.index} live.` }
      } catch (error) {
        return { content: `jupyter-pair: ${error.message}` }
      }
    }

    async function deleteCell(input, context) {
      try {
        const bad = suffixError(input.path)
        if (bad) return bad
        const notebook = await requireNotebook(input.path, context.sessionID)
        const key = cacheKey(context.sessionID, notebook)
        const snapshot = cellView(context.sessionID, notebook, input.index)
        if (!snapshot) return { content: `Read cell ${input.index} first, then retry.` }
        const result = await bridge(notebook, { op: "delete", index: input.index,
          expected_id: snapshot.id, expected_hash: snapshot.source_hash },
        await directoryFor(context.sessionID), context.signal)
        if (!result.ok) return { content: result.error }
        viewed.delete(key)
        return { content: `Deleted cell ${input.index} live.` }
      } catch (error) {
        return { content: `jupyter-pair: ${error.message}` }
      }
    }

    const pathSchema = { type: "string", description: "Path to the .ipynb notebook, absolute or relative to the session directory" }
    const indexSchema = { type: "integer", minimum: 0, description: "Zero-based notebook cell index" }

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

      editor.add({
        name: "run_cell",
        description: "Run one code cell in its live Jupyter kernel and return the outputs. Read the cell first.",
        input: { type: "object", properties: { path: pathSchema, index: indexSchema, timeout: { type: "number" } }, required: ["path", "index"], additionalProperties: false },
        options: { namespace: "jupyter", permission: "edit", codemode: true },
        execute: runCell,
      })
      editor.add({
        name: "add_cell",
        description: "Append or insert a code/markdown cell in a live Jupyter notebook.",
        input: { type: "object", properties: { path: pathSchema, source: { type: "string" }, cell_type: { type: "string", enum: ["code", "markdown"] }, index: indexSchema }, required: ["path", "source"], additionalProperties: false },
        options: { namespace: "jupyter", permission: "edit", codemode: true },
        execute: addCell,
      })
      editor.add({
        name: "delete_cell",
        description: "Delete one cell from a live Jupyter notebook. Read the cell first.",
        input: { type: "object", properties: { path: pathSchema, index: indexSchema }, required: ["path", "index"], additionalProperties: false },
        options: { namespace: "jupyter", permission: "edit", codemode: true },
        execute: deleteCell,
      })
    })
  },
}
