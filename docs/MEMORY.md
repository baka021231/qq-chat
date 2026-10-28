# Memory search v1

## Implementation and verification status (2026-10-28)

The manual search and Codex quiet-progress changes are merged. Local restart,
QQ connection, capability rejection and retrieval checks passed. One real owner
group turn retrieved its test phrase through the tool; that proves this call,
not general recall accuracy or a new-session benchmark. Quiet progress passed
execution-event tests and deployment checks; user-visible quiet-chat acceptance
remains a separate check. No private test phrase, group IDs or journal contents
are part of this public document.

Direct diary storage is merged and active in this installation. Daily writing,
the nightly organizer and private retrieval use workspace/memory/private/diary/.
Queries search it directly without copying or refreshing a cache. After migration
was verified, the owner authorized deletion of the old workspace journals and
the retired diary-current / existing-records snapshots; only the canonical diary
directory remains under private here. Other installations must verify their own
migration before removing sources (see Direct diary storage).

Automatic fact extraction, topic-file writing, correction/forgetting workflows
and retrieval before every message remain future work. Dated implementation
records in CHANGELOG are historical, not rollout instructions.

This phase provides one **manually invoked**, read-only `memory_search` tool.
It does not automatically save new facts, inject retrieval on every message,
run embeddings, or migrate journals. Diary writing and offline migration are
separate from retrieval. Results are reference data and never authorize actions.
There is no separate read tool.

The excerpt/runtime-command repair is implemented separately from the prior
rollout. An isolated real Codex thread retrieved fresh random values in two
consecutive turns over the Unix tool service; old tokens were rejected and grant
files removed. This is not the live QQ private-chat acceptance or deployment.

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
them into `shared`. Prepare reviewed memory files separately. Search is read-only:
it does not create or update records, managed copies or caches.

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

Codex receives the current command as a trusted developer history item via
`thread/inject_items` immediately before `turn/start`. Repeated `thread/resume`
on a loaded thread does not refresh developer instructions in the tested local
version. A turn without a terminal memory grant instead receives a reminder not
to reuse old paths. Injection failure stops execution; it never falls back to a
revoked command. Other thread configuration is unchanged.

CLI failures include a fixed `error` and a `code`: `grant_missing`,
`grant_invalid`, `grant_expired`, `service_unavailable`, `service_timeout`,
`request_rejected`, or `invalid_response`. These distinguish local grant files,
transport failures and server rejection without printing capability contents.

Query: 1–200 characters, at most eight whitespace-separated literal terms.
Unicode NFKC/case folding is supported. This is not semantic search: callers
should extract short keywords themselves; a natural-language paraphrase can
miss. Matches rank by the number of matched terms; body lines beat Markdown
headings on ties within a file. Up to three distinct excerpts per file include
bounded neighbouring context. Equivalent-strength first excerpts from other
files precede second excerpts from one file. The returned line is the matching
line, not necessarily the beginning of the context. No fuzzy confidence claims.

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
topic, linking back to dated evidence. Topic creation/update rules are saved in
the local reflection rules, but no topic writer has been implemented. Authorized
owner conversations manually maintain dated records under the local daily rules;
this is separate from automatic fact extraction. Do not infer that search
maintains journals or topic files. Explicit requests to
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
live directly under memory/private/diary/ with our-story/development-logs
and OUR_STORY.md as the date index; the local migration and rule switch are complete.
No live model run is required just to save rules; the existing timer need not be
restarted to pick up the file on its next run. Local rules, organizer, tests and
private records remain excluded from the public repository.

## Direct diary storage

Daily records have one canonical location: workspace/memory/private/diary/.
The first-person daily pages live in our-story/YYYY-MM/YYYY-MM-DD.md, technical
logs in development-logs/YYYY-MM/YYYY-MM-DD.md, and OUR_STORY.md is the index.
The owner's local writing rules and nightly organizer must switch together.
Reflection rules remain outside memory in memory-rules/reflection.md.

ToolServer calls memory.search directly; it does not copy sources, acquire the
organizer lock, or run a model before searching. Private owner queries include
private/diary; groups still see only shared/ and their own groups/<id>/ scope.
The service excludes private/diary-current and the generated
existing-records-<date>-<time>-<microseconds> snapshots. Other manual memory is
preserved. These exclusions remain for compatibility even when the retired
copies have been deleted, as on this installation. No all-chat capture, topic
writer or semantic search is added.

For an existing installation, tools/migrate_diaries.py validates the fixed old
OUR_STORY, our-story and development-logs sources; --apply copies them into the
canonical directory under the nightly lock. It retains all originals as frozen
recovery material; it does not delete them. Subsequent cleanup is a separate,
owner-authorized action after verifying canonical records. Existing identical
destination files are skipped; different
content aborts before any writes. Source whitelist, no-follow descriptor opens,
hardlink rejection and bounded reads protect the offline migration. A partial
copy can be retried; it is not a multi-file transaction. Migration is never run
by a query, visitor or group call. Do not rerun it over edited canonical records.

Deployment must back up local rules and organizer, migrate current originals,
switch OWNER.md and nightly paths, and verify byte equality plus direct lookup.
Do not treat merging public code as proof that private local files were moved.
If rolling back, preserve newer canonical diary entries before restoring old
writers. No symlinks or ongoing duplicate synchronization are required.
