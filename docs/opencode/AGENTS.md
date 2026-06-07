# Working with large files on a small-context model

You are running against a **local model with a small context window** (served by posterchanAI).
Holding a whole large file in context, or emitting one in a single response, will **truncate and
cut off functions**. The context is also **shared across every file you open in a task** — reading
several whole files at once overflows it and the model loses the question entirely. Follow these
rules:

## Reading — grep first, never read a whole file to "find" something
- **Never `read` a whole file just to locate a symbol, value, or definition.** Use `grep` (or
  `glob`) to find the exact line number first, then `read` with a tight `offset`/`limit`
  (e.g. 5–30 lines) around it.
- A whole-file `read` is only acceptable for a genuinely small file (well under a few hundred
  lines) that you have already confirmed is small.
- Read just enough to make the change — a few dozen lines around the target, not the file.

## Exploring across MANY files (the small-GPU danger zone)
- When a decision needs information from several files, **do not open them all whole** — that is
  the #1 way to blow the context on a small GPU and get a failed/empty answer.
- Instead: `grep` the symbol/pattern across the tree to get a list of `file:line` hits, then
  `read` only the few relevant lines from each. Pull facts, not files.
- Prefer one targeted `grep` over many `read`s. Summarize what you learned from each file in a
  sentence before moving on, so you don't need to keep its full text in context.

## Editing an existing file
- **Never rewrite a whole file** with `write`. Use `edit` to replace the **smallest unique
  snippet** that captures your change.
- To add code at a precise location, find the line with `grep`, then use `insert_at_line`.
- After editing, `read` back only the changed region to confirm it landed correctly.

## Creating a large new file
- **Do not emit the whole file in one `write`** — it will be cut off mid-function.
- Write the first chunk with `write`, then extend the file with **`append_file`**, one
  function/class/section per call.
- After the final chunk, `read` the tail of the file to confirm it ends where you intended and no
  content was dropped.

## General
- Prefer many small, targeted tool calls over one large one.
- Keep each tool result small. If a command would dump a lot of output, narrow it (grep, head,
  line ranges).
