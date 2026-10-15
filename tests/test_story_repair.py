"""Main-controlled spatial repairs require JEV and commit before publication."""

from copy import deepcopy

import httpx
import pytest

from core.director import MAX_DIRECTOR_SCENES
from core.intent import Intent, parse_command
from core.memory import build_memory
from core.operators import ActionDecision
from core.director import DirectorDecision, DirectorProposal
from core.state import encode_game
from core.world import Enemy, Scene
from test_campaign import decisions, restart, run
from test_operators import (Endpoint, EXECUTE_ACTION, EXECUTE_PARSED, PROGRESS,
                            audits, roles, supervised)

BOARD = "登上船長的船"
SHIP = {"name": "船長的船", "description": "一艘停靠碼頭的漁船，舷梯連著潮濕的甲板。", "npc": ""}
APPROVED = {"relevant": True, "consistent": True, "reason": "碼頭已有漁船，登船符合原意"}
APPROVE_PARSED = {**EXECUTE_PARSED, "director": True}
APPROVE_ACTION = {**EXECUTE_ACTION, "director": True}
APPROVE_REPAIR = {"decision": "execute", "intent": "repair", "director": True}


@pytest.mark.parametrize("text,destination", [
    (BOARD, "船長的船"), ("我想登上船長的船", "船長的船"),
    ("依思 走進破舊酒館", "破舊酒館"), ("進入船艙", "船艙"),
    ("踏入小巷", "小巷"), ("board Captain Jack's ship", "Captain Jack's ship"),
    ("enter the cabin", "the cabin"),
])
def test_entry_actions_have_movement_candidates(text, destination):
    intent = parse_command(text, ["依思"])
    assert intent.action == "move" and intent.destination == destination


@pytest.mark.parametrize("text", ["不要登上船長的船", "如果登上船長的船？", "登上船長的船然後出航"])
def test_qualified_boarding_is_not_reduced_to_unconditional_movement(text):
    assert parse_command(text) is None


def test_boarding_repairs_omitted_exit_with_jev_even_when_plan_omits_director(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]}, APPROVE_ACTION],
        action=[{"action": "move", "destination": "船長的船", "confidence": .95}],
        director=[SHIP], evaluator=[APPROVED])
    s = supervised(tmp_path, endpoint)  # optional item evaluation is off
    original_party, original_inventory = deepcopy(s.game.party), deepcopy(s.game.inventory)
    delivered = []

    async def on_resolved(lines):
        saved = s.repository.load(s.channel_id)
        assert saved["snapshot"]["world"]["current"] == "dir1"
        assert roles(endpoint)[-1] == "main" and "narrator" not in roles(endpoint)
        delivered.append(lines)

    lines, prose = run(s.handle(BOARD, user_id="u1", on_resolved=on_resolved))
    assert delivered and "船長的船" in lines[0] and prose
    assert roles(endpoint) == ["main", "action", "director", "evaluator", "main", "narrator"]
    assert not endpoint.unexpected and max(a["call_count"] for a in audits(s)) == 6
    assert s.game.world.current == "dir1"
    assert s.game.world.here.exits == {"dock": "霧錨鎮碼頭"}
    assert s.game.world.scenes["dock"].exits["dir1"] == "船長的船"
    assert s.game.world.scenes["dock"].npcs[0]["name"] == "老船長"
    assert s.game.world.here.npcs == []  # boarding does not move the captain
    assert s.game.party == original_party and s.game.inventory == original_inventory
    assert decisions(s)[-1]["accepted"] and decisions(s)[-1]["evaluation"] == "approved"
    assert restart(tmp_path).game.world.current == "dir1"
    assert "船長的船" in build_memory(s.game).render()
    assert s._operator_state["pending_triggers"] == ["scene"]
    judge_prompt = next(p for role, p in endpoint.calls if role == "evaluator")
    assert BOARD in judge_prompt and "霧錨鎮碼頭" in judge_prompt
    assert "所有權" in judge_prompt and "可達" in judge_prompt


@pytest.mark.parametrize("action", [
    {"action": "unsupported_board"},
    {"action": "creative", "utterance": "跨過舷梯踏上甲板"},
])
def test_main_can_delegate_one_repair_when_action_operator_misses_movement(tmp_path, action):
    endpoint = Endpoint(main=[{"consult": ["action", "director"]}, APPROVE_REPAIR],
        action=[action], director=[{"destination": "船長的船", "scene": SHIP}], evaluator=[APPROVED])
    s = supervised(tmp_path, endpoint)
    lines, _ = run(s.handle("跨過舷梯踏上甲板", user_id="u1"))
    assert "船長的船" in lines[0] and s.game.world.current == "dir1"
    assert roles(endpoint) == ["main", "action", "director", "evaluator", "main", "narrator"]
    assert decisions(s)[-1]["accepted"]
    assert s.game.ledger.turn == 1 and not endpoint.unexpected


@pytest.mark.parametrize("evaluation,expected", [
    ({"relevant": False, "consistent": True, "reason": "不可透露的裁決細節"}, "未批准"),
    ({"relevant": True, "consistent": False, "reason": "不可透露的裁決細節"}, "未批准"),
    ({"relevant": "true", "consistent": True, "reason": "bad type"}, "格式"),
    (httpx.Response(503), "評估服務"),
])
def test_rejected_or_unavailable_jev_blocks_even_a_main_execute(tmp_path, evaluation, expected):
    endpoint = Endpoint(main=[{"consult": []}, APPROVE_PARSED],
                        director=[SHIP], evaluator=[evaluation])
    s = supervised(tmp_path, endpoint)
    before, plot = encode_game(s.game), deepcopy(s.plot_state)
    lines, prose = run(s.handle(BOARD, user_id="u1"))
    assert expected in lines[0] and "已嘗試修復故事銜接" in lines[0] and not prose
    assert "不可透露的裁決細節" not in lines[0]
    assert encode_game(s.game) == before and s.plot_state == plot
    assert s.repository.load("t")["snapshot"]["world"]["current"] == "dock"
    assert not decisions(s)[-1]["accepted"] and decisions(s)[-1]["evaluation"] == "rejected"
    assert roles(endpoint) == ["main", "director", "evaluator", "main"]


@pytest.mark.parametrize("final", [
    {"decision": "clarify", "intent": "parsed", "director": False},
    EXECUTE_PARSED,
    httpx.Response(503),
])
def test_main_must_authorize_the_repair_after_positive_jev(tmp_path, final):
    endpoint = Endpoint(main=[{"consult": []}, final], director=[SHIP], evaluator=[APPROVED])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, prose = run(s.handle(BOARD, user_id="u1"))
    assert "尚未執行" in lines[0] and not prose
    assert encode_game(s.game) == before and not decisions(s)[-1]["accepted"]
    assert "narrator" not in roles(endpoint)


@pytest.mark.parametrize("proposal", [
    {**SHIP, "npc": "老船長"},  # cannot duplicate or teleport an existing NPC
    {**SHIP, "hp": 99},
    {**SHIP, "name": ""},
])
def test_engine_shape_and_uniqueness_gate_precedes_jev(tmp_path, proposal):
    endpoint = Endpoint(main=[{"consult": []}, APPROVE_PARSED], director=[proposal])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, _ = run(s.handle(BOARD, user_id="u1"))
    assert "尚未執行" in lines[0] and encode_game(s.game) == before
    assert "evaluator" not in roles(endpoint) and not endpoint.unexpected


@pytest.mark.parametrize("limit", [4, 5])
def test_small_budget_defers_whole_repair_without_unjudged_world_changes(tmp_path, limit):
    endpoint = Endpoint(main=[{"consult": ["action"]}, APPROVE_ACTION],
        action=[{"action": "move", "destination": "船長的船"}])
    s = supervised(tmp_path, endpoint, max_calls=limit)
    before = encode_game(s.game)
    lines, prose = run(s.handle(BOARD, user_id="u1"))
    assert "額度不足" in lines[0] and not prose and encode_game(s.game) == before
    assert roles(endpoint) == ["main", "action", "main"]


def test_unowned_actor_and_pending_check_cannot_trigger_repair(tmp_path):
    endpoint = Endpoint()
    s = supervised(tmp_path, endpoint)
    lines, _ = run(s.handle("大力蕉 " + BOARD, user_id="u1"))
    assert "🚫" in lines[0] and not endpoint.calls
    s.game._pending_check = {"actor": "依思", "event_id": 1, "scene": "dock", "user_id": "u1"}
    s._save()
    endpoint.responses["main"].extend([{"consult": []}, APPROVE_PARSED])
    lines, _ = run(s.handle(BOARD, user_id="u1"))
    assert "待定檢定" in lines[0] and "director" not in roles(endpoint)


def test_no_new_exit_can_be_invented_to_bypass_combat(tmp_path):
    endpoint = Endpoint(main=[{"consult": []}, APPROVE_PARSED])
    s = supervised(tmp_path, endpoint)
    s.game.enemies = {"守衛": Enemy.make("守衛", 20, 15)}
    s.game.combat.order = [{"name": "依思", "npc": False}, {"name": "守衛", "npc": True}]
    s._save()
    before = encode_game(s.game)
    lines, _ = run(s.handle(BOARD, user_id="u1"))
    assert "/flee" in lines[0] and encode_game(s.game) == before
    assert roles(endpoint) == ["main", "main"]


def test_scene_limit_explains_the_world_constraint(tmp_path):
    endpoint = Endpoint(main=[{"consult": []}, APPROVE_PARSED])
    s = supervised(tmp_path, endpoint)
    s.game.director_scene_count = MAX_DIRECTOR_SCENES
    s._save()
    lines, _ = run(s.handle(BOARD, user_id="u1"))
    assert "場景上限" in lines[0] and roles(endpoint) == ["main", "main"]


def test_existing_ship_exit_moves_without_a_new_scene_or_evaluation(tmp_path):
    endpoint = Endpoint(main=[{"consult": []}, EXECUTE_PARSED, {"options": [0]}], progress=[PROGRESS])
    s = supervised(tmp_path, endpoint)
    s.game.world.scenes["ship"] = Scene("ship", "船長的船", "停泊中的甲板。", exits={"dock": "碼頭"})
    s.game.world.here.exits["ship"] = "船長的船"
    s._save()
    run(s.handle(BOARD, user_id="u1"))
    assert s.game.world.current == "ship" and len(s.game.world.scenes) == 3
    assert "director" not in roles(endpoint) and "evaluator" not in roles(endpoint)
    assert not endpoint.unexpected


def test_low_confidence_still_needs_owner_confirmation_before_jev(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION,
                              {"consult": []}, APPROVE_PARSED],
        action=[{"action": "move", "destination": "船長的船", "confidence": .2}],
        director=[SHIP], evaluator=[APPROVED])
    s = supervised(tmp_path, endpoint)
    lines, _ = run(s.handle(BOARD, user_id="u1"))
    assert "/confirm" in lines[0] and roles(endpoint) == ["main", "action", "main"]
    assert s.game.world.current == "dock" and s.pending_user_id == "u1"
    run(s.confirm("u1"))
    assert s.game.world.current == "dir1" and not s.pending
    assert roles(endpoint)[-5:] == ["main", "director", "evaluator", "main", "narrator"]


def test_generated_repair_rolls_back_on_save_failure_without_publication(tmp_path):
    endpoint = Endpoint(main=[{"consult": []}, APPROVE_PARSED], director=[SHIP], evaluator=[APPROVED])
    s = supervised(tmp_path, endpoint)
    before = s.repository.load("t")
    with s.repository.connect() as conn:
        conn.execute("CREATE TRIGGER fail_repair BEFORE UPDATE OF snapshot ON channels "
                     "BEGIN SELECT RAISE(ABORT, 'write failure'); END")
    lines, prose = run(s.handle(BOARD, user_id="u1"))
    assert "未能儲存" in lines[0] and not prose
    assert s.repository.load("t") == before and s.game.world.current == "dock"
    assert not decisions(s) and "narrator" not in roles(endpoint)


def test_final_engine_gate_requires_evaluation_even_for_an_injected_proposal(tmp_path):
    s = supervised(tmp_path, Endpoint())
    intent = Intent("move", actor="依思", destination="船長的船")
    decision = DirectorDecision("scene", "dock", proposal=SHIP)
    s._supervised = ActionDecision(intent, s._revision, DirectorProposal("scene", "dock", SHIP), decision)
    s._apply_supervised_proposal(intent)
    assert len(s.game.world.scenes) == 2
    assert not decision.accepted and "evaluation_required" in decision.reasons
