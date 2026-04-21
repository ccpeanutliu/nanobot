# Multi-User Workspace Support

This document describes the changes made to support multiple users with fully isolated workspaces when running nanobot as a shared service (e.g. via `nanobot serve`).

## Problem

nanobot is designed as a personal agent. By default, a single `AgentLoop` instance shares one workspace across all users, meaning:

- **Conversation history** (`sessions/*.jsonl`) — already isolated per `session_key` (`channel:chat_id`), no problem.
- **`USER.md`** — shared. Dream writes user-specific personal info here. User B would see User A's profile.
- **`memory/MEMORY.md`** — shared. Facts learned from all users mix together over time.
- **`SOUL.md`** — shared, but this is intentional (it's the bot's personality, not user data).

## Solution

Each user gets their own workspace subdirectory derived from their `session_key`. All memory-related state (`USER.md`, `MEMORY.md`, `history.jsonl`, Dream cursor files) lives inside that subdirectory.

```
~/.nanobot/workspace/              ← base workspace (shared)
├── SOUL.md                        ← bot personality, shared across all users
├── sessions/                      ← conversation history, already per-session
│   ├── api_alice.jsonl
│   └── api_bob.jsonl
└── users/                         ← per-user workspaces (new)
    ├── api_alice/
    │   ├── USER.md                ← Alice's profile
    │   └── memory/
    │       ├── MEMORY.md          ← Alice's long-term memory
    │       └── history.jsonl
    └── api_bob/
        ├── USER.md
        └── memory/
            ├── MEMORY.md
            └── history.jsonl
```

## How to Use

When calling `POST /v1/chat/completions`, include a `session_id` that identifies the user:

```json
{
  "messages": [{"role": "user", "content": "Hello"}],
  "session_id": "alice"
}
```

The `session_key` becomes `api:alice`, and nanobot automatically creates and uses `users/api_alice/` as Alice's isolated workspace.

## Files Changed

### 1. `nanobot/agent/autocompact.py`

**What changed:** The `consolidator` parameter in `AutoCompact.__init__` now accepts either a `Consolidator` instance or a `Callable[[str], Consolidator]`.

**Why:** `AutoCompact._archive(key)` runs background session compression. It needs to call the correct user's consolidator (which points to that user's `history.jsonl`), not a single global one. By accepting a factory callable, `AutoCompact` can resolve the right consolidator per session key at archive time.

```python
# Before
def __init__(self, sessions, consolidator: Consolidator, ...):
    self.consolidator = consolidator

async def _archive(self, key):
    summary = await self.consolidator.archive(archive_msgs)

# After
def __init__(self, sessions, consolidator: "Consolidator | Callable[[str], Consolidator]", ...):
    self._consolidator_or_factory = consolidator

async def _archive(self, key):
    factory = self._consolidator_or_factory
    consolidator = factory(key) if callable(factory) else factory
    summary = await consolidator.archive(archive_msgs)
```

---

### 2. `nanobot/agent/loop.py`

This is the main change. Three areas were modified:

#### 2a. `__init__` — store config for reuse, add per-user caches

```python
# Store so per-user objects can be created with the same settings
self._timezone = timezone
self._disabled_skills = disabled_skills
self._dream_model_override: str | None = None
self._dream_max_batch_size: int | None = None
self._dream_max_iterations: int | None = None
self._dream_annotate_line_ages: bool = True

# Per-user object caches (keyed by session_key)
self._user_contexts: dict[str, ContextBuilder] = {}
self._user_consolidators: dict[str, Consolidator] = {}
self._user_dreams: dict[str, Dream] = {}

# Pass factory instead of single consolidator to AutoCompact
self.auto_compact = AutoCompact(
    sessions=self.sessions,
    consolidator=self._get_user_consolidator,   # callable, not instance
    session_ttl_minutes=session_ttl_minutes,
)

# self.dream and self.context still exist for the CLI scheduler
# and CronTool (base workspace), but are not used for user messages.
```

#### 2b. Three new helper methods

```python
def _user_workspace(self, session_key: str) -> Path:
    """Return (and create) a workspace subdirectory isolated per session_key."""
    safe_key = safe_filename(session_key.replace(":", "_"))
    ws = self.workspace / "users" / safe_key
    ws.mkdir(parents=True, exist_ok=True)
    return ws

def _get_user_context(self, session_key: str) -> ContextBuilder:
    """Lazy-create and cache a ContextBuilder for this user's workspace."""
    if session_key not in self._user_contexts:
        self._user_contexts[session_key] = ContextBuilder(
            self._user_workspace(session_key),
            timezone=self._timezone,
            disabled_skills=self._disabled_skills,
        )
    return self._user_contexts[session_key]

def _get_user_consolidator(self, session_key: str) -> Consolidator:
    """Lazy-create and cache a Consolidator backed by this user's MemoryStore."""
    if session_key not in self._user_consolidators:
        ctx = self._get_user_context(session_key)
        self._user_consolidators[session_key] = Consolidator(
            store=ctx.memory, ...
        )
    return self._user_consolidators[session_key]

def _get_user_dream(self, session_key: str) -> Dream:
    """Lazy-create and cache a Dream processor for this user's workspace."""
    if session_key not in self._user_dreams:
        ctx = self._get_user_context(session_key)
        d = Dream(store=ctx.memory, ...)
        # Apply any dream config overrides set by the CLI
        ...
        self._user_dreams[session_key] = d
    return self._user_dreams[session_key]
```

#### 2c. `_process_message` — use per-user objects instead of shared ones

```python
# Before
await self.consolidator.maybe_consolidate_by_tokens(session, session_summary=pending)
initial_messages = self.context.build_messages(...)
self._schedule_background(self.consolidator.maybe_consolidate_by_tokens(session, ...))

# After
user_ctx = self._get_user_context(key)
user_consolidator = self._get_user_consolidator(key)
await user_consolidator.maybe_consolidate_by_tokens(session, session_summary=pending)
initial_messages = user_ctx.build_messages(...)
self._schedule_background(user_consolidator.maybe_consolidate_by_tokens(session, ...))
```

The same change applies to the system message path (subagent messages).

---

### 3. `nanobot/command/builtin.py`

**What changed:** The `/dream` in-chat command now runs the user's own Dream instance instead of the global one.

```python
# Before
did_work = await loop.dream.run()

# After
did_work = await loop._get_user_dream(ctx.key).run()
```

`ctx.key` is the session key of the user who sent `/dream`, so they only consolidate their own memory.

---

### 4. `nanobot/cli/commands.py`

**What changed:** Dream config overrides (model, batch size, iterations, age annotation) are now stored on `AgentLoop` in addition to being applied to the base `dream` instance. This ensures per-user Dream instances created later inherit the same settings.

```python
# Before
agent.dream.model = dream_cfg.model_override
agent.dream.max_batch_size = dream_cfg.max_batch_size
...

# After
agent.dream.model = dream_cfg.model_override          # base dream (CLI scheduler)
agent._dream_model_override = dream_cfg.model_override # stored for per-user dreams
agent.dream.max_batch_size = dream_cfg.max_batch_size
agent._dream_max_batch_size = dream_cfg.max_batch_size
...
```

## What is NOT Changed

| Component | Isolation | Reason |
|-----------|-----------|--------|
| `SOUL.md` | Shared | Bot personality — intentionally global |
| `sessions/*.jsonl` | Per session_key | Already isolated before this change |
| `SessionManager` | Shared instance | Stores sessions in base workspace; session filenames already include the user key |
| `ToolRegistry` | Shared | Tools (file, web, shell) are stateless per-call |
| LLM provider | Shared | Stateless HTTP client |
| MCP connections | Shared | Server-level resources, not user-specific |
| CLI dream scheduler | Base workspace | Runs on cron; per-user dream runs only via `/dream` command |

## Keeping Up with Upstream

The changes are intentionally minimal and contained to 4 files. When rebasing on upstream:

- **`loop.py`** — highest risk. Watch for changes to `_process_message`, `self.consolidator`, `self.context`, and `maybe_consolidate_by_tokens` call signatures.
- **`autocompact.py`** — low risk. Only `__init__` signature and `_archive` body changed.
- **`builtin.py`** — minimal. One line changed.
- **`cli/commands.py`** — minimal. Dream config block duplicated onto `agent._dream_*`.

When upstream adds a new call to `self.consolidator` or `self.context` inside `_process_message`, apply the same pattern: replace with `self._get_user_consolidator(key)` / `self._get_user_context(key)`.
