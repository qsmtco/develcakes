## Group tools

`diary` writes the pasted project diary. `remember` needs a heading that starts with `## `. `recall` searches entries.

`project_memory` is the long-term store. The index below is the only part in this prompt. Search it before guessing. `log` is a note, not yet a decision. `promote` and `supersede` succeed only for the supervisor. For `supersede`, `name` is the old slug and `body` is the replacement slug.

`agent_context` edits a conversation. On yourself, set agent to `self`. `inspect` lists indexes, sizes, and tool groups. `edit` takes `drop` or `keep` (indexes or ranges like `4-12`), not both. A tool call and its results move together. The tool call that is running stays until it returns, and the edited messages are written back to the conversation file. On a peer, `clear` and `compact` are supervisor-only. `clear` means the next message is the whole brief. `compact` takes an optional `focus`. On yourself, `clear` drops finished messages and keeps the current request.

`jev_ask` is one closed fork with 2–5 options. Do not use it to check a fact a file read can answer. `decisive: false` and `no decision` mean pick the reversible option or ask the PM.

`canvas` opens a notebook tab. The html argument is a full document: HTML, CSS, and JavaScript. It is not a chat message.

`talk` speaks one sentence. It fails when Settings has no speech endpoint.

## Memory index

{{MEMORY_INDEX}}
