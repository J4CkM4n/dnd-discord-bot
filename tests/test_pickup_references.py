"""Ground-item references must transfer an existing identity, never create a label."""
from copy import deepcopy

import pytest

from core.director import classify, try_materialize
from core.intent import Intent, parse_command
from core.pickups import parse_pickup
from core.rules_core import resolve
from core.state import encode_game
from test_campaign import restart, run
from test_operators import Endpoint, roles, supervised
from test_pickups import game
from test_system_one import routed, router_answer


REQUESTS = [
    "執起武器", "拾起地上武器", "我執起地下嘅武器", "攞起嗰把武器",
    "pick up the weapon on the ground", "pickup the weapon on the ground",
    "take the weapon", "地面の武器を手に取る", "地上の武器を拾う",
]


@pytest.mark.parametrize("text", REQUESTS)
def test_unique_ground_weapon_reference_moves_canonical_item_and_cannot_repeat(text):
    g = game()
    g.give_item("依思", "長劍", 1)
    g.world.here.hidden_items = [("秘密匕首", 1)]
    intent = parse_pickup("依思 " + text, g.party)
    assert intent is not None and intent.action == "take"
    result = resolve(g, intent)
    assert result.accepted and "拾起 匕首×1" in "".join(result.lines)
    assert g.inventory["依思"] == [["長劍", 1], ["匕首", 1]]
    assert g.world.here.ground_items == [("治療藥水", 2), ("火把", 3)]
    assert g.world.here.hidden_items == [("秘密匕首", 1)]
    assert not any(e.kind == "materialize" for e in g.ledger.entries)
    event = next(e for e in g.ledger.entries if e.kind == "item")
    assert event.data["item"] == "匕首" and event.data["source"] == "ground"
    before = deepcopy(encode_game(g))
    assert not resolve(g, intent).accepted
    assert encode_game(g) == before


@pytest.mark.parametrize("name", ["生鏽匕首", "短劍", "魚叉", "rusty dagger", "longsword", "ダガー", "短剣"])
def test_weapon_reference_preserves_actual_item_name(name):
    g = game()
    g.world.here.ground_items = [(name, 1), ("布條", 2)]
    result = resolve(g, Intent("take", actor="依思", item="武器"))
    assert result.accepted and g.inventory["依思"] == [[name, 1]]
    assert g.world.here.ground_items == [("布條", 2)]


@pytest.mark.parametrize("value", ["武器", "the weapon", "その武器", "治療藥水、武器"])
def test_ambiguous_weapon_reference_asks_without_partial_transfers_or_turn(value):
    g = game()
    g.world.here.ground_items.append(("長劍", 1))
    before = deepcopy(encode_game(g))
    result = resolve(g, Intent("take", actor="依思", item=value))
    assert not result.accepted and "匕首、長劍" in "".join(result.lines)
    assert encode_game(g) == before
    assert not any(e.kind in ("materialize", "item") for e in g.ledger.entries)


@pytest.mark.parametrize("ground", [[], [("火把", 1)], [("空瓶", 1)], [("匕首", 0)]])
def test_missing_weapon_reference_cannot_select_hidden_carried_or_improvised_items(ground):
    g = game()
    g.world.here.ground_items = ground
    g.world.here.hidden_items = [("長劍", 1)]
    g.give_item("依思", "匕首", 1)
    before = deepcopy(encode_game(g))
    result = resolve(g, Intent("take", actor="依思", item="武器"))
    assert not result.accepted and "地上看不到" in "".join(result.lines)
    assert encode_game(g) == before


def test_reference_and_actual_name_share_one_transfer_per_stack():
    g = game()
    result = resolve(g, parse_command("依思 拾起治療藥水、武器、匕首", g.party))
    assert result.accepted
    assert g.inventory["依思"] == [["治療藥水", 2], ["匕首", 1]]
    assert g.world.here.ground_items == [("火把", 3)]
    assert len([e for e in g.ledger.entries if e.kind == "item"]) == 2


@pytest.mark.parametrize("name", ["武器", "兵器", "weapon", "the weapon", "その武器"])
def test_weapon_labels_cannot_be_materialized_through_other_paths(name):
    g = game()
    before = deepcopy(encode_game(g))
    assert classify(name) == "blocked"
    assert not try_materialize(g, "依思", name)
    assert encode_game(g) == before


@pytest.mark.parametrize("text", [
    "如果執起武器", "唔好執起武器", "執起武器然後攻擊船長", "執起武器？",
    "pick up the weapon on the ground then attack", "地面の武器を手に取る？",
    "もし地面の武器を手に取る", "武器を拾ってから匕首を手に取る",
])
def test_qualified_requests_never_execute_a_pickup_prefix(text):
    assert parse_pickup(text) is None


@pytest.mark.parametrize("routing", [False, True])
@pytest.mark.parametrize("text", [REQUESTS[1], REQUESTS[5], REQUESTS[7]])
def test_reference_transfer_is_direct_durable_and_repeated_reference_is_read_only(tmp_path, routing, text):
    endpoint = Endpoint(router=[router_answer(category="creative", compound="yes", improvisation="yes") for _ in range(2)])
    s = routed(tmp_path, endpoint)[0] if routing else supervised(tmp_path, endpoint)
    s.game = game()
    s._save()
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "拾起 匕首×1" in "".join(lines)
    assert s.game.inventory["依思"] == [["匕首", 1]]
    assert encode_game(restart(tmp_path).game) == encode_game(s.game)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "地上看不到" in "".join(lines)
    assert encode_game(s.game) == before
    assert roles(endpoint) == (["router", "narrator", "router"] if routing else ["narrator"])
    assert not endpoint.unexpected


@pytest.mark.parametrize("routing", [False, True])
def test_operator_cannot_establish_another_weapon_to_resolve_an_ambiguous_reference(tmp_path, routing):
    endpoint = Endpoint(router=[router_answer(category="creative", improvisation="yes")])
    s = routed(tmp_path, endpoint)[0] if routing else supervised(tmp_path, endpoint)
    s.game = game()
    s.game.world.here.ground_items.append(("長劍", 1))
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle("拾起地上武器", user_id="u1"))
    assert "匕首、長劍" in "".join(lines)
    assert encode_game(s.game) == before
    assert roles(endpoint) == (["router"] if routing else [])
    assert not endpoint.unexpected
