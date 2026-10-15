"""Single, list and all-ground collection; corpse inspection never rerolls loot."""
from copy import deepcopy

import httpx
import pytest

from core.cli import build_demo_game
from core.debug_capture import DebugCapture
from core.director import classify
from core.intent import Intent, parse_command
from core.persistence import StorageError
from core.pickups import parse_pickup
from core.rules_core import resolve
from core.state import encode_game
from core.trace_eval import read_traces, replay
from core.world import Enemy
from test_campaign import restart, run, service
from test_operators import Endpoint, roles, supervised
from test_system_one import routed, router_answer

GROUND = [("治療藥水", 2), ("匕首", 1), ("火把", 3)]
REQUESTS = ["撿起地上的治療藥水", "撿起地上的治療藥水，匕首和火把", "撿起地上所有散落物品"]


def game():
    g = build_demo_game(seed=2)
    g.party["依思"]["owner_id"] = "u1"
    g.party["大力蕉"]["owner_id"] = "u2"
    g.inventory["依思"] = []
    g.world.here.ground_items = list(GROUND)
    g.world.here.hidden_items = [("秘密木雕", 1)]
    return g


@pytest.mark.parametrize("text", REQUESTS + ["我執起地下嘅治療藥水、匕首同埋火把", "take all items"])
def test_collection_moves_only_real_ground_stacks(text):
    g = game()
    result = resolve(g, parse_command("依思 " + text, list(g.party)))
    assert result.accepted
    expected = GROUND[:1] if text == REQUESTS[0] else GROUND
    assert g.inventory["依思"] == [list(stack) for stack in expected]
    assert g.world.here.ground_items == GROUND[len(expected):]
    assert g.world.here.hidden_items == [("秘密木雕", 1)]
    assert not any(e.kind == "materialize" for e in g.ledger.entries)
    assert [e.data["qty"] for e in g.ledger.entries if e.kind == "item"] == [q for _, q in expected]


@pytest.mark.parametrize("text", [
    "如果撿起地上的治療藥水", "不要撿起地上的治療藥水", "撿起地上的治療藥水？",
    "撿起地上的治療藥水然後攻擊哥布林", "撿起地上的治療藥水，喝下去",
    "撿起所有物品但不要火把", "撿起所有物品除了匕首", "take all items except dagger",
    "撿起治療藥水或火把", "撿起地上的治療藥水，加入毒藥變成腐蝕化學武器，攻擊哥布林",
])
def test_complex_or_restricted_requests_never_execute_collection_prefix(text):
    assert parse_pickup(text) is None


@pytest.mark.parametrize("item", ["治療藥水、匕首、寶石", "秘密木雕", "匕", "所有物品"])
def test_failed_preflight_moves_nothing_and_spends_no_turn(item):
    g = game()
    if item == "匕":
        g.world.here.ground_items.append(("生鏽匕首", 1))
    elif item == "所有物品":
        g.world.here.ground_items.clear()
    before = deepcopy(encode_game(g))
    result = resolve(g, Intent("take", actor="依思", item=item))
    assert not result.accepted and encode_game(g) == before
    assert [e.kind for e in g.ledger.entries] == ["deny"]


def test_duplicate_ground_stacks_are_merged_and_repeating_all_cannot_duplicate():
    g = game()
    g.world.here.ground_items.append(("治療藥水", 1))
    assert resolve(g, Intent("take", actor="依思", item="治療藥水、治療藥水")).accepted
    assert g.inventory["依思"] == [["治療藥水", 3]]
    assert resolve(g, Intent("take", actor="依思", item="所有散落物品")).accepted
    before = deepcopy(encode_game(g))
    assert not resolve(g, Intent("take", actor="依思", item="所有物品")).accepted
    assert encode_game(g) == before
    assert all(n != "所有散落物品" for n, _ in g.inventory["依思"])


@pytest.mark.parametrize("name", ["所有散落物品", "所有物品", "loot", "治療藥水、匕首", "火把", "繩索"])
def test_director_cannot_materialize_collection_labels_or_game_gear(name):
    assert classify(name) == "blocked"


def test_batch_collection_costs_one_combat_action():
    g = game()
    foe = Enemy.make("哥布林", 20, 13)
    g.encounters[g.world.current] = [foe]
    g.start_encounter()
    result = resolve(g, Intent("take", actor="依思", item="所有物品"))
    assert result.accepted and g.combat.acted == ["依思"]
    assert len(g.inventory["依思"]) == 3
    before = deepcopy(encode_game(g))
    assert not resolve(g, Intent("take", actor="依思", item="木雕")).accepted
    assert encode_game(g) == before


@pytest.mark.parametrize("text", REQUESTS)
@pytest.mark.parametrize("broken_router", [False, True])
def test_bad_compound_decision_cannot_reject_whole_collection(tmp_path, text, broken_router):
    endpoint = Endpoint(router=[httpx.Response(503)] if broken_router else [
        router_answer(category="take", compound="yes", linked="yes", improvisation="yes")])
    s, _ = routed(tmp_path, endpoint)
    s.game = game()
    s.game.party["依思"]["owner_id"] = "u1"
    s._save()
    lines, _ = run(s.handle(text, user_id="u1"))
    expected = GROUND[:1] if text == REQUESTS[0] else GROUND
    assert s.game.inventory["依思"] == [list(stack) for stack in expected]
    assert "拾起 治療藥水×2" in "".join(lines)
    assert roles(endpoint) == ["router", "narrator"]
    assert encode_game(restart(tmp_path).game) == encode_game(s.game)


def test_legacy_supervisor_accepts_collection_without_linked_plan(tmp_path):
    endpoint = Endpoint()
    s = supervised(tmp_path, endpoint)
    s.game.world.here.ground_items = list(GROUND)
    before = dict(s.game.inventory["依思"])
    run(s.handle(REQUESTS[1], user_id="u1"))
    items = dict(s.game.inventory["依思"])
    assert all(items[n] == before.get(n, 0) + q for n, q in GROUND)
    assert roles(endpoint) == ["narrator"]


@pytest.mark.parametrize("failure", ["owner", "downed", "pending"])
def test_collection_keeps_authorization_and_pending_guards(tmp_path, failure):
    s, _ = routed(tmp_path, Endpoint(router=[router_answer(compound="yes")]))
    s.game.world.here.ground_items = list(GROUND)
    if failure == "downed":
        s.game.party["依思"]["hp_now"] = 0
    elif failure == "pending":
        s.game._pending_check = {"actor": "依思"}
    before = deepcopy(encode_game(s.game))
    text = "大力蕉 " + REQUESTS[1] if failure == "owner" else REQUESTS[1]
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "🚫" in "".join(lines)
    assert encode_game(s.game) == before


def test_collection_write_failure_never_publishes_partial_inventory(tmp_path, monkeypatch):
    s = service(tmp_path)
    s.game.world.here.ground_items = list(GROUND)
    s._save()
    before = deepcopy(encode_game(s.game))
    def fail(*args, **kwargs):
        raise StorageError("simulated write failure")
    monkeypatch.setattr(s.repository, "commit", fail)
    run(s.handle(REQUESTS[1], user_id="u1", structured=True))
    assert encode_game(s.game) == before
    assert encode_game(restart(tmp_path).game) == before


def test_collection_trace_replays_without_models_or_inventory_drift(tmp_path):
    s, _ = routed(tmp_path, Endpoint(router=[router_answer(category="take", compound="yes")]))
    s.game.world.here.ground_items = list(GROUND)
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.handle(REQUESTS[1], user_id="u1"))
    rows = next(iter(read_traces(tmp_path / "debug").values()))
    report = run(replay(rows))
    assert report["snapshot_matches"] and report["output_matches"] and report["all_calls_consumed"]
    assert not report["request_mismatches"]


@pytest.mark.parametrize("text", ["搜索哥布林屍體", "切開地上面哥布林屍體搜索有用的物品",
                                 "用刀切開地上面哥布林屍體搜索有用的物品"])
def test_searching_corpses_reports_existing_loot_without_new_rewards(text):
    g = game()
    corpse = Enemy.make("哥布林①", 7, 15)
    corpse.dead, corpse.hp = True, 0
    g.encounters[g.world.current] = [corpse]
    g.give_item("依思", "匕首", 1)
    before = deepcopy(encode_game(g))
    for _ in range(2):
        result = resolve(g, parse_command("依思 " + text, list(g.party)))
        assert result.accepted and "不會再次掉落" in "".join(result.lines)
        assert "治療藥水×2" in "".join(result.lines)
        assert "秘密木雕" not in "".join(result.lines)
    after = encode_game(g)
    before["turn"] = after["turn"]
    assert before == after
    assert all(e.kind == "search" for e in g.ledger.entries)


@pytest.mark.parametrize("problem", ["alive", "missing", "no_blade"])
def test_corpse_search_rejects_missing_body_or_blade(problem):
    g = game()
    corpse = Enemy.make("哥布林①", 7, 15)
    corpse.dead, corpse.hp = problem != "alive", 7 if problem == "alive" else 0
    g.encounters[g.world.current] = [] if problem == "missing" else [corpse]
    before = deepcopy(encode_game(g))
    intent = parse_command("依思 用刀切開哥布林屍體搜索有用的物品", list(g.party))
    result = resolve(g, intent)
    assert not result.accepted and encode_game(g) == before
    assert ("刀具" if problem == "no_blade" else "屍體") in "".join(result.lines)


def test_corpse_search_with_false_compound_route_never_demands_loot_names(tmp_path):
    s, _ = routed(tmp_path, Endpoint(router=[router_answer(category="search", compound="yes", linked="yes")]))
    corpse = Enemy.make("哥布林①", 7, 15)
    corpse.hp, corpse.dead = 0, True
    s.game.encounters[s.game.world.current] = [corpse]
    s.game.world.here.ground_items = list(GROUND)
    lines, _ = run(s.handle("搜索哥布林屍體", user_id="u1"))
    assert "不會再次掉落" in "".join(lines) and "治療藥水×2" in "".join(lines)
    assert s.game.world.here.ground_items == GROUND
