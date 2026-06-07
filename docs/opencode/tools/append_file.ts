// opencode custom tool: append_file
//
// Builds a large file incrementally instead of emitting the whole thing in one `write` tool
// call. With a small-context local model (served by posterchanAI), a single whole-file write
// gets truncated mid-function; appending one chunk per call keeps each turn small and lossless.
//
// Install: copy to ~/.config/opencode/tools/append_file.ts (global) or <project>/.opencode/tools/.
// The filename becomes the tool name: `append_file`.
import { tool } from "@opencode-ai/plugin"
import * as fs from "node:fs"
import * as path from "node:path"

export default tool({
  description:
    "Append text to the end of a file, creating it (and parent dirs) if it does not exist. " +
    "Use this to build a large NEW file in small chunks (e.g. one function/class per call) " +
    "instead of writing the whole file at once, which truncates on small-context models.",
  args: {
    path: tool.schema.string().describe("File path, relative to the project root or absolute."),
    content: tool.schema.string().describe("Text to append verbatim (include a trailing newline)."),
  },
  async execute(args, context) {
    const root = context.worktree || context.directory || process.cwd()
    const target = path.isAbsolute(args.path) ? args.path : path.join(root, args.path)
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.appendFileSync(target, args.content, "utf8")
    const lines = fs.readFileSync(target, "utf8").split("\n").length
    return `Appended ${args.content.length} chars to ${args.path} (now ~${lines} lines).`
  },
})
