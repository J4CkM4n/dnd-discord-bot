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
        channel_id=123,
        user=SimpleNamespace(id=1, display_name="player",
                             guild_permissions=SimpleNamespace(manage_guild=False)),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        channel=SimpleNamespace(send=AsyncMock()),
    )


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
