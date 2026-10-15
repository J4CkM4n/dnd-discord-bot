"""Linked attempts: dependencies, real resources, action slots, restart and replay."""
from copy import deepcopy
import json

import httpx
import pytest

from core.action_plans import current_plan, parse_plan
from core.debug_capture import DebugCapture
from core.intent import Intent
from core.rules_core import resolve
from core.persistence import StorageError
from core.service import V4Service
from core.state import decode_game, encode_game
from core.trace_eval import read_traces, replay
from core.turn import Game
from core.world import Enemy
from test_campaign import run
from test_operators import Endpoint, roles, audits
from test_system_one import routed, router_answer


TEXT = "BANANA 撿起地上的治療藥水，加入毒藥變成腐蝕化學武器，攻擊哥布林"


def chain(target="哥布林", **updates):
    return {"relationship": "linked", "goal": "attack", "steps": [
        {"action": "take", "item": "治療藥水"},
        {"action": "prepare", "method": "將治療藥水與毒藥混合成腐蝕投擲物", "materials": ["治療藥水", "毒藥"]},
        {"action": "attack", "target": target, "item": "$prepared"}], **updates}


def template(**updates):
    return {"method": "嘗試製作腐蝕投擲物", "skill": "arcana", "difficulty": "hard",
            "template": "improvised_throw", **updates}


def endpoint(raw=None, *, prep=None, evaluator=True, main=True, **updates):
    return Endpoint(router=[router_answer(category="attack", compound="yes", linked="yes",
                                          improvisation="yes", creative_detail="yes")],
        action=[raw or chain()], director=[prep or template()],
        evaluator=[{"approved": evaluator}], main=[{"approved": main}], **updates)


def game_service(tmp_path, ep=None, *, combat=False, **routing):
    s, requests = routed(tmp_path, ep or endpoint(), **routing)
    s.game.party["BANANA"] = s.game.party.pop("依思")
    s.game.inventory.pop("依思")
    s.game.inventory["BANANA"] = [["毒藥", 1]]
    s.game.world.here.ground_items = [("治療藥水", 2)]
    s.game.world.here.hidden_items = [("秘密寶藏", 1)]
    if combat:
        foe = Enemy.make("哥布林", 30, 12)
        s.game.enemies = {foe.name: foe}
        s.game.encounters[s.game.world.current] = [foe]
        s.game.combat.order = [{"name": n, "npc": False} for n in s.game.party] + [
            {"name": foe.name, "npc": True}]
    else:
        s.game.world.here.npcs.append({"name": "哥布林", "disposition": "hostile", "knows": []})
    s._save()
    return s, requests


def restored(s, tmp_path):
    other = V4Service(str(tmp_path), channel_id="t", gateway=s.gateway)
    other.plot, other.rules = {}, {}
    return other


def test_combat_craft_then_explicit_attack_uses_two_round_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(Game, "d20", lambda self: 20)
    ep = endpoint()
    s, _ = game_service(tmp_path, ep, combat=True)
    lines, _ = run(s.handle(TEXT, user_id="u1"))
    plan = current_plan(s.game, "BANANA")
    assert plan["next_step"] == 2 and plan["status"] == "ready"
    assert "尚未攻擊" in "".join(lines)
    assert s.game.ledger.turn == 1 and s.game.combat.acted == ["BANANA"]
    assert s.game.world.here.ground_items == [("治療藥水", 1)]
    assert s.game.inventory["BANANA"] == [[plan["prepared_item"], 1]]
    assert not any(e.kind == "attack" for e in s.game.ledger.entries)
    assert roles(ep) == ["router", "action", "director", "evaluator", "main", "narrator"]
    assert max(r.get("call_count", 0) for r in audits(s)) <= 8
    lines, _ = run(s.confirm("u1"))
    assert "已行動" in "".join(lines) and s.game.enemies["哥布林"].hp == 30
    s = restored(s, tmp_path)
    run(s.handle("大力蕉 防禦", user_id="u2", structured=True))
    assert s.game.enemies["哥布林"].hp == 30  # Other players never auto-run this plan.
    lines, _ = run(s.action_plan("resume", "u1"))
    assert "臨時武器 1d4" in "".join(lines)
    assert 26 <= s.game.enemies["哥布林"].hp <= 29
    assert s.game._action_plans["BANANA"]["status"] == "completed"
    assert not s.game.inventory["BANANA"] and not s.game._prepared_items
    hp = s.game.enemies["哥布林"].hp
    run(s.confirm("u1"))
    assert s.game.enemies["哥布林"].hp == hp
    assert roles(ep).count("director") == 1


@pytest.mark.parametrize("die", [1, 20])
def test_pending_preparation_survives_restart_and_never_attacks_on_roll(tmp_path, die):
    ep = endpoint()
    s, _ = game_service(tmp_path, ep)
    run(s.handle(TEXT, user_id="u1"))
    assert s.game._pending_check["effect"]["kind"] == "linked_prepare"
    assert not s.game.inventory["BANANA"]
    plan_id = s.game._action_plans["BANANA"]["id"]
    s = restored(s, tmp_path)
    run(s.settle_roll(die, user_id="u1"))
    plan = s.game._action_plans["BANANA"]
    assert plan["id"] == plan_id and plan["status"] == ("ready" if die == 20 else "failed")
    assert not s.game.combat.active and not any(e.kind == "attack" for e in s.game.ledger.entries)
    inventory = deepcopy(s.game.inventory)
    run(s.settle_roll(die, user_id="u1"))
    assert s.game.inventory == inventory and roles(ep).count("director") == 1
    if die == 20:
        run(s.confirm("u1"))
        assert s.game.combat.active and any(e.kind == "attack" and e.actor == "BANANA" for e in s.game.ledger.entries)
    else:
        run(s.confirm("u1"))
        assert not s.game.combat.active and not s.game.inventory["BANANA"]


@pytest.mark.parametrize("missing", ["毒藥", "治療藥水", "哥布林"])
def test_missing_prerequisite_never_executes_partial_plan(tmp_path, missing):
    ep = endpoint()
    s, _ = game_service(tmp_path, ep)
    if missing == "毒藥":
        s.game.inventory["BANANA"] = []
    elif missing == "治療藥水":
        s.game.world.here.ground_items = []
        s.game.world.here.hidden_items.append(("治療藥水", 1))
    else:
        s.game.world.here.npcs = [n for n in s.game.world.here.npcs if n["name"] != missing]
    before = deepcopy(encode_game(s.game))
    lines, prose = run(s.handle(TEXT, user_id="u1"))
    assert missing in "".join(lines) and not prose
    assert encode_game(s.game) == before
    assert roles(ep) == ["router", "action"] and "秘密寶藏" not in json.dumps(ep.calls, ensure_ascii=False)


@pytest.mark.parametrize("failure", ["evaluator", "main", "template", "director_outage"])
def test_failed_review_never_picks_up_or_spends_materials(tmp_path, failure):
    ep = endpoint(evaluator=failure != "evaluator", main=failure != "main",
                  prep=template(template="instant_kill") if failure == "template" else None)
    if failure == "director_outage":
        ep.responses["director"].clear()
        ep.responses["director"].append(httpx.Response(503))
    s, _ = game_service(tmp_path, ep)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(TEXT, user_id="u1"))
    assert "❓" in "".join(lines) and encode_game(s.game) == before


@pytest.mark.parametrize("change", ["target", "scene", "item", "downed"])
def test_resume_revalidates_current_world_and_keeps_original_method(tmp_path, change):
    s, _ = game_service(tmp_path)
    run(s.handle(TEXT, user_id="u1"))
    run(s.settle_roll(20, user_id="u1"))
    if change == "target":
        s.game.world.here.npcs = [n for n in s.game.world.here.npcs if n["name"] != "哥布林"]
    elif change == "scene":
        s.game.world.current = "tavern"
    elif change == "item":
        s.game.inventory["BANANA"] = []
    else:
        s.game.party["BANANA"]["hp_now"] = 0
    turn = s.game.ledger.turn
    lines, _ = run(s.confirm("u1"))
    assert not any(e.kind == "attack" for e in s.game.ledger.entries)
    assert s.game.ledger.turn == turn
    assert any(w in "".join(lines) for w in ("找不到", "場景已改變", "不會改用普通攻擊", "倒地"))


def test_cancel_clears_only_owned_pending_plan_and_does_not_refund(tmp_path):
    s, _ = game_service(tmp_path)
    run(s.handle(TEXT, user_id="u1"))
    original = deepcopy(s.game._pending_check)
    run(s.action_plan("cancel", "u2"))
    assert s.game._pending_check == original
    run(s.action_plan("cancel", "u1"))
    assert s.game._pending_check is None and not s.game.inventory["BANANA"]
    assert s.game._action_plans["BANANA"]["status"] == "cancelled"
    run(s.settle_roll(20, user_id="u1"))
    run(s.confirm("u1"))
    assert not s.game.inventory["BANANA"] and not s.game.combat.active
    assert restored(s, tmp_path).game._action_plans["BANANA"]["status"] == "cancelled"


def test_other_player_cannot_create_or_resume_bananas_plan(tmp_path):
    ep = endpoint()
    s, _ = game_service(tmp_path, ep)
    lines, _ = run(s.handle(TEXT, user_id="u2"))
    assert "屬於其他玩家" in lines[0] and not ep.calls
    run(s.handle(TEXT, user_id="u1"))
    run(s.settle_roll(20, user_id="u1"))
    before = deepcopy(encode_game(s.game))
    run(s.confirm("u2"))
    assert encode_game(s.game) == before


@pytest.mark.parametrize("relationship", ["independent", "hypothetical", "ambiguous"])
def test_independent_and_hypothetical_requests_execute_nothing(tmp_path, relationship):
    ep = endpoint(chain(relationship=relationship))
    s, _ = game_service(tmp_path, ep)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(TEXT, user_id="u1"))
    assert "❓" in lines[0] and encode_game(s.game) == before
    assert roles(ep) == ["router", "action"]


@pytest.mark.parametrize("mutation", [
    lambda p: p["steps"].append({"action": "attack", "target": "哥布林", "item": ""}),
    lambda p: p["steps"][-1].update(item=""),
    lambda p: p["steps"][1].update(materials=["治療藥水", "憑空出現的酸液"]),
    lambda p: p["steps"][1].update(amount=999),
])
def test_unbounded_or_method_changing_plan_is_rejected(tmp_path, mutation):
    raw = chain()
    mutation(raw)
    ep = endpoint(raw)
    s, _ = game_service(tmp_path, ep)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(TEXT, user_id="u1"))
    assert "❓" in lines[0] and encode_game(s.game) == before


@pytest.mark.parametrize("goal", ["attack", "use"])
def test_existing_pickup_then_action_needs_no_director_and_one_turn(tmp_path, goal):
    raw = {"relationship": "linked", "goal": goal, "steps": [
        {"action": "take", "item": "治療藥水"},
        {"action": goal, "target": "哥布林" if goal == "attack" else "BANANA", "item": "治療藥水"}]}
    ep = endpoint(raw)
    s, _ = game_service(tmp_path, ep)
    s.game.party["BANANA"]["hp_now"] = 1
    lines, _ = run(s.handle("BANANA 撿起治療藥水" + ("投擲攻擊哥布林" if goal == "attack" else "喝下治療自己"), user_id="u1"))
    assert s.game._action_plans["BANANA"]["status"] == "completed" and s.game.ledger.turn == 1
    assert "director" not in roles(ep) and "evaluator" in roles(ep)
    if goal == "use":
        assert s.game.party["BANANA"]["hp_now"] > 1
    else:
        assert "臨時武器 1d4" in "".join(lines)


def test_prepared_projectile_is_consumed_even_on_miss(tmp_path, monkeypatch):
    s, _ = game_service(tmp_path)
    run(s.handle(TEXT, user_id="u1"))
    run(s.settle_roll(20, user_id="u1"))
    item = s.game._action_plans["BANANA"]["prepared_item"]
    monkeypatch.setattr(Game, "d20", lambda self: 1)
    run(s.confirm("u1"))
    assert not any(n == item for stacks in s.game.inventory.values() for n, q in stacks)
    assert not any(n == item for n, q in s.game.world.here.ground_items)
    assert not s.game._prepared_items


def test_native_failure_still_fits_creative_budget_and_replays_offline(tmp_path):
    ep = endpoint()
    s, _ = game_service(tmp_path, ep, native=True, native_error=503)
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.handle(TEXT, user_id="u1"))
    rows = next(iter(read_traces(tmp_path / "debug").values()))
    assert any(r["kind"] == "action_plan_proposed" for r in rows)
    result = run(replay(rows))
    assert result["output_matches"] and result["snapshot_matches"] and result["all_calls_consumed"]
    assert not result["request_mismatches"]
    assert len([r for r in audits(s) if r.get("kind") == "model_call"]) == 7


@pytest.mark.parametrize("payload", [None, [], {}, {"actor": "BANANA"}])
def test_malformed_engine_plan_is_denied_without_mutation(tmp_path, payload):
    s, _ = game_service(tmp_path)
    before = deepcopy(encode_game(s.game))
    result = resolve(s.game, Intent("creative", actor="BANANA", args={"linked_plan": payload}))
    assert not result.accepted and encode_game(s.game) == before


def test_v8_save_loads_without_linked_plan_fields(tmp_path):
    s, _ = game_service(tmp_path)
    blob = deepcopy(encode_game(s.game))
    blob["v"] = 8
    blob["effects"].pop("_action_plans")
    blob["effects"].pop("_prepared_items")
    g = decode_game(blob, [])
    assert current_plan(g, "BANANA") is None and g.inventory == s.game.inventory


def test_pickup_healing_in_combat_spends_actor_slot(tmp_path):
    raw = {"relationship": "linked", "goal": "use", "steps": [
        {"action": "take", "item": "治療藥水"},
        {"action": "use", "target": "BANANA", "item": "治療藥水"}]}
    s, _ = game_service(tmp_path, endpoint(raw), combat=True)
    s.game.party["BANANA"]["hp_now"] = 1
    run(s.handle("BANANA 撿起治療藥水並喝下", user_id="u1"))
    assert s.game.combat.acted == ["BANANA"] and s.game.ledger.turn == 1
    hp = s.game.party["BANANA"]["hp_now"]
    lines, _ = run(s.handle("BANANA 防禦", user_id="u1", structured=True))
    assert "已行動" in "".join(lines) and s.game.party["BANANA"]["hp_now"] == hp


@pytest.mark.parametrize("stage", ["initial", "settle", "resume"])
def test_storage_failure_never_publishes_partial_plan_or_attack(tmp_path, monkeypatch, stage):
    s, _ = game_service(tmp_path)
    if stage != "initial":
        run(s.handle(TEXT, user_id="u1"))
    if stage == "resume":
        run(s.settle_roll(20, user_id="u1"))
    before = deepcopy(encode_game(s.game))
    saved = s.repository.load("t")

    def failed(*args, **kwargs):
        raise StorageError("injected write failure")

    monkeypatch.setattr(s.repository, "commit", failed)
    if stage == "initial":
        lines, _ = run(s.handle(TEXT, user_id="u1"))
    elif stage == "settle":
        lines, _ = run(s.settle_roll(20, user_id="u1"))
    else:
        lines, _ = run(s.confirm("u1"))
    assert "行動未執行" in "".join(lines)
    assert encode_game(s.game) == before and s.repository.load("t") == saved


def test_plan_status_and_cancel_replay_and_never_make_model_calls(tmp_path):
    ep = endpoint()
    s, _ = game_service(tmp_path, ep)
    run(s.handle(TEXT, user_id="u1"))
    calls = len(ep.calls)
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.action_plan("status", "u1"))
    run(s.action_plan("cancel", "u1"))
    assert len(ep.calls) == calls
    for rows in read_traces(tmp_path / "debug").values():
        result = run(replay(rows))
        assert result["output_matches"] and result["snapshot_matches"] and result["all_calls_consumed"]


def test_linked_router_decision_without_compound_still_uses_planner(tmp_path):
    ep = endpoint()
    ep.responses["router"].clear()
    ep.responses["router"].append(router_answer(category="attack", linked="yes"))
    s, _ = game_service(tmp_path, ep)
    run(s.handle(TEXT, user_id="u1"))
    assert current_plan(s.game, "BANANA")["status"] == "awaiting_check"
    assert roles(ep).count("director") == 1


def test_plan_command_routes_status_and_continuation_to_owned_service():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from adapters.discord_bot import DiscordBot
    from test_campaign_integration import interaction
    bot = DiscordBot()
    s = SimpleNamespace(game=SimpleNamespace(combat=SimpleNamespace(active=False)),
                        action_plan=AsyncMock(return_value=(["計畫狀態"], "")))
    bot._v4_games["123"] = s
    for action in ("status", "resume", "cancel"):
        run(bot.tree.get_command("plan").callback(interaction(), action))
        assert s.action_plan.await_args.args == (action, "1")


def test_reviewer_objection_is_visible_and_audited_without_spending(tmp_path):
    ep = endpoint()
    ep.responses["evaluator"].clear()
    ep.responses["evaluator"].append({"approved": False, "reason": "要求的範圍傷害超出此投擲物模板"})
    s, _ = game_service(tmp_path, ep)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(TEXT, user_id="u1"))
    assert "範圍傷害超出" in "".join(lines) and encode_game(s.game) == before
    assert any(r.get("role") == "evaluator" and r.get("status") == "rejected" for r in audits(s))


@pytest.mark.parametrize("review", [{"approved": "true"}, {"approved": True, "damage": 999},
                                     {"approved": True, "reason": ["invalid"]}])
def test_malformed_review_cannot_approve_plan(tmp_path, review):
    ep = endpoint()
    ep.responses["evaluator"].clear()
    ep.responses["evaluator"].append(review)
    s, _ = game_service(tmp_path, ep)
    before = deepcopy(encode_game(s.game))
    run(s.handle(TEXT, user_id="u1"))
    assert encode_game(s.game) == before
