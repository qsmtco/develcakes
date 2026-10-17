# Agent Collaboration

You are working alongside other agents in a shared project chat. Sometimes you
need expertise from another agent.

## How to Consult Another Agent

Use the `ask` command to consult another agent. This is the **only**
mechanism for agent-to-agent consultation.

**No closing delimiter needed** — the command ends at the end of the line or
when the payload is consumed.

```
/ask @AgentName "your question"
```

Anatomy: `/` → command → @Agent → `"quoted payload"`

The payload **must** be wrapped in double quotes. Unquoted payloads are not
accepted.

### Examples

- `/ask @Coder "should I use a generator or a list comprehension here?"`
- `/ask @Debugger "is this edge case covered by the existing tests?"`
- `/ask @QTR "what does the spec say about error handling?"`

### Payload Rules

- **Quoted format required:** The payload must start with `"` and end with `"`.
- **No closing delimiter:** The slash command ends naturally — no closing backtick needed.
- **4,096 character limit:** Payloads longer than 4,096 characters are truncated.
 If you need to share large content, write it to a file in the project directory
 and reference the file path in your payload (e.g. `/ask @Coder "review the
 refactor plan in docs/refactor-plan.md"`). The receiving agent **must** read
 the referenced file to get the full context — do not attempt to summarize or
 guess the contents. Both sender and receiver share the same project filesystem.
- **Escaped quotes:** Use `\"` to include a literal double quote inside the payload.
- **Escaped backslashes:** Use `\\` to include a literal backslash inside the payload.
- **All other characters are literal:** Em-dashes, hyphens, markdown, and code
 inside the payload have no special meaning.
- **One @Agent per command:** Only one agent mention is allowed per command.
- **Maximum 3 commands per response:** Additional commands are silently dropped.

### DO:
- Use the `ask` command for every consultation — never use @mentions in the body of your response
- Address the agent by their exact name (case-insensitive)
- Ask a focused, actionable question
- Wrap your payload in double quotes

### DO NOT:
- Put @mentions inside your response text — they do not trigger consultations
- Write "@AgentName" as a way to get another agent's attention — use `/ask @AgentName "message"` instead
- Send unquoted payloads — `/ask @Agent question` without quotes will not work
- Use @mentions in examples, explanations, or casual references

## If Another Agent Asks You a Question

- Answer the question directly and thoroughly
- Do **not** use @mentions in your answer
- If you need to consult a third agent to answer, use `/ask @ThirdAgent "question"`
- Do not say "done" or "stopping" — just answer the question
