// opencode custom tool: insert_at_line
//
// Inserts text into an existing file after a given 1-based line number WITHOUT rewriting the
// whole file. Lets a small-context model grow a large existing file at a precise location
// (locate it first with grep/glob, then insert) instead of re-emitting the entire file.
//
// Install: copy to ~/.config/opencode/tools/insert_at_line.ts or <project>/.opencode/tools/.
// The filename becomes the tool name: `insert_at_line`.
import { tool } from "@opencode-ai/plugin"
import * as fs from "node:fs"
import * as path from "node:path"

export default tool({
  description:
    "Insert text into an existing file AFTER a 1-based line number (line=0 prepends). Use to grow " +
    "a large existing file at a precise spot without rewriting it. Find the line with grep first.",
  args: {
    path: tool.schema.string().describe("File path, relative to the project root or absolute."),
    line: tool.schema.number().describe("1-based line number to insert AFTER (0 = prepend to top)."),
    content: tool.schema.string().describe("Text to insert verbatim (include a trailing newline)."),
  },
  async execute(args, context) {
    const root = context.worktree || context.directory || process.cwd()
    const target = path.isAbsolute(args.path) ? args.path : path.join(root, args.path)
    const lines = fs.readFileSync(target, "utf8").split("\n")
    const at = Math.max(0, Math.min(args.line, lines.length))
    lines.splice(at, 0, args.content.replace(/\n$/, ""))
    fs.writeFileSync(target, lines.join("\n"), "utf8")
    return `Inserted ${args.content.length} chars after line ${at} in ${args.path} (now ${lines.length} lines).`
  },
})
