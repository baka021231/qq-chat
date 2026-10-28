# Memory search v1

## Implementation and verification status (2026-10-28)

The manual search and Codex quiet-progress changes are merged. Local restart,
QQ connection, capability rejection and retrieval checks passed. One real owner
group turn retrieved its test phrase through the tool; that proves this call,
not general recall accuracy or a new-session benchmark. Quiet progress passed
execution-event tests and deployment checks; user-visible quiet-chat acceptance
remains a separate check. No private test phrase, group IDs or journal contents
are part of this public document.

Manually copied existing journals are snapshots, not an implemented migration
or sync pipeline. Automatic writing, current topic/preference files, correction
and forgetting, and retrieval before every message remain future work. Dated
implementation records in CHANGELOG are historical, not rollout instructions.

This phase provides one **manually invoked**, read-only `memory_search` tool.
It does not automatically save facts, inject retrieval on every message, run
embeddings, or migrate existing journals. Results are reference data and never
authorize actions. There is no separate read tool.

## Storage and visibility

Only UTF-8 `.md` / `.txt` documents under `workspace/memory/` are searched:

| Directory | Permitted owner context |
| --- | --- |
| `shared/` | Owner private or group chat |
| `private/` | Owner private chat only |
| `groups/<real-group-id>/` | Owner in that specific group only |

Visitors receive neither a grant nor tool instructions. `shared` does not give
visitors permission to search. Unclassified root files are excluded. Scope is
derived from a bridge-issued per-turn capability, never from model arguments.
Old mixed journals outside this directory stay outside retrieval; do not copy
them into `shared`. Prepare reviewed memory files separately. The tool is
read-only and does not create the directory.

Directory and file opens use retained directory descriptors with `O_NOFOLLOW`;
symbolic links (including scoped directories and the memory root), hardlinked
files, pipes, devices and hidden files are excluded. These checks constrain this
tool; FULL agents still have their existing general filesystem tools, so this
is not an OS sandbox. The repository and its trusted runtime directories must
remain controlled by the local user.

## Invocation

In existing Claude FRONT mode, `memory_search` is part of the per-turn `qq` MCP
tool set. FULL Codex/Grok/Claude owner turns instead receive a command in their
current instructions:

```text
python3 -I /trusted/repository/tools/memory_search.py --grant /private/run/mcp-....json --query "种子 语音"
```

The wrapper submits only `query` to the same Unix tool service; it cannot choose
a memory root, group, user or visibility. The grant file contains a short-lived
capability, not an account credential: never publish or print it. It is removed
and revoked after the turn, including failure/cancellation. A FULL memory-only
grant cannot invoke task dispatch, poke or task management. The CLI is the
single-agent tool interface, not a new native Codex MCP registration.

Query: 1–200 characters, at most eight whitespace-separated literal terms.
Unicode NFKC/case folding is supported. This is not semantic search: callers
should extract short keywords themselves; a natural-language paraphrase can
miss. Files rank by the number of matched terms, ties by relative filename and
line. One best matching-line excerpt per file; no fuzzy confidence claims.
A title can win a tie and omit the answer on the next line; retry with a more
specific keyword. The first version does not return neighbouring lines.

Response includes `results[{file,line,text}]`, `status`, `partial`, and (for a
completed search) `reference_only`. At most five excerpts, 500 characters each,
2000 excerpt characters total. Filenames are relative to memory. Missing root
returns `not_configured`; a completed zero-match search returns `no_results`.
Unreadable, invalid-UTF8, binary or oversized files and reached traversal budgets
set `partial`, so zero results do not falsely prove absence. Invalid inputs and
expired grants return tool errors, not empty success. No query/content logging.

Traversal limits: 256 candidate files, 4 MiB total reads, 256 KiB per file,
2048 directory entries, depth 8. A bounded scan, no persistent index and no
third-party dependencies. First version favours a small curated memory folder.

## Rollout and rollback

`memory.enabled` defaults true. False removes the FULL command/grants and prevents
memory tool calls. FRONT still advertises the fixed validated seven-tool set,
but calls return a disabled error. Startup launches the existing private Unix
service when memory is enabled, even without a background profile. No automatic
QQ messages or speech changes. Memory files remain gitignored with the workspace.

Develop on a separate branch, run tests and update FEATURES/CHANGELOG. Report the
result before merge/restart approval. Restart only after the reply is delivered.
Rollback code/config to the previous revision; read-only memory files need no
schema migration and remain untouched.

## Verification

Tests use synthetic files, temporary databases and real Unix/CLI transport.
Cover owner/visitor and cross-group isolation, revoked capabilities, task-tool
denial, invalid path/scope inputs, symlinks/hardlinks/pipes, bounded scans,
normalization, source line numbers and result budgets. Existing provider policy
and streaming tests must continue passing. These do **not** establish a real
model's recall accuracy: QQ acceptance and new-model semantic recall are separate
follow-up checks; the single successful owner group retrieval noted above is
a limited real-call check, not a replacement for those benchmarks.

## Quiet Codex lookup replies

In single-agent owner Codex turns with a memory grant, commentary is buffered
until the provider starts non-memory work. A direct bridge-issued memory_search
command (or qq MCP memory_search event) discards the pending commentary; its
tool-detail events are hidden even with show_tool_progress. Normal commands,
file changes and unknown tools release the buffer and keep subsequent progress.
Plain conversation without work also discards the pending commentary. Final
answer streaming, runtime errors and interruption semantics are unchanged.

Only the exact direct Python invocation is recognized, including an optional
`bash -lc` shell wrapper; compound commands or arbitrary helper scripts are
work, rather than relying on text that merely mentions memory. The current
request instructions ask the model to use that command quietly. This gate
is specific to Codex's execution events; Claude/Grok answer/progress stream
classification has not been changed by this patch.

## Writing and topic memory: design only

Date-based journals preserve the sequence of events. A proposed second view
would maintain current preferences, project state and effective decisions by
topic, linking back to dated evidence. Neither a topic writer nor rules for
when/where to update those files have been implemented. Do not infer that the
existing search tool automatically maintains either view. Explicit requests to
remember, inferred preferences and verified completion require different
evidence; the latest request must take precedence over an older default.

## Daily rules and scheduled reflection (local arrangement)

The owner's local OWNER.md contains the daily diary rule and is reloaded for
each authorized owner turn; the full reflection rule is not included there.
The separate local workspace/memory-rules/reflection.md is read explicitly by
the existing nightly organizer when it runs (this installation uses 22:00
Australia/Sydney). The rule is part of the organizer's prompt and checkpoint
signature. Missing/empty rules fail without overwriting source records; a rule
change during generation also prevents saving that output.

The current organizer still produces only log/story outputs. It cannot create
or update topic files. Rules for creating new topics, updating existing current
conclusions and preserving diary evidence are written down for that later
stage; their presence does not mean a topic writer is enabled. Daily records
stay in the existing our-story/development-logs layout, with OUR_STORY as index.
No live model run is required just to save rules; the existing timer need not be
restarted to pick up the file on its next run. Local rules, organizer, tests and
private records remain excluded from the public repository.
