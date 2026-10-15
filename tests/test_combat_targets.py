"""Defeated enemies remain encounter history, never repeatable kill rewards."""
import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from core.cli import build_demo_game
from core.decisions import routing_questions
from core.intent import Intent
from core.memory import build_memory
from core.rules_core import _attack, _throw, resolve, resolve_target
from core.state import decode_game, encode_game
from test_campaign import restart, run, service
from test_operators import Endpoint, supervised
from test_system_one import routed


def encounter(g):
    g.world.current = "tavern"
    g.start_encounter()
    g.enemies["哥布林①"].hp = 1
    g.enemies["哥布林①"].loot = ["治療藥水"]
    g.enemies["哥布林②"].hp = 5
    return g


def defeated_game():
    g = encounter(build_demo_game(seed=2))
    g.enemies["哥布林①"].hp = 0
    g.enemies["哥布林①"].dead = True
    return g


@pytest.mark.parametrize("dead,hp", [(True, 0), (False, 0), (True, 3)])
def test_generic_name_skips_defeated_enemy(dead, hp):
    g = defeated_game()
    g.enemies["哥布林①"].dead, g.enemies["哥布林①"].hp = dead, hp
    assert resolve_target(g, "哥布林") == (g.enemies["哥布林②"], None)
    assert "哥布林①" not in g.target_names()
    questions, targets, _ = routing_questions(g)
    assert "哥布林①" not in targets
    assert "哥布林②" in questions["target"]["criteria"].values()
    current = json.loads(build_memory(g).current)
    assert current["enemies"]["哥布林①"]["dead"] is True
    assert resolve(g, Intent("observe", actor="依思")).accepted
    assert g._aid == {"哥布林②": True}


@pytest.mark.parametrize("target", ["哥布林①", "第一隻哥布林", "哥布林第1隻"])
@pytest.mark.parametrize("method", ["attack", "spell", "throw", "creative", "observe"])
def test_explicit_corpse_is_denied_without_costs(target, method):
    g = defeated_game()
    g._stealth = {"依思": True}
    g._aid = {"哥布林①": True}
    g._dodge = {"依思": True}
    g.add_cond("依思", "inspired", None)
    intent = Intent("attack", actor="依思", target=target)
    if method == "spell":
        intent.args = {"move": "地獄斥喝"}
    elif method == "throw":
        intent.args = {"thrown_item": "治療藥水"}
    elif method == "creative":
        intent.action, intent.item, intent.utterance = "creative", "治療藥水", "投擲治療藥水"
    elif method == "observe":
        intent.action = "observe"
    before = deepcopy(encode_game(g))
    events = len(g.ledger.entries)
    result = resolve(g, intent)
    assert not result.accepted and "哥布林① 已倒下" in "".join(result.lines)
    assert encode_game(g) == before
    assert [e.kind for e in g.ledger.entries[events:]] == ["deny"]


@pytest.mark.parametrize("target", ["哥布林③", "第三隻哥布林", "第3隻哥布林"])
def test_absent_ordinal_does_not_redirect_to_living_sibling(target):
    g = defeated_game()
    assert resolve_target(g, target) == (None, None)
    before = deepcopy(encode_game(g))
    result = resolve(g, Intent("attack", actor="依思", target=target))
    assert not result.accepted and "找不到目標" in "".join(result.lines)
    assert encode_game(g) == before


def test_exact_name_wins_over_partial_name():
    g = defeated_game()
    g.enemies["哥布林"] = deepcopy(g.enemies["哥布林②"])
    g.enemies["哥布林"].name = "哥布林"
    assert resolve_target(g, "哥布林") == (g.enemies["哥布林"], None)
    assert resolve_target(g, "向哥布林②投擲") == (g.enemies["哥布林②"], None)


@pytest.mark.parametrize("throw", [False, True])
def test_low_level_attack_guard_precedes_resource_spending(throw):
    g = defeated_game()
    before = deepcopy(encode_game(g))
    if throw:
        result = _throw(g, "依思", Intent("creative", item="治療藥水", target="哥布林①"), "投擲治療藥水")
    else:
        result = _attack(g, "依思", "哥布林①", "地獄斥喝")
    assert not result.accepted and encode_game(g) == before


def test_kill_save_reload_then_generic_attack_never_repeats_rewards():
    g = encounter(build_demo_game(seed=2))
    g.d20 = lambda: 19
    g.roll = lambda _: (3, "1d4 [3]")
    assert resolve(g, Intent("attack", actor="依思", target="哥布林①")).accepted
    assert g.enemies["哥布林①"].dead
    assert g.world.here.ground_items == [("治療藥水", 1)]
    # Actual JSON round-trip, including shared encounter/enemy identity.
    g = decode_game(json.loads(json.dumps(encode_game(g))),
                    [asdict(e) for e in g.ledger.entries])
    assert g.enemies["哥布林①"] is g.encounters["tavern"][0]
    assert g.enemies["哥布林①"].dead and g.enemies["哥布林①"].hp == 0
    g.d20 = lambda: 19
    g.roll = lambda _: (3, "1d4 [3]")
    rewards = {n: (e.get("xp"), e["level"]) for n, e in g.party.items()}
    events = len(g.ledger.entries)
    result = resolve(g, Intent("attack", actor="大力蕉", target="哥布林"))
    assert result.accepted and "攻擊 哥布林②" in "".join(result.lines)
    assert g.enemies["哥布林②"].hp == 2
    assert g.world.here.ground_items == [("治療藥水", 1)]
    assert {n: (e.get("xp"), e["level"]) for n, e in g.party.items()} == rewards
    assert not any(e.kind in ("loot", "xp", "death") for e in g.ledger.entries[events:])


def test_durable_service_rejects_corpse_after_restart(tmp_path):
    s = service(tmp_path)
    s.game = defeated_game()
    s.game.party["依思"]["owner_id"] = "u1"
    s._save()
    restored = restart(tmp_path)
    before = deepcopy(encode_game(restored.game))
    lines, _ = run(restored.handle("依思 攻擊 哥布林①", user_id="u1", structured=True))
    assert "已倒下" in "".join(lines)
    assert encode_game(restored.game) == before
    assert encode_game(restart(tmp_path).game) == before


def test_native_router_only_offers_living_enemy(tmp_path):
    endpoint = Endpoint(action=[{"action": "attack", "target": "哥布林②"}])
    s, requests = routed(tmp_path, endpoint, native=True,
                         selection={"category": "attack", "target": "t0"})
    encounter(s.game)
    s.game.enemies["哥布林①"].hp, s.game.enemies["哥布林①"].dead = 0, True
    lines, _ = run(s.handle("攻擊哥布林", user_id="u1"))
    assert "攻擊 哥布林②" in "".join(lines)
    choices = requests[0]["questions"]["target"]["criteria"].values()
    assert "哥布林①" not in choices and "哥布林②" in choices


def test_target_filter_keeps_downed_party_for_healing():
    g = defeated_game()
    g.party["大力蕉"]["hp_now"] = 0
    assert "大力蕉" in g.target_names()
    assert resolve_target(g, "大力蕉") == (None, "大力蕉")


def test_legacy_action_specialist_only_offers_living_enemy(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]},
        {"decision": "execute", "intent": "action", "director": False}],
        action=[{"action": "attack", "target": "哥布林②"}])
    s = supervised(tmp_path, endpoint)
    encounter(s.game)
    s.game.enemies["哥布林①"].hp, s.game.enemies["哥布林①"].dead = 0, True
    lines, _ = run(s.handle("攻擊哥布林", user_id="u1"))
    assert "攻擊 哥布林②" in "".join(lines)
    prompt = next(p for role, p in endpoint.calls if role == "action")
    target_line = next(line for line in prompt.splitlines() if line.startswith("已知目標"))
    assert "哥布林①" not in target_line and "哥布林②" in target_line
