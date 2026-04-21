"""Tests for per-user workspace isolation (multi-user support)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.nanobot import Nanobot


def _write_config(tmp_path: Path) -> Path:
    data = {
        "providers": {"openrouter": {"apiKey": "sk-test-key"}},
        "agents": {"defaults": {"model": "openai/gpt-4.1"}},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data))
    return config_path


def _make_loop(tmp_path: Path):
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)
    return bot._loop


# ---------------------------------------------------------------------------
# _user_workspace
# ---------------------------------------------------------------------------

def test_user_workspace_is_under_users_dir(tmp_path):
    loop = _make_loop(tmp_path)
    ws = loop._user_workspace("api:alice")
    assert ws.parent == tmp_path / "users"
    assert ws.exists()


def test_different_session_keys_get_different_workspaces(tmp_path):
    loop = _make_loop(tmp_path)
    ws_alice = loop._user_workspace("api:alice")
    ws_bob = loop._user_workspace("api:bob")
    assert ws_alice != ws_bob


def test_same_session_key_gets_same_workspace(tmp_path):
    loop = _make_loop(tmp_path)
    assert loop._user_workspace("api:alice") == loop._user_workspace("api:alice")


# ---------------------------------------------------------------------------
# _get_user_context
# ---------------------------------------------------------------------------

def test_user_context_workspace_matches_user_workspace(tmp_path):
    loop = _make_loop(tmp_path)
    ctx = loop._get_user_context("api:alice")
    assert ctx.workspace == loop._user_workspace("api:alice")


def test_user_context_is_cached(tmp_path):
    loop = _make_loop(tmp_path)
    ctx1 = loop._get_user_context("api:alice")
    ctx2 = loop._get_user_context("api:alice")
    assert ctx1 is ctx2


def test_different_users_get_different_contexts(tmp_path):
    loop = _make_loop(tmp_path)
    ctx_alice = loop._get_user_context("api:alice")
    ctx_bob = loop._get_user_context("api:bob")
    assert ctx_alice is not ctx_bob
    assert ctx_alice.workspace != ctx_bob.workspace


def test_user_context_memory_store_points_to_user_workspace(tmp_path):
    loop = _make_loop(tmp_path)
    ctx = loop._get_user_context("api:alice")
    # MemoryStore.workspace should be inside users/api_alice/
    assert ctx.memory.workspace == loop._user_workspace("api:alice")


# ---------------------------------------------------------------------------
# _get_user_consolidator
# ---------------------------------------------------------------------------

def test_user_consolidator_is_cached(tmp_path):
    loop = _make_loop(tmp_path)
    c1 = loop._get_user_consolidator("api:alice")
    c2 = loop._get_user_consolidator("api:alice")
    assert c1 is c2


def test_different_users_get_different_consolidators(tmp_path):
    loop = _make_loop(tmp_path)
    c_alice = loop._get_user_consolidator("api:alice")
    c_bob = loop._get_user_consolidator("api:bob")
    assert c_alice is not c_bob


def test_user_consolidator_store_matches_user_context(tmp_path):
    loop = _make_loop(tmp_path)
    ctx = loop._get_user_context("api:alice")
    consolidator = loop._get_user_consolidator("api:alice")
    assert consolidator.store is ctx.memory


# ---------------------------------------------------------------------------
# _get_user_dream
# ---------------------------------------------------------------------------

def test_user_dream_is_cached(tmp_path):
    loop = _make_loop(tmp_path)
    d1 = loop._get_user_dream("api:alice")
    d2 = loop._get_user_dream("api:alice")
    assert d1 is d2


def test_different_users_get_different_dreams(tmp_path):
    loop = _make_loop(tmp_path)
    d_alice = loop._get_user_dream("api:alice")
    d_bob = loop._get_user_dream("api:bob")
    assert d_alice is not d_bob


def test_user_dream_store_matches_user_context(tmp_path):
    loop = _make_loop(tmp_path)
    ctx = loop._get_user_context("api:alice")
    dream = loop._get_user_dream("api:alice")
    assert dream.store is ctx.memory


# ---------------------------------------------------------------------------
# USER.md isolation（確認寫入路徑真的分開）
# ---------------------------------------------------------------------------

def test_user_md_paths_are_isolated(tmp_path):
    loop = _make_loop(tmp_path)
    user_file_alice = loop._get_user_context("api:alice").memory.user_file
    user_file_bob = loop._get_user_context("api:bob").memory.user_file
    assert user_file_alice != user_file_bob
    assert "alice" in str(user_file_alice)
    assert "bob" in str(user_file_bob)


def test_memory_md_paths_are_isolated(tmp_path):
    loop = _make_loop(tmp_path)
    mem_alice = loop._get_user_context("api:alice").memory.memory_file
    mem_bob = loop._get_user_context("api:bob").memory.memory_file
    assert mem_alice != mem_bob
