# Working with large files on a small-context model

You are running against a **local model with a small context window** (served by posterchanAI).
Holding a whole large file in context, or emitting one in a single response, will **truncate and
cut off functions**. Follow these rules:

## Reading
- **Never read a whole large file.** Use `grep` / `glob` to locate the relevant symbol, then
  `read` with `offset` and `limit` to pull only that window.
- Read just enough to make the change — a few dozen lines around the target, not the file.

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
