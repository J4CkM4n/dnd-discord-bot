"""October 17 chat regressions: ownership, escape, NPC continuity and Japanese."""
from copy import deepcopy
import json

import pytest

from core.action_plans import current_plan
from core.director import DirectorProposal, verify_proposal
from core.debug_capture import DebugCapture
from core.intent import Intent, parse_command
from core.memory import build_memory
from core.npc_actions import observation_plan
from core.operators import MainAgent
from core.persistence import StorageError
from core.rules_core import resolve, resolve_pending_check
from core.state import decode_game, encode_game
from core.templates import render_hint
from core.theft import parse_theft
from core.trace_eval import read_traces, replay
from core.turn import Game
from core.world import Enemy
from test_campaign import run, service, restart
from test_operators import Endpoint, roles
from test_system_one import routed, router_answer


THEFT = "船長の武器を盗む"
DISTRACTION = "仲間が船長の注意を引いている間に船長の物を盗む。"
OBSERVE_ASK = "船長をよく観察してからここは何処ですかを聞く"


@pytest.mark.parametrize("die", [1, 20])
def test_theft_roll_transfers_only_the_recorded_item_to_the_actor_after_restart(tmp_path, die):
    s = service(tmp_path)
    s.game.party["依思"]["stats"]["DEX"] = 1
    inventory = deepcopy(s.game.inventory)
    lines, prose = run(s.handle(THEFT, user_id="u1"))
    assert "檢定" in "".join(lines) and "尚未取得物品" in prose
    assert s.game.inventory == inventory
    assert s.game._pending_check["effect"] == {
        "kind": "theft", "target": "老船長", "item": "匕首", "scene": "dock"}
    event_id = s.game._pending_check["event_id"]
    s = restart(tmp_path)
    hp = {n: e["hp_now"] for n, e in s.game.party.items()}
    # Even an unbolded ownership claim or injury cannot reach delivery.
    s.narrator.text = "匕首落在[PC2]的掌心。老船長痛苦呻吟，鮮血直流。"
    delivered = []
    lines, prose = run(s.settle_roll(die, user_id="u1", expected_event_id=event_id,
                                   on_delta=delivered.append))
    assert delivered == [prose] and not s.narrator.calls
    assert "大力蕉" not in prose and "鮮血" not in prose and "沒有造成傷害" in prose
    assert {n: e["hp_now"] for n, e in s.game.party.items()} == hp
    assert s.game.inventory["大力蕉"] == inventory["大力蕉"]
    assert (["匕首", 1] in s.game.inventory["依思"]) == (die == 20)
    assert s.game.scene_npcs()[0]["loot"] == ([] if die == 20 else ["匕首"])
    outcome = next(e for e in reversed(s.game.ledger.entries) if e.kind == "theft")
    assert outcome.actor == outcome.data["recipient"] == "依思"
    assert outcome.data["outcome"] == ("transferred" if die == 20 else "failed")
    s = restart(tmp_path)
    before = deepcopy(encode_game(s.game))
    run(s.settle_roll(20, user_id="u1", expected_event_id=event_id))
    assert encode_game(s.game) == before
    assert sum(e.kind == "theft" for e in s.game.ledger.entries) == 1
    assert all("鮮血" not in e.text for e in s.game.ledger.entries if e.kind == "narration")


@pytest.mark.parametrize("problem", ["no_loot", "missing_item", "many_weapons", "many_targets", "missing_target"])
def test_invalid_theft_never_rolls_creates_loot_or_changes_state(tmp_path, problem):
    s = service(tmp_path)
    text = THEFT
    if problem == "no_loot":
        s.game.world.here.npcs[0].pop("loot")  # Existing saves need honest preflight too.
    elif problem == "missing_item":
        text = "船長の長劍を盗む"
    elif problem == "many_weapons":
        s.game.world.here.npcs[0]["loot"].append("長劍")
    elif problem == "many_targets":
        s.game.world.here.npcs.append({"name": "若船長", "loot": ["匕首"]})
    else:
        text = "王の武器を盗む"
    before = deepcopy(encode_game(s.game))
    rng = s.game._random.getstate()
    lines, prose = run(s.handle(text, user_id="u1"))
    assert lines and not prose
    assert encode_game(s.game) == before and s.game._random.getstate() == rng
    assert not getattr(s.game, "_pending_check", None)
    assert not any(e.kind == "theft" for e in s.game.ledger.entries)


@pytest.mark.parametrize("change", ["item", "target", "scene", "actor"])
def test_stale_successful_theft_does_not_create_or_redirect_possessions(tmp_path, change):
    g = service(tmp_path).game
    g.party["依思"]["stats"]["DEX"] = 1
    assert resolve(g, Intent("steal", actor="依思", target="老船長", item="匕首")).accepted
    if change == "item":
        g.world.here.npcs[0]["loot"] = ["長劍"]
    elif change == "target":
        g.world.here.npcs.clear()
    elif change == "scene":
        g.world.current = "tavern"
    else:
        g.party["依思"]["hp_now"] = 0
    before = deepcopy(g.inventory)
    ok, line = resolve_pending_check(g, 20)
    assert ok and "沒有物品轉移" in line
    assert g.inventory == before and g.ledger.entries[-1].data["outcome"] == "cancelled"
    assert resolve_pending_check(g, 20) == (False, "")


def test_plain_sleight_of_hand_stays_a_check_without_transferring_items(tmp_path):
    s = service(tmp_path)
    s.game.party["依思"]["stats"]["DEX"] = 1
    before = deepcopy(s.game.inventory)
    run(s.handle("依思 技能 手技 老船長", user_id="u1", structured=True))
    assert s.game._pending_check["effect"] is None
    run(s.settle_roll(20, user_id="u1"))
    assert s.game.inventory == before and s.game.scene_npcs()[0]["loot"] == ["匕首"]


def test_failed_theft_commit_preserves_the_pending_roll_and_both_inventories(tmp_path, monkeypatch):
    s = service(tmp_path)
    run(s.handle(THEFT, user_id="u1"))
    before = deepcopy(encode_game(s.game))
    saved = s.repository.load("t")

    def fail(*args, **kwargs):
        raise StorageError("injected write failure")

    delivered = []
    with monkeypatch.context() as m:
        m.setattr(s.repository, "commit", fail)
        lines, _ = run(s.settle_roll(20, user_id="u1", on_delta=delivered.append))
    assert "行動未執行" in "".join(lines) and not delivered
    assert encode_game(s.game) == before and s.repository.load("t") == saved
    run(s.settle_roll(20, user_id="u1"))
    assert ["匕首", 1] in s.game.inventory["依思"] and s.game.scene_npcs()[0]["loot"] == []


def test_theft_attempt_and_settlement_replay_without_models(tmp_path):
    s, _ = routed(tmp_path, Endpoint())
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.handle(THEFT, user_id="u1"))
    run(s.settle_roll(20, user_id="u1"))
    traces = read_traces(tmp_path / "debug")
    assert len(traces) == 2
    for rows in traces.values():
        result = run(replay(rows))
        assert result["output_matches"] and result["snapshot_matches"] and result["all_calls_consumed"]


def test_stolen_enemy_possession_cannot_drop_again_on_death_or_return(tmp_path, monkeypatch):
    s = service(tmp_path)
    g = s.game
    foe = Enemy.make("船長", 1, 13, loot=["匕首"])
    g.world.here.npcs.clear()
    g.encounters["dock"] = [foe]
    g.start_encounter()
    monkeypatch.setattr(Game, "d20", lambda self: 20)
    assert resolve(g, Intent("steal", actor="依思", target="船長", item="匕首")).accepted
    assert foe.loot == [] and foe.loot_fixed
    assert resolve(g, Intent("attack", actor="大力蕉", target="船長")).accepted
    assert foe.dead and not g.combat.active
    assert not g.world.here.ground_items
    assert ["匕首", 1] in g.inventory["依思"]
    g.start_encounter()
    assert not g.combat.active and g.encounters["dock"][0] is foe
    s._save()
    s = restart(tmp_path)
    assert not s.game.combat.active and s.game.encounters["dock"][0].dead
    assert s.game.inventory["依思"].count(["匕首", 1]) == 1
    proposal = DirectorProposal("scene", "dock", {"name": "船長艙", "description": "新的房間。", "npc": "船長"})
    assert verify_proposal(s.game, proposal) == "duplicate_npc"


def test_conditional_japanese_theft_waits_for_a_real_companions_help(tmp_path):
    endpoint = Endpoint(router=[router_answer(compound="yes", linked="yes", ambiguous="yes")])
    s, _ = routed(tmp_path, endpoint)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(DISTRACTION, user_id="u1"))
    assert "/help" in "".join(lines) and "假設" not in "".join(lines)
    assert not endpoint.calls and encode_game(s.game) == before
    # Self-help does not fulfil a request that depends on a companion.
    run(s.handle("依思 幫助 依思", user_id="u1", structured=True))
    lines, _ = run(s.handle(DISTRACTION, user_id="u1"))
    assert "/help" in "".join(lines) and not getattr(s.game, "_pending_check", None)
    run(s.handle("大力蕉 幫助 依思", user_id="u2", structured=True))
    turn = s.game.ledger.turn
    lines, _ = run(s.handle(DISTRACTION, user_id="u1"))
    assert "檢定" in "".join(lines) and s.game._pending_check["helped"]
    assert s.game.ledger.turn == turn + 1
    assert sum(e.kind == "help" for e in s.game.ledger.entries) == 2
    assert roles(endpoint).count("narrator") == 2
    assert set(roles(endpoint)) <= {"narrator", "progress"}
    run(s.settle_roll(20, user_id="u1"))
    assert ["匕首", 1] in s.game.inventory["依思"]
    assert "匕首" not in dict(s.game.inventory["大力蕉"])
    assert not s.game._help_check.get("依思")


@pytest.mark.parametrize("text", ["船長の武器を盗まない", "もし船長の武器を盗むなら？",
    "船長の武器を盗む、そして逃げる", "仲間が船長の注意を引いている間に船長の物を盗む。それから逃げる",
    "不要偷偷扒竊老船長", "偷偷扒竊老船長然後逃走", "偷偷扒竊老船長並逃走",
    "偷取老船長的匕首但不拿走", "偷取老船長的匕首,逃走", "steal dagger from captain, run"])
def test_theft_grammar_never_executes_a_qualified_prefix(text):
    assert parse_theft(text, ["依思", "大力蕉"]) is None
    assert parse_command(text, ["依思", "大力蕉"]) is None


def test_negative_japanese_request_is_not_partially_executed_by_the_router(tmp_path):
    endpoint = Endpoint(router=[router_answer(compound="yes", linked="yes", ambiguous="yes")],
        action=[{"relationship": "hypothetical", "goal": "attack", "steps": []}])
    s, _ = routed(tmp_path, endpoint)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle("もし船長の武器を盗むなら？", user_id="u1"))
    assert "假設" in "".join(lines) and encode_game(s.game) == before
    assert roles(endpoint) == ["router", "action"]


def test_old_unfinished_theft_prose_cannot_append_new_ownership_or_damage(tmp_path):
    s = service(tmp_path)
    run(s.handle(THEFT, user_id="u1"))
    idx = len(s.game.ledger.entries)
    run(s.settle_roll(20, user_id="u1"))
    s._unfinished = dict(s._narration_job(idx, THEFT), text="匕首緩緩落入")
    s._save()
    s = restart(tmp_path)

    async def invented(*args, **kwargs):
        return "[PC2]的手裡。老船長痛苦倒地。"

    s.narrator.continue_text = invented
    before, job = deepcopy(encode_game(s.game)), deepcopy(s._unfinished)
    delivered = []
    assert run(s.continue_narration(delivered.append)) == (False, "")
    assert not delivered and s._unfinished == job and encode_game(s.game) == before


def test_escape_uses_explicit_outcome_and_rejects_combat_start_prose(tmp_path, monkeypatch):
    s = service(tmp_path)
    monkeypatch.setattr(Game, "d20", lambda self: 1)
    run(s.handle("攻擊老船長", user_id="u1", structured=True))
    monkeypatch.setattr(Game, "d20", lambda self: 20)
    s.narrator.text = "殺聲乍起，戰鬥爆發！"
    delivered = []
    lines, prose = run(s.handle("撤退", user_id="u2", structured=True, on_delta=delivered.append))
    assert "脫離戰鬥" in "".join(lines) and not s.game.combat.active
    assert "脫離戰鬥" in prose and "爆發" not in prose and delivered == [prose]
    entry = next(e for e in reversed(s.game.ledger.entries) if e.kind == "combat")
    assert entry.data["outcome"] == "escaped"
    assert render_hint(entry) == render_hint(entry)
    legacy = deepcopy(entry)
    legacy.data.clear()
    assert "脫離戰鬥" in render_hint(legacy) and "爆發" not in render_hint(legacy)
    assert not restart(tmp_path).game.combat.active


@pytest.mark.parametrize("level", [2, 5])
def test_hostility_escape_restart_and_return_preserve_one_captain_and_his_injuries(tmp_path, monkeypatch, level):
    s = service(tmp_path)
    for pc in s.game.party.values():
        pc["level"] = level
    original = deepcopy(s.game.world.here.npcs[0])
    monkeypatch.setattr(Game, "d20", lambda self: 20)
    monkeypatch.setattr(Game, "roll", lambda self, expr: (1, "fixed"))
    run(s.handle("攻擊老船長", user_id="u1", structured=True))
    foe = s.game.enemies["老船長"]
    assert foe.hp < foe.hp_max and foe.npc["knows"] == original["knows"]
    hp, hp_max, bonus = foe.hp, foe.hp_max, foe.attack_bonus
    run(s.handle("撤退", user_id="u2", structured=True))
    s = restart(tmp_path)
    assert s.game.scene_npcs()[0]["desc"] == original["desc"]
    assert s.game.scene_npcs()[0]["disposition"] == "hostile"
    assert "老船長" in s.scene() and f"{hp}/{hp_max}HP" in s.scene()
    before = set(s.game.world.scenes)
    lines, _ = run(s.handle("一人で船長の所に戻る", user_id="u1"))
    assert "仍在這裡" in "".join(lines) and not s.director.calls
    assert set(s.game.world.scenes) == before
    memory = json.loads(build_memory(s.game).current)
    assert memory["enemies"]["老船長"]["hp"] == hp
    assert memory["npcs"][0]["disposition"] == "hostile"
    for name in ("老船長", "船長"):
        proposed = DirectorProposal("scene", "dock", {"name": "船長艙", "description": "新房間。", "npc": name})
        assert verify_proposal(s.game, proposed) == "duplicate_npc"
    run(s.handle("攻擊老船長", user_id="u1", structured=True))
    foe = s.game.enemies["老船長"]
    assert foe.hp == hp - 2 and foe.hp_max == hp_max and foe.attack_bonus == bonus
    assert len(s.game.encounters["dock"]) == 1


def test_npc_possessions_remain_stealable_after_hostility_and_escape(tmp_path, monkeypatch):
    s = service(tmp_path)
    monkeypatch.setattr(Game, "d20", lambda self: 1)
    run(s.handle("攻擊老船長", user_id="u1", structured=True))
    monkeypatch.setattr(Game, "d20", lambda self: 20)
    run(s.handle("撤退", user_id="u2", structured=True))
    s = restart(tmp_path)
    run(s.handle(THEFT, user_id="u1"))
    run(s.settle_roll(20, user_id="u1"))
    assert s.game.encounters["dock"][0].loot == []
    assert ["匕首", 1] in s.game.inventory["依思"]
    assert not s.game.world.here.npcs


def test_old_snapshot_without_npc_metadata_keeps_saved_captain_identity(tmp_path):
    g = service(tmp_path).game
    g.world.here.npcs.clear()
    g.encounters["dock"] = [Enemy.make("船長", 8, 13)]
    blob = deepcopy(encode_game(g))
    old = blob["encounters"]["dock"][0]
    for key in ("npc", "loot_fixed", "spawned"):
        old.pop(key)
    old["hp"] = 7
    restored = decode_game(blob, [])
    assert "船長" in restored.target_names()
    restored.start_encounter()
    assert restored.enemies["船長"].hp == 7
    p = DirectorProposal("scene", "dock", {"name": "船長艙", "description": "房間。", "npc": "船長"})
    assert verify_proposal(restored, p) == "duplicate_npc"


def test_director_return_context_keeps_npc_in_his_actual_scene(tmp_path, monkeypatch):
    s = service(tmp_path)
    monkeypatch.setattr(Game, "d20", lambda self: 1)
    run(s.handle("攻擊老船長", user_id="u1", structured=True))
    s.game.end_combat()
    s.game.world.current = "tavern"
    context = MainAgent.repair_context(s.game, "返回船長身邊", {}, {})
    places = json.loads(context.split("世界設定與既有場所（不能重新創造或任意移動）：\n")[1])["places"]
    assert next(p for p in places if p["id"] == "dock")["npcs"] == ["老船長"]
    assert next(p for p in places if p["id"] == "tavern")["npcs"] == []


def test_japanese_observation_then_question_waits_for_explicit_owner_resume(tmp_path):
    s = service(tmp_path)
    lines, _ = run(s.handle(OBSERVE_ASK, user_id="u1"))
    assert "觀察" in "".join(lines) and "尚未交談" in "".join(lines)
    plan = current_plan(s.game, "依思")
    assert plan["next_step"] == 1 and plan["steps"][-1]["utterance"] == "ここは何処ですか"
    assert not any(e.kind in ("talk", "reveal") for e in s.game.ledger.entries)
    assert not s.director.calls and not getattr(s.game, "_pending_check", None)
    s = restart(tmp_path)
    run(s.confirm("u2"))
    assert not any(e.kind == "talk" for e in s.game.ledger.entries)
    run(s.confirm("u1"))
    assert s.game._action_plans["依思"]["status"] == "completed"
    talk = next(e for e in s.game.ledger.entries if e.kind == "talk")
    assert talk.actor == "依思" and talk.data["npc"] == "老船長"
    assert talk.data["utterance"] == "ここは何処ですか"
    reveals = [e for e in s.game.ledger.entries if e.kind == "reveal"]
    assert len(reveals) == 1
    run(s.confirm("u1"))
    assert len([e for e in s.game.ledger.entries if e.kind == "reveal"]) == 1


def test_observe_ask_plan_stops_if_target_leaves_before_conversation(tmp_path):
    s = service(tmp_path)
    run(s.handle(OBSERVE_ASK, user_id="u1"))
    s.game.world.here.npcs.clear()
    lines, _ = run(s.confirm("u1"))
    assert "找不到交談對象" in "".join(lines)
    assert not any(e.kind == "talk" for e in s.game.ledger.entries)


def test_model_linked_schema_supports_observe_then_talk_without_dropping_steps(tmp_path):
    raw = {"relationship": "linked", "goal": "talk", "steps": [
        {"action": "observe", "target": "老船長"},
        {"action": "talk", "target": "老船長", "utterance": "這裡是哪裡"}]}
    ep = Endpoint(router=[router_answer(category="talk", compound="yes", linked="yes")],
                  action=[raw], evaluator=[{"approved": True}], main=[{"approved": True}])
    s, _ = routed(tmp_path, ep)
    lines, _ = run(s.handle("先觀察老船長，然後問這裡是哪裡", user_id="u1"))
    assert "尚未交談" in "".join(lines)
    assert roles(ep) == ["router", "action", "evaluator", "main", "narrator"]
    assert current_plan(s.game, "依思")["next_step"] == 1


@pytest.mark.parametrize("text", ["もし船長をよく観察してからここは何処ですかを聞く", "船長を観察しないでからここは何処ですかを聞く",
                                 "船長をよく観察してからここは何処ですかを聞く、それから逃げる"])
def test_observation_grammar_preserves_qualified_requests(tmp_path, text):
    assert observation_plan(service(tmp_path).game, text) is None
