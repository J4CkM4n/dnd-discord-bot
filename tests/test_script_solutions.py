"""Reviewable repairs, stale controls, private model calls and durable amendments."""

import asyncio
import json
import sqlite3
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from adapters.discord_bot import DiscordBot
from core.adventures import empty_definition
from core.model_gateway import current_turn
from core.persistence import CampaignRepository
from core.privacy import private_data
from core.script_solutions import MAX_SOLUTION_PROMPT
from core.script_store import ScriptError, ScriptStore
from core.scripts import ScriptService
from test_admins_scripts import Gateway, LINKED, NOTE, SECRET, SOURCE, admin_interaction, definition, messages
from test_campaign import run

PREFERENCE = "保留四片真碎片，但將幻覺改成全隊共同揭露。"
PROPOSAL = {"summary": "提出可供審閱的幻覺改編。", "changes": [
    {"issue": "逐角色幻覺狀態目前不受支援。", "change": "改為抵達高塔時全隊共同看見真相。",
     "impact": "所有玩家同時得知真相，取消逐人每日意志檢定。"}]}


def blocked(store, *, source=SOURCE, status="blocked"):
    draft_id = store.create(42, 123, 1, source, 2 if len(source) <= 3000 else 3)
    data = definition()
    data.update(complete=False, conversion_issues=["逐角色幻覺狀態目前不受支援。"])
    store.update(draft_id, status=status, definition=data, issues=["complete: conversion is incomplete"])
    return draft_id


def pending(store, draft_id, proposal=PROPOSAL):
    solution_id = store.begin_solution(42, 123, draft_id, 1)
    assert store.finish_solution(draft_id, solution_id, proposal=deepcopy(proposal))
    return solution_id


def view_from(it):
    return next(call.kwargs["view"] for call in reversed(messages(it)) if "view" in call.kwargs)


def button(view, action):
    return next(item for item in view.children if item.custom_id.startswith("script-solution:" + action + ":"))


def test_review_deny_refine_accept_and_explicit_approval(tmp_path, caplog):
    async def scenario():
        revised = deepcopy(PROPOSAL)
        revised["changes"][0]["change"] = "改為抵達石灘時全隊共同看見真相。"
        gateway = Gateway([PROPOSAL, revised, definition(), LINKED])
        bot = DiscordBot(gateway=gateway)
        store = bot.scripts.store
        bot.admins.change(42, 1, 99, promote=True)
        bot.admins.change(42, 2, 99, promote=True)
        # A new conversion must not replace an already selected adventure.
        initial = store.create(42, 123, 1, "old source", 2)
        store.update(initial, status="ready", definition=definition())
        selected = store.approve(42, 123, initial, 1)
        draft_id = blocked(store)
        original = store.draft(42, 123)
        status = admin_interaction()
        await bot._script_operation(status, "status")
        first_view = view_from(status)
        generated = admin_interaction()
        await button(first_view, "generate").callback(generated)
        draft = store.draft(42, 123)
        assert draft["solution"]["status"] == "pending"
        assert draft["definition"] == original["definition"]
        proposal_view = view_from(generated)
        assert {b.label for b in proposal_view.children} == {"接受並重新轉換", "拒絕建議", "補充要求／重新建議"}
        assert any(c.kwargs.get("file") and c.kwargs["file"].filename == "script-conversion-review.json"
                   for c in messages(generated))
        other = admin_interaction(2)
        await button(proposal_view, "accept").callback(other)
        assert store.draft(42, 123) == draft

        denied = admin_interaction()
        await button(proposal_view, "deny").callback(denied)
        assert store.draft(42, 123)["solution"]["status"] == "denied"
        assert store.draft(42, 123)["definition"] == original["definition"]
        assert not bot.scripts.tasks
        modal_it = admin_interaction()
        modal_it.response.send_modal = AsyncMock()
        await button(view_from(denied), "regenerate").callback(modal_it)
        modal = modal_it.response.send_modal.call_args.args[0]
        modal.prompt._value = PREFERENCE
        refined = admin_interaction()
        await modal.on_submit(refined)
        draft = store.draft(42, 123)
        assert draft["solution"]["status"] == "pending"
        assert draft["solution"]["id"] != proposal_view.solution_id
        assert PREFERENCE in gateway.calls[1][1] and '"status": "denied"' in gateway.calls[1][1]

        stale = admin_interaction()
        await button(proposal_view, "accept").callback(stale)
        assert "已更新" in messages(stale)[-1].args[0]
        assert store.draft(42, 123) == draft
        accepted = admin_interaction()
        await button(view_from(refined), "accept").callback(accepted)
        updated = store.draft(42, 123)
        assert updated["draft_id"] != draft_id and updated["status"] == "preparing"
        assert updated["source_text"] == SOURCE
        assert updated["conversion_guidance"][0]["changes"] == revised["changes"]
        assert store.selected(42, 123)["version_id"] == selected
        await asyncio.gather(*bot.scripts.tasks.values())
        updated = store.draft(42, 123)
        assert updated["status"] == "ready" and not updated["issues"]
        assert revised["changes"][0]["change"] in gateway.calls[2][1]
        assert SOURCE in gateway.calls[2][1]
        assert store.selected(42, 123)["version_id"] == selected
        version = store.approve(42, 123, updated["draft_id"], 1)
        with store.repository.connect() as conn:
            row = conn.execute("SELECT source_text,conversion_guidance FROM adventure_versions WHERE version_id=?",
                               (version,)).fetchone()
        assert row[0] == SOURCE and json.loads(row[1]) == updated["conversion_guidance"]
        assert all(c.kwargs["ephemeral"] for it in (status, generated, denied, refined, stale, accepted, other)
                   for c in messages(it))
        assert not bot._v4_games
        await bot.scripts.close()
    run(scenario())
    assert all(value not in caplog.text for value in (SOURCE, SECRET, NOTE, PREFERENCE))


@pytest.mark.parametrize("identity", ["other-admin", "demoted", "dm", "other-guild", "other-channel"])
@pytest.mark.parametrize("action", ["generate", "accept", "deny", "modal", "modal-submit"])
def test_solution_actions_recheck_owner_current_admin_and_scope(identity, action):
    async def scenario():
        gateway = Gateway()
        bot = DiscordBot(gateway=gateway)
        bot.admins.change(42, 1, 99, promote=True)
        bot.admins.change(42, 2, 99, promote=True)
        store = bot.scripts.store
        draft_id = blocked(store)
        solution_id = pending(store, draft_id)
        it = admin_interaction(2 if identity == "other-admin" else 1)
        it.response.send_modal = AsyncMock()
        if identity == "demoted":
            bot.admins.change(42, 1, 99, promote=False)
        elif identity == "dm":
            it.guild = it.guild_id = None
        elif identity == "other-guild":
            it.guild.id = it.guild_id = 777
        elif identity == "other-channel":
            it.channel_id = 456
        before = store.draft(42, 123)
        if action in ("generate", "modal-submit"):
            await bot._suggest_script(it, draft_id, PREFERENCE, solution_id)
        elif action == "modal":
            await bot._prompt_script_solution(it, draft_id, solution_id)
        else:
            await bot._decide_script_solution(it, draft_id, solution_id, accept=action == "accept")
        assert store.draft(42, 123) == before
        assert not gateway.calls and not bot.scripts.tasks and not bot._v4_games
        assert not it.response.send_modal.called
        assert all(c.kwargs["ephemeral"] for c in messages(it))
        await bot.scripts.close()
    run(scenario())


def test_regeneration_and_stale_modal_cannot_overwrite_a_new_upload(tmp_path):
    async def scenario():
        store = ScriptStore(CampaignRepository(str(tmp_path / "drafts.db")))
        draft_id = blocked(store)
        old_id = pending(store, draft_id)
        new_id = store.begin_solution(42, 123, draft_id, 1, PREFERENCE, old_id)
        assert not store.finish_solution(draft_id, old_id, proposal=PROPOSAL)
        with pytest.raises(ScriptError, match="已更新"):
            store.decide_solution(42, 123, draft_id, old_id, 1, accept=True)
        replacement = store.create(42, 123, 1, "replacement source", 2)
        assert not store.finish_solution(draft_id, new_id, proposal=PROPOSAL)
        with pytest.raises(ScriptError, match="替換"):
            store.begin_solution(42, 123, draft_id, 1, PREFERENCE, new_id)
        assert store.draft(42, 123)["draft_id"] == replacement
        assert store.draft(42, 123)["conversion_guidance"] == []
        assert store.draft(42, 123)["solution"] is None
    run(scenario())


@pytest.mark.parametrize("response", [RuntimeError(SOURCE + SECRET + PREFERENCE), {"wrong": SECRET}])
def test_suggestion_failures_are_private_and_keep_conversion_blocked(tmp_path, caplog, response):
    async def scenario():
        store = ScriptStore(CampaignRepository(str(tmp_path / "drafts.db")))
        draft_id = blocked(store, status="failed")
        gateway = Gateway([response, response])
        service = ScriptService(store, gateway)
        await service.suggest(42, 123, draft_id, 1, PREFERENCE)
        draft = store.draft(42, 123)
        assert draft["status"] == "failed" and draft["solution"]["status"] == "failed"
        assert all(value not in draft["solution"]["error"] for value in (SOURCE, SECRET, PREFERENCE))
        assert len(gateway.calls) <= 2
        with pytest.raises(ScriptError):
            store.approve(42, 123, draft_id, 1)
        await service.close()
    run(scenario())
    assert all(value not in caplog.text for value in (SOURCE, SECRET, PREFERENCE))


def test_suggestion_schema_repair_is_bounded_private_and_has_its_own_budget(tmp_path):
    class PrivateGateway(Gateway):
        async def object(self, role, prompt):
            assert private_data() and current_turn().remaining > 0
            current_turn().calls += 1
            return await super().object(role, prompt)

    async def scenario():
        store = ScriptStore(CampaignRepository(str(tmp_path / "drafts.db")))
        draft_id = blocked(store)
        gateway = PrivateGateway([{"summary": SECRET, "changes": []}, PROPOSAL])
        service = ScriptService(store, gateway)
        await service.suggest(42, 123, draft_id, 1)
        assert store.draft(42, 123)["solution"]["status"] == "pending"
        assert len(gateway.calls) == 2 and "solution.changes" in gateway.calls[1][1]
        assert not private_data() and current_turn() is None
        await service.close()
    run(scenario())


def test_restart_resumes_suggestions_and_accepted_amendments_on_every_chunk(tmp_path):
    async def scenario():
        path = str(tmp_path / "drafts.db")
        store = ScriptStore(CampaignRepository(path))
        draft_id = blocked(store, source="a" * 3001)
        store.begin_solution(42, 123, draft_id, 1, PREFERENCE)
        resumed = ScriptService(ScriptStore(CampaignRepository(path)), Gateway([PROPOSAL]))
        resumed.resume()
        await asyncio.gather(*resumed.tasks.values())
        solution = resumed.store.draft(42, 123)["solution"]
        assert solution["status"] == "pending" and solution["prompt"] == PREFERENCE
        new_id = resumed.store.decide_solution(42, 123, draft_id, solution["id"], 1, accept=True)
        await resumed.close()
        gateway = Gateway([definition(), empty_definition(), LINKED])
        restarted = ScriptService(ScriptStore(CampaignRepository(path)), gateway)
        restarted.resume()
        await asyncio.gather(*restarted.tasks.values())
        draft = restarted.store.draft(42, 123)
        assert draft["draft_id"] == new_id and draft["status"] == "ready"
        assert draft["source_text"] == "a" * 3001
        assert all(PROPOSAL["changes"][0]["change"] in prompt for _, prompt in gateway.calls[:2])
        await restarted.close()
    run(scenario())


def test_demotion_during_model_work_does_not_disclose_the_result():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowGateway(Gateway):
            async def object(self, role, prompt):
                entered.set()
                await release.wait()
                return await super().object(role, prompt)

        bot = DiscordBot(gateway=SlowGateway([PROPOSAL]))
        bot.admins.change(42, 1, 99, promote=True)
        draft_id = blocked(bot.scripts.store)
        it = admin_interaction()
        task = asyncio.create_task(bot._suggest_script(it, draft_id))
        await entered.wait()
        bot.admins.change(42, 1, 99, promote=False)
        release.set()
        await task
        assert bot.scripts.store.draft(42, 123)["solution"]["status"] == "pending"
        assert all(PROPOSAL["summary"] not in str(c) and not c.kwargs.get("file") for c in messages(it))
        await bot.scripts.close()
    run(scenario())


def test_prompt_limit_and_ready_drafts_do_not_start_suggestion_calls(tmp_path):
    store = ScriptStore(CampaignRepository(str(tmp_path / "drafts.db")))
    draft_id = blocked(store)
    with pytest.raises(ScriptError, match="2000"):
        store.begin_solution(42, 123, draft_id, 1, "x" * (MAX_SOLUTION_PROMPT + 1))
    assert store.draft(42, 123)["solution"] is None
    replacement = store.create(42, 123, 1, SOURCE, 2)
    store.update(replacement, status="ready", definition=definition())
    with pytest.raises(ScriptError, match="只有轉換失敗"):
        store.begin_solution(42, 123, replacement, 1)


def test_accepting_a_proposal_does_not_bypass_unresolved_conversion_errors(tmp_path):
    async def scenario():
        store = ScriptStore(CampaignRepository(str(tmp_path / "drafts.db")))
        draft_id = blocked(store)
        solution_id = pending(store, draft_id)
        unresolved = definition()
        unresolved.update(complete=False, conversion_issues=["逐角色幻覺仍未能轉換。"])
        service = ScriptService(store, Gateway([unresolved, LINKED]))
        new_id = service.decide_solution(42, 123, draft_id, solution_id, 1, accept=True)
        await asyncio.gather(*service.tasks.values())
        draft = store.draft(42, 123)
        assert draft["status"] == "blocked" and draft["issues"]
        assert draft["definition"]["conversion_issues"] == unresolved["conversion_issues"]
        with pytest.raises(ScriptError):
            store.approve(42, 123, new_id, 1)
        assert store.selected(42, 123) is None
        await service.close()
    run(scenario())


def test_generation_rechecks_demotion_after_discord_acknowledgment():
    async def scenario():
        bot = DiscordBot(gateway=Gateway())
        bot.admins.change(42, 1, 99, promote=True)
        draft_id = blocked(bot.scripts.store)
        before = bot.scripts.store.draft(42, 123)
        it = admin_interaction()

        async def acknowledge(**kwargs):
            bot.admins.change(42, 1, 99, promote=False)

        it.response.defer.side_effect = acknowledge
        await bot._suggest_script(it, draft_id, PREFERENCE)
        assert bot.scripts.store.draft(42, 123) == before
        assert not bot.model_gateway.calls and not bot.scripts.tasks
        await bot.scripts.close()
    run(scenario())


def test_v3_migration_preserves_private_drafts_and_immutable_selected_versions(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE script_drafts (
                guild_id TEXT,channel_id TEXT,draft_id TEXT UNIQUE,submitted_by TEXT,source_text TEXT,
                status TEXT,completed INTEGER DEFAULT 0,total INTEGER,definition TEXT,issues TEXT DEFAULT '[]',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(guild_id,channel_id));
            CREATE TABLE adventure_versions (
                version_id TEXT PRIMARY KEY,draft_id TEXT UNIQUE,guild_id TEXT,channel_id TEXT,
                submitted_by TEXT,source_text TEXT,definition TEXT,approved_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE script_selections (guild_id TEXT,channel_id TEXT,version_id TEXT,PRIMARY KEY(guild_id,channel_id));
            PRAGMA user_version=3;
        """)
        conn.execute("INSERT INTO script_drafts(guild_id,channel_id,draft_id,submitted_by,source_text,status,total) "
                     "VALUES ('42','123','draft','1',?,'failed',2)", (SOURCE,))
        conn.execute("INSERT INTO adventure_versions VALUES ('version','approved','42','123','1',?,?,CURRENT_TIMESTAMP)",
                     (SOURCE, json.dumps(definition())))
        conn.execute("INSERT INTO script_selections VALUES ('42','123','version')")
    store = ScriptStore(CampaignRepository(path))
    assert store.draft(42, 123)["source_text"] == SOURCE
    assert store.draft(42, 123)["conversion_guidance"] == []
    assert store.draft(42, 123)["solution"] is None
    assert store.selected(42, 123)["version_id"] == "version"
    with store.repository.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT conversion_guidance FROM adventure_versions").fetchone()[0] == "[]"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE adventure_versions SET source_text='changed'")
    assert ScriptStore(CampaignRepository(path)).selected(42, 123)["version_id"] == "version"
