"""Adapter, reader, and final-gate regressions without a Discord connection."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.discord_bot import DiscordBot
from core.director import DirectorProposal, verify_proposal
from core.rules_core import ResolveResult
from core.state import encode_game
from health.health_board import (HealthBoardBot, read_v4_state,
                                 read_new_v4_entries, v4_summary)
from test_campaign import service, restart, run, decisions


def test_rejected_resolution_discards_prepared_director_scene(tmp_path, monkeypatch):
    s = service(tmp_path)
    before = encode_game(s.game)

    def rejected(g, intent):
        assert "dir1" in g.world.scenes
        g.ledger.add(intent.actor, "deny", "action rejected", reason="injected rejection")
        return ResolveResult(["拒絕"], accepted=False)

    monkeypatch.setattr("core.service.resolve", rejected)
    run(s.handle("依思 go 小巷", user_id="u1", structured=True))
    assert encode_game(s.game) == before
    assert encode_game(restart(tmp_path).game) == before
    assert not decisions(s)[0]["accepted"]
    assert "action_rejected" in decisions(s)[0]["reasons"]


def test_cancel_before_commit_never_publishes_stage(tmp_path):
    s = service(tmp_path)
    before = s.repository.load("t")

    async def cancelled(*args, **kwargs):
        assert s.game.world.current == "dock" and len(s.game.world.scenes) == 2
        raise asyncio.CancelledError()

    s.director.gen_scene = cancelled
    with pytest.raises(asyncio.CancelledError):
        run(s.handle("依思 go 小巷", user_id="u1", structured=True))
    assert s.repository.load("t") == before
    assert len(s.game.world.scenes) == 2


def test_generated_id_avoids_fuzzy_exit_redirection(tmp_path):
    s = service(tmp_path)
    s.director.scene["name"] = "酒館"
    run(s.handle("依思 go 新地方", user_id="u1", structured=True))
    assert s.game.world.current == "dir1"
    restored = restart(tmp_path)
    restored.director.scene["name"] = "霧中山丘"
    run(restored.handle("依思 go 山丘", user_id="u1", structured=True))
    assert restored.game.world.current == "dir2"
    assert restored.game.director_scene_count == 2


def test_engine_gate_rejects_stale_or_untyped_proposals(tmp_path):
    s = service(tmp_path)
    assert verify_proposal(s.game, DirectorProposal("item", "tavern", {"name": "木雕"})) == "stale_scene"
    assert verify_proposal(s.game, DirectorProposal("item", "dock", {"name": "木雕", "damage": 9})) == "invalid_fields"
    assert verify_proposal(s.game, DirectorProposal("item", "dock", {"name": ["木雕"]})) == "invalid_fields"
    s.game.world.here.ground_items = [("木雕", 1)]
    assert verify_proposal(s.game, DirectorProposal("item", "dock", {"name": "木雕"})) == "duplicate_item"


def test_health_reader_prefers_sqlite_and_reads_incrementally(tmp_path):
    s = service(tmp_path, "123")
    path = str(tmp_path / "v4_state_123.json")
    # A preserved pre-import file must never override current data.
    (tmp_path / "v4_state_123.json").write_text('{"party":{},"entries":[]}')
    run(s.admin_give("依思", "火把"))
    assert read_v4_state(path)["inventory"]["依思"][-1] == ["火把", 1]
    cursor, lines = read_new_v4_entries(path, 0)
    assert cursor == 1 and "火把" in lines[0]
    assert read_new_v4_entries(path, cursor) == (cursor, [])
    assert "霧錨鎮碼頭" in v4_summary(path, "test")
    board = HealthBoardBot(123, str(tmp_path / "campaign.db"))
    assert board._v4_state_files() == [path]
    # New database-only channels must appear without materialized JSON files.
    other = service(tmp_path, "456")
    assert str(tmp_path / "v4_state_456.json") in board._v4_state_files()
    assert not (tmp_path / "v4_state_456.json").exists()


def test_health_reader_still_handles_unimported_json(tmp_path):
    path = tmp_path / "v4_state_123.json"
    path.write_text(json.dumps({"entries": [
        {"turn": 1, "kind": "scene", "actor": "engine", "text": "at dock"}]}))
    assert read_new_v4_entries(str(path), 0) == (1, ["[t1][scene] engine: at dock"])


def interaction():
    return SimpleNamespace(
        id=123456789,
        channel_id=123,
        user=SimpleNamespace(id=1, display_name="player",
                             guild_permissions=SimpleNamespace(manage_guild=False)),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        channel=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.parametrize("path", ["turn", "confirm", "settle", "admin", "continue"])
@pytest.mark.parametrize("outcome", ["complete", "empty", "error"])
def test_discord_final_output_waits_for_delayed_stream_edit(path, outcome):
    async def scenario():
        started, release, produced = asyncio.Event(), asyncio.Event(), asyncio.Event()
        history, callbacks = [], []
        partial = "依思翻找濕漉漉的木甲板，海霧湧入衣領"
        final = partial + "。老船長低聲說：「霧裡的東西在等你。」"

        async def edit(*, content):
            if content.endswith("▍"):
                started.set()
                await release.wait()
            history.append(content)

        async def delete():
            history.append("deleted")

        msg = SimpleNamespace(edit=AsyncMock(side_effect=edit),
                              delete=AsyncMock(side_effect=delete))

        async def narrate(*args, on_delta=None, on_resolved=None, **kwargs):
            callback = on_delta or args[0]
            callbacks.append(callback)
            if on_resolved:
                await on_resolved(["檢定失敗"])
            callback(partial)
            await started.wait()
            # A slow edit is still in flight when the service completes.
            callback(partial + "，霧更濃了")
            produced.set()
            if outcome == "error":
                raise RuntimeError("narration failed")
            text = final if outcome == "complete" else ""
            return (bool(text), text) if path == "continue" else ([], text)

        bot = DiscordBot()
        svc = SimpleNamespace(
            game=SimpleNamespace(_pending_check={"event_id": 17},
                                 combat=SimpleNamespace(active=False)),
            handle=narrate, confirm=narrate, settle_roll=narrate,
            continue_narration=narrate, has_unfinished=lambda: True,
            nudge=lambda: "繼續探索。")
        bot._v4_games["123"] = svc
        it = interaction()
        it.channel.send.return_value = msg
        if path == "continue":
            work = bot.tree.get_command("continue").callback(it)
        elif path == "settle":
            work = bot._v4_settle(it, "d20")
        elif path == "admin":
            work = bot._v4_admin(it, "依思", "搜索")
        else:
            work = bot._v4_turn(it, "搜索", confirmation=path == "confirm")
        task = asyncio.create_task(work)
        await asyncio.wait_for(produced.wait(), timeout=2)
        try:
            assert not history, "final output must wait for the pending edit"
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=2)
        assert history[0] == partial + " ▍"
        if outcome == "complete":
            assert history[-1] == ("📖（續）" if path == "continue" else "📖 ") + final
            assert "▍" not in history[-1] and "[PC1]" not in history[-1]
        elif outcome == "empty" or path == "continue":
            assert history[-1] == "deleted"
        else:
            assert history[-1] == "⚠️ 回覆中斷。請用 `/status` 查看結果，或 `/continue` 取得敘事與下一步。"
        callbacks[0](final)  # A late callback cannot restart a closed stream.
        await asyncio.sleep(0)
        assert len(history) == 2

    run(scenario())


def test_live_narration_restores_names_and_keeps_continuation_private(tmp_path):
    s = service(tmp_path)
    updates, continued, inputs = [], [], []
    partial = "霧中，[PC1]緊握欄杆"
    continuation = "，[PC2]低聲說：「它還在海裡。」"

    async def narrate(*args, on_delta=None, **kwargs):
        for i in range(1, len(partial) + 1):
            on_delta(partial[:i])
        return partial

    async def continue_text(text, on_delta=None, **kwargs):
        inputs.append(text)
        for i in range(1, len(continuation) + 1):
            on_delta(continuation[:i])
        return continuation

    s.narrator.narrate = narrate
    s.narrator.continue_text = continue_text
    _, text = run(s.handle("依思 rest", user_id="u1", structured=True,
                           on_delta=updates.append))
    assert text == "霧中，依思緊握欄杆" and s.has_unfinished()
    assert s._unfinished["text"] == partial
    ok, text = run(s.continue_narration(continued.append))
    assert ok and text == "，大力蕉低聲說：「它還在海裡。」"
    assert inputs == [partial] and not s.has_unfinished()
    assert updates[-1] == "霧中，依思緊握欄杆"
    assert continued[-1] == text
    assert all("[" not in text and "PC" not in text for text in updates + continued)


def test_discord_confirm_and_roll_route_to_authorized_service(tmp_path):
    bot = DiscordBot()
    svc = SimpleNamespace(
        game=SimpleNamespace(_pending_check={"event_id": 17}),
        confirm=AsyncMock(return_value=([], "")),
        settle_roll=AsyncMock(return_value=([], "")))
    bot._v4_games["123"] = svc
    it = interaction()
    run(bot.tree.get_command("confirm").callback(it))
    assert svc.confirm.await_args.args == ("1",)
    assert svc.confirm.await_args.kwargs["admin"] is False
    run(bot._v4_settle(it, "d20"))
    args = svc.settle_roll.await_args.kwargs
    assert args["user_id"] == "1" and args["expected_event_id"] == 17
    assert args["admin"] is False


def test_concurrent_confirm_consumes_action_once(tmp_path):
    s = service(tmp_path)
    from core.intent import Intent
    s.pending = Intent("move", actor="依思", destination="小巷")
    s.pending_user_id = "u1"
    s._save()

    async def scenario():
        await asyncio.gather(s.confirm("u1"), s.confirm("u1"))

    run(scenario())
    assert len(s.director.calls) == 1
    assert s.game.ledger.turn == 1
    assert not restart(tmp_path).pending


def test_admin_confirmation_remains_bound_to_admin_identity(tmp_path):
    s = service(tmp_path)
    from core.intent import Intent
    s.pending = Intent("move", actor="依思", destination="小巷")
    s.pending_user_id, s.pending_admin = "admin1", True
    s._save()
    run(s.confirm("admin1"))
    run(s.confirm("admin2", admin=True))
    assert s.pending and not s.director.calls
    run(s.confirm("admin1", admin=True))
    assert s.game.world.current == "dir1" and not s.pending
