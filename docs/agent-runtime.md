# Agent runtime boundaries

The model-facing system lives under `aios_core/agent`:

```text
aios_core/agent/
├── runtime.py          # streamed agent loop and persistence timing
├── events.py           # provider-neutral public event contract
├── context.py          # per-run/tool invocation context
├── messages.py         # chat-to-model input conversion
├── persistence.py      # SDK sessions/hooks over canonical storage
├── factory.py          # main-agent and subagent construction
├── openai.py           # OpenAI Agents SDK adapter
├── prompts/            # prompt builders, loader, and templates
├── tools/              # model-visible tools and thin domain adapters
└── pi/                 # Pi tool, process runtime, protocol, and cloud bridge
```

`AgentRuntime.run(AgentRunRequest)` is the public execution boundary. It yields
`AgentEvent` values and owns when conversation turns, native provider events,
and tool lifecycle records are persisted. `ConversationStore` owns how those
records are written to SQLite. Cron and dream jobs use the same module's
one-shot completion helper instead of importing the provider SDK themselves.

`aios_core/execution` owns queued runs and durable run projections. Its chat
runner consumes `AgentEvent` values and translates them into run events. It
does not import or drive the OpenAI SDK.

`server` owns transport and process composition: HTTP routes, SSE delivery,
authentication, startup/shutdown, and hardware presentation state. Server code
does not implement the agent loop.

Domain implementations such as memory, cron scheduling, app workspaces, and
deployment remain outside `agent`. Their files under `agent/tools` are only the
model-facing adapters.
