# Multi-User Workspace Support｜多使用者 Workspace 隔離

> 繁體中文 / English bilingual

---

## 問題 Problem

nanobot 原本設計為個人使用的 agent。單一 `AgentLoop` 實例預設共用同一個 workspace，造成：

nanobot is designed as a personal agent. By default, a single `AgentLoop` shares one workspace across all users:

| 元件 Component | 原本行為 Before | 說明 |
|---|---|---|
| `sessions/*.jsonl` | ✅ 已隔離 Already isolated | Session key = `channel:chat_id`，本來就各自分開 |
| `USER.md` | ❌ 共用 Shared | Dream 會把 User A 的個人資料寫進去，User B 看到的是錯的 |
| `memory/MEMORY.md` | ❌ 共用 Shared | 所有人的對話事實混在一起，越多 user 越亂 |
| `SOUL.md` | ✅ 共用是預期行為 Intentionally shared | Bot 人格，所有人共用是正確的 |

---

## 解法 Solution

每個 user 根據其 `session_key` 取得獨立的 workspace 子目錄，所有記憶相關狀態（`USER.md`、`MEMORY.md`、`history.jsonl`）都在該目錄內。

Each user gets an isolated workspace subdirectory derived from their `session_key`. All memory state lives inside that directory.

```
~/.nanobot/workspace/              ← base workspace（共用 shared）
├── SOUL.md                        ← bot 人格，所有人共用 / bot personality, shared
├── sessions/                      ← 對話歷史，本來就 per-session / already isolated
│   ├── api_alice.jsonl
│   └── api_bob.jsonl
└── users/                         ← per-user workspaces（新增 new）
    ├── api_alice/
    │   ├── USER.md                ← Alice 的個人資料 / Alice's profile
    │   └── memory/
    │       ├── MEMORY.md          ← Alice 的長期記憶 / Alice's long-term memory
    │       └── history.jsonl
    └── api_bob/
        ├── USER.md
        └── memory/
            ├── MEMORY.md
            └── history.jsonl
```

---

## 使用方式 How to Use

呼叫 `POST /v1/chat/completions` 時，在 request body 帶入 `session_id` 識別使用者：

Include `session_id` in the request body to identify the user:

```bash
curl -X POST http://localhost:8900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}], "session_id":"alice"}'
```

Session key 會變成 `api:alice`，nanobot 自動建立並使用 `users/api_alice/` 作為 Alice 獨立的 workspace。

The session key becomes `api:alice`, and nanobot automatically creates and uses `users/api_alice/` as Alice's isolated workspace.

---

## 改動的檔案 Files Changed

### 1. `nanobot/agent/autocompact.py`

**改了什麼 What changed**

`AutoCompact.__init__` 的 `consolidator` 參數現在接受 `Consolidator` 實例**或** `Callable[[str], Consolidator]`。

The `consolidator` parameter now accepts either a `Consolidator` instance or a `Callable[[str], Consolidator]`.

**為什麼 Why**

`AutoCompact._archive(key)` 在背景壓縮閒置 session 時，需要呼叫**該 user 自己的** consolidator（指向該 user 的 `history.jsonl`），而不是一個全域的。改成接受 factory callable 後，archive 時才根據 session key 動態取得正確的 consolidator。

`AutoCompact._archive(key)` runs background session compression. It needs the correct user's consolidator (pointing to that user's `history.jsonl`), not a global one. A factory callable lets it resolve the right consolidator per session key at runtime.

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

### 2. `nanobot/agent/loop.py`（主要改動 Main change）

#### 2a. `__init__` — 保存設定、新增 per-user 快取 / Store config, add per-user caches

```python
# 保存以便建立 per-user 物件時重用
# Stored for reuse when creating per-user objects
self._timezone = timezone
self._disabled_skills = disabled_skills
self._dream_model_override: str | None = None
self._dream_max_batch_size: int | None = None
self._dream_max_iterations: int | None = None
self._dream_annotate_line_ages: bool = True

# Per-user 物件快取，key = session_key
# Per-user object caches, keyed by session_key
self._user_contexts: dict[str, ContextBuilder] = {}
self._user_consolidators: dict[str, Consolidator] = {}
self._user_dreams: dict[str, Dream] = {}

# 傳入 factory callable，而非單一 consolidator 實例
# Pass factory callable instead of a single consolidator instance
self.auto_compact = AutoCompact(
    sessions=self.sessions,
    consolidator=self._get_user_consolidator,
    session_ttl_minutes=session_ttl_minutes,
)
```

#### 2b. 三個新的 helper methods / Three new helper methods

```python
def _user_workspace(self, session_key: str) -> Path:
    """根據 session_key 建立並回傳對應的 user workspace 目錄。
    Return (and create) an isolated workspace directory per session_key."""
    safe_key = safe_filename(session_key.replace(":", "_"))
    ws = self.workspace / "users" / safe_key
    ws.mkdir(parents=True, exist_ok=True)
    return ws

def _get_user_context(self, session_key: str) -> ContextBuilder:
    """Lazy-create 並快取該 user 的 ContextBuilder。
    Lazy-create and cache a ContextBuilder for this user's workspace."""
    if session_key not in self._user_contexts:
        self._user_contexts[session_key] = ContextBuilder(
            self._user_workspace(session_key),
            timezone=self._timezone,
            disabled_skills=self._disabled_skills,
        )
    return self._user_contexts[session_key]

def _get_user_consolidator(self, session_key: str) -> Consolidator:
    """Lazy-create 並快取該 user 的 Consolidator。
    Lazy-create and cache a Consolidator backed by this user's MemoryStore."""
    if session_key not in self._user_consolidators:
        ctx = self._get_user_context(session_key)
        self._user_consolidators[session_key] = Consolidator(
            store=ctx.memory, ...
        )
    return self._user_consolidators[session_key]

def _get_user_dream(self, session_key: str) -> Dream:
    """Lazy-create 並快取該 user 的 Dream processor。
    Lazy-create and cache a Dream processor for this user's workspace."""
    if session_key not in self._user_dreams:
        ctx = self._get_user_context(session_key)
        d = Dream(store=ctx.memory, ...)
        # 套用 CLI 設定的 dream config overrides
        # Apply dream config overrides set by the CLI
        ...
        self._user_dreams[session_key] = d
    return self._user_dreams[session_key]
```

#### 2c. `_process_message` — 改用 per-user 物件 / Use per-user objects

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

---

### 3. `nanobot/command/builtin.py`

**改了什麼 What changed**

`/dream` 指令改呼叫該 user 自己的 Dream 實例，而非全域的。

The `/dream` command now runs the user's own Dream instance instead of the global one.

```python
# Before
did_work = await loop.dream.run()

# After
did_work = await loop._get_user_dream(ctx.key).run()
```

`ctx.key` 是送出 `/dream` 那個 user 的 session key，確保只處理自己的記憶。

`ctx.key` is the session key of the user who sent `/dream`, ensuring only their own memory is processed.

---

### 4. `nanobot/cli/commands.py`

**改了什麼 What changed**

Dream config 設定（model、batch size 等）除了套用在 base `dream` 實例上，也儲存在 `AgentLoop` 的欄位裡，讓後續建立的 per-user Dream 繼承相同設定。

Dream config overrides are stored on `AgentLoop` in addition to the base instance, so per-user Dream instances created later inherit the same settings.

```python
# Before
agent.dream.model = dream_cfg.model_override
agent.dream.max_batch_size = dream_cfg.max_batch_size

# After
agent.dream.model = dream_cfg.model_override           # base dream（CLI 排程用）
agent._dream_model_override = dream_cfg.model_override # per-user dream 繼承用
agent.dream.max_batch_size = dream_cfg.max_batch_size
agent._dream_max_batch_size = dream_cfg.max_batch_size
```

---

## 沒有更動的部分 What is NOT Changed

| 元件 Component | 隔離狀態 Isolation | 原因 Reason |
|---|---|---|
| `SOUL.md` | 共用 Shared | Bot 人格，刻意全域 / Bot personality, intentionally global |
| `sessions/*.jsonl` | Per session_key | 改動前就已隔離 / Already isolated before this change |
| `SessionManager` | 共用實例 Shared instance | Session 檔名本來就包含 user key |
| `ToolRegistry` | 共用 Shared | Tools 無狀態，per-call 執行 / Stateless per-call |
| LLM provider | 共用 Shared | 無狀態 HTTP client / Stateless HTTP client |
| MCP connections | 共用 Shared | Server 層級資源 / Server-level resources |
| CLI dream 排程 | Base workspace | Cron 跑的是全域的；per-user dream 只透過 `/dream` 指令觸發 |

---

## 跟上 Upstream 更新 Keeping Up with Upstream

改動刻意控制在 4 個檔案。Rebase 時的注意事項：

Changes are intentionally minimal and contained to 4 files. When rebasing on upstream:

- **`loop.py`** — 風險最高 Highest risk。注意 upstream 對 `_process_message`、`self.consolidator`、`self.context`、`maybe_consolidate_by_tokens` 簽名的更動。若 upstream 在 `_process_message` 裡新增了 `self.consolidator` 或 `self.context` 的呼叫，套用同樣的模式替換成 `self._get_user_consolidator(key)` / `self._get_user_context(key)`。
- **`autocompact.py`** — 低風險 Low risk。只改了 `__init__` 簽名和 `_archive` 內部。
- **`builtin.py`** — 極小 Minimal。只改了一行。
- **`cli/commands.py`** — 極小 Minimal。Dream config 區塊多複製一份到 `agent._dream_*`。
