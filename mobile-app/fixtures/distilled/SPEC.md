# Distilled live-stream transform — canonical contract (R107)

This is the **language-neutral specification** for the *distilled* rendering of a
Roost agent job's live stream. The CLI is the reference implementation
(`roost.cli.distill_log_line`); iOS (R108) and Android (R109) **MUST** mirror
these rules exactly. The golden fixtures in `cases.json` next to this file pin
the transform: every client's distiller, run over each case's `raw`, must produce
that case's `distilled` (where `null` means *suppress the line*).

## Why distill

An agent job (`kind: claude` / `kind: auto`) runs
`claude -p --output-format stream-json --verbose`. The worker relays **each raw
stdout line** as a `log` SSE event whose `data` field is **one line of Anthropic
stream-json** — the `{"type": ...}` streaming envelope. Shown raw, this is a
firehose: base64 `signature` blobs on `thinking` blocks, 100 KB `tool_result`
bodies, init banners, rate-limit pings, and JSON wrappers around one line of
actual assistant text.

The distilled view turns each line into at most one readable line, suppressing
the noise. It is the **default**; the raw firehose is available behind
`--verbose` / `--raw` (CLI) or a "raw" toggle (mobile, default off).

## Input

One string: the `data` field of a single `log` SSE event (equivalently, one
element of `GET /jobs/{id}/logs` `logs[].data`). This is one *line*; the worker
relays line-by-line, so each call sees exactly one stream-json message (or one
line of a `command` job's plain stdout).

## Output

A single string to display, or **suppress** (`null` / `None`) to show nothing.
A returned string may itself contain `\n` (an assistant message with several
content blocks distils to one line per block, joined by `\n`).

## Rules (apply in order)

1. **Not JSON → passthrough verbatim.** If the trimmed line does not start with
   `{`, or fails to parse as a JSON object, return it unchanged. This preserves
   a `command` job's plain stdout and any non-JSON worker output. *(Never lose a
   line — when in doubt, show it.)*

2. **JSON object without a recognized stream-json `type` → passthrough
   verbatim.** Roost's own internal event envelopes (e.g.
   `{"type": "started", ...}`) and any unknown JSON shape pass through unchanged,
   so nothing roost-internal is silently dropped.

3. **Recognized Anthropic stream-json envelopes** distil by `type`:

   | `type`             | condition                     | distilled output                              |
   |--------------------|-------------------------------|-----------------------------------------------|
   | `system`           | `subtype == "init"`           | `🔎 starting…`  *(phase divider)*             |
   | `system`           | any other subtype             | **suppress** (`init`-noise, `thinking_tokens`)|
   | `rate_limit_event` | —                             | **suppress**                                  |
   | `result`           | `is_error` truthy             | `✗ failed`  *(phase divider)*                 |
   | `result`           | otherwise                     | `✓ done`  *(phase divider)*                   |
   | `assistant`/`user` | see content-block rules below | joined block lines, or **suppress** if empty  |

4. **`assistant` / `user` content blocks.** Read `message.content`:
   - If it is a **string**, show its first-line, whitespace-collapsed, capped at
     **200** chars (suppress if empty).
   - If it is a **list**, map each block and join the non-empty results with
     `\n`; suppress the whole line if nothing survives:
     - `type == "text"` → the text, whitespace-collapsed, first 200 chars.
     - `type == "tool_use"` → `→ <name>: <hint>` where `<hint>` is the first
       present value among input keys
       `command, file_path, path, pattern, query, url, description, prompt, intent`,
       whitespace-collapsed and capped at **80** chars. If no hint key is
       present, just `→ <name>`.
     - `type == "tool_result"` → `  ⎿ <summary>` (note the two leading spaces and
       the `⎿` continuation glyph). `<summary>` is the result text
       (string content, or the first `text` block of list content),
       whitespace-collapsed, first 200 chars; `(result)` if empty. If
       `is_error` is truthy, prefix the summary with `✗ ` → `  ⎿ ✗ <summary>`.
     - `type == "thinking"` / `"redacted_thinking"` → **suppress** (drops the
       reasoning text *and* the base64 `signature` blob).
     - any other block type → ignored.

5. **Truncation / collapse.** "Whitespace-collapsed" means split on any
   whitespace and rejoin with single spaces (flattens multi-line bodies to one
   line). "First N chars" appends a single `…` (U+2026) when the collapsed string
   exceeds N. Hint cap = 80; text/result cap = 200.

## Constants (must match across implementations)

- `TOOL_HINT_KEYS = [command, file_path, path, pattern, query, url, description, prompt, intent]`
- `HINT_MAX = 80`
- `RESULT_MAX = 200`
- truncation marker = `…` (U+2026)
- tool-call prefix = `→ ` ; tool-result prefix = `  ⎿ ` ; error mark = `✗ `
- phase dividers = `🔎 starting…`, `✓ done`, `✗ failed`

## Fixtures

`cases.json` — `{ version, description, cases: [ { note, source, raw, distilled } ] }`.
`source` is `captured` (a real line from a recorded `claude … stream-json` run)
or `synthesized` (representative line for a shape not present in the small
capture). `distilled: null` = suppress. Each client loads this file in its tests
and asserts `distill(case.raw) == case.distilled` for every case.
