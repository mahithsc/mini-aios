# Memory

Mini AIOS uses two complementary forms of memory: a small curated snapshot
that is always available to the agent, and an on-demand search over complete
chat transcripts.

The design is adapted from the core principles of the
[Hermes Agent memory system](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/):
bounded curated context for important facts and cheap on-demand recall for
historical detail. The implementation is native to the Mini AIOS prompt, tool,
and SQLite architecture.

## Curated memory

Curated memory lives outside individual chat directories:

- Development: `<project>/memories/`
- Production: `~/.mini-aios/memories/`

The two stores are:

| File | Purpose | Limit |
| --- | --- | ---: |
| `MEMORY.md` | Projects, environment, conventions, decisions, and lessons | 2,200 characters |
| `USER.md` | Identity, preferences, communication style, and workflow habits | 1,375 characters |

Entries are separated by a line containing `§`. The agent manages entries with
the `memory` tool using `add`, `replace`, and `remove`. Replacements and removals
use a unique substring rather than an unstable generated identifier.

Writes are locked and atomically renamed so readers never observe a partially
written file. Exact duplicates, ambiguous edits, over-capacity writes,
instruction-shaped payloads, invisible control characters, and common secret
formats are rejected. Files are scanned again when building the prompt; unsafe
entries placed on disk manually are replaced with a blocked marker.

The snapshot is rebuilt when Mini AIOS constructs an agent for a turn. The
OpenAI Agents runtime creates a fresh agent for every message, so a successful
memory write is visible on the next turn without changing the active model
request midway through execution.

## Conversation recall

Every persisted user and assistant message is projected into
`chat_search_documents` in the existing `aios.db`. SQLite FTS5 indexes the text
when available; a bounded `LIKE` search is used as a compatibility fallback.

The `session_search` tool supports three modes:

- No arguments: list recent chats.
- `chat_id` only: browse recent messages from a chat.
- `query`: search actual message text across chats, optionally filtered by
  `chat_id`.

Search does not call a model or summarize results. Returned text is treated as
untrusted historical data and cannot override current user or system
instructions.

The index is maintained in the same transactions as chat writes. Existing
messages are backfilled when the database initializes after this feature is
installed.

## Saving policy

Save information when it will prevent the user from having to repeat durable
context, including:

- explicit preferences and corrections;
- stable project or environment facts;
- conventions and important decisions;
- compact lessons likely to matter again.

Do not save credentials, raw private data, temporary task state, large excerpts,
complete transcripts, or facts that can be rediscovered cheaply. Full historical
detail belongs in transcript search rather than curated memory.

## Deliberate first-version boundaries

This implementation does not include external memory providers, autonomous
post-turn review calls, or a write-approval queue. Those can be added later
without changing the two-layer storage model.
