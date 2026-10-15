"""Meaningful effects, resource bounds, public hints and migration invariants."""
from copy import deepcopy

import pytest

from core.agent_config import HintConfig
from core.cli import build_demo_game
from core.hints import progress_key, track_attempt
from core.improvisation import parse_proposal, verify_improvisation
from core.intent import Intent
from core.rules_core import resolve, resolve_pending_check
from core.state import decode_game, encode_game
from core.world import Enemy
from test_campaign import run
from test_operators import Endpoint
from test_system_one import creative_endpoint, proposal, routed, router_answer


def test_damage_and_cover_use_engine_numbers_and_single_combat_slot():
    g = build_demo_game()
    g.enemies["影怪"] = Enemy.make("影怪", 30, 10)
    g.combat.order = [{"name": name, "npc": False} for name in g.party] + [{"name": "影怪", "npc": True}]
    g.d20 = lambda: 20
    actor = next(iter(g.party))
    raw = proposal(success=[{"kind": "damage", "target": "影怪"}, {"kind": "cover", "target": actor}])
    plan = parse_proposal(raw, g, actor, 1)
    before_turn = g.ledger.turn
    result = resolve(g, Intent("creative", actor=actor, args={"improvisation": plan}))
    assert result.accepted and 26 <= g.enemies["影怪"].hp <= 29
    assert g._dodge[actor] is True and g.combat.acted == [actor]
    assert g.ledger.turn == before_turn + 1
    assert not resolve(g, Intent("creative", actor=actor, args={"improvisation": plan})).accepted


def test_healing_pays_potion_once_even_if_check_is_pending():
    g = build_demo_game()
    actor = next(iter(g.party))
    g.inventory[actor] = [["治療藥水", 1]]
    g.party[actor]["hp_now"] = 1
    raw = proposal(costs=["治療藥水"], success=[{"kind": "heal", "target": actor}])
    plan = parse_proposal(raw, g, actor, 1)
    resolve(g, Intent("creative", actor=actor, args={"improvisation": plan}))
    assert g.inventory[actor] == [] and g._pending_check
    resolve_pending_check(g, 20)
    assert 1 < g.party[actor]["hp_now"] <= g.party[actor]["hp_max"]
    hp = g.party[actor]["hp_now"]
    resolve_pending_check(g, 20)
    assert g.party[actor]["hp_now"] == hp
    with pytest.raises(ValueError, match="healing_requires_potion"):
        parse_proposal(proposal(success=[{"kind": "heal", "target": actor}]), g, actor, 1)


def test_failed_checks_apply_only_approved_failure_effects_and_no_reward():
    g = build_demo_game()
    actor = next(iter(g.party))
    npc = g.world.here.npcs[0]
    before = npc["disposition"]
    plan = parse_proposal(proposal(success=[{"kind": "reward", "catalog": "salvage"}],
        failure=[{"kind": "npc_reaction", "target": npc["name"]}]), g, actor, 1)
    resolve(g, Intent("creative", actor=actor, args={"improvisation": plan}))
    resolve_pending_check(g, 1)
    assert npc["disposition"] != before
    assert not any(n == "繩索" for n, _ in g.inventory[actor])


def test_reward_allowance_persists_and_is_shared_across_players():
    g = build_demo_game()
    actor = next(iter(g.party))
    plan = parse_proposal(proposal(success=[{"kind": "reward", "catalog": "salvage"}]), g, actor, 1)
    resolve(g, Intent("creative", actor=actor, args={"improvisation": plan}))
    resolve_pending_check(g, 20)
    restored = decode_game(encode_game(g), [])
    other = next(n for n in g.party if n != actor)
    with pytest.raises(ValueError, match="reward_allowance_exhausted"):
        parse_proposal(proposal(success=[{"kind": "reward", "catalog": "salvage"}]), restored, other, 2)


def test_stale_plan_and_changed_scene_cannot_apply_effects():
    g = build_demo_game()
    actor = next(iter(g.party))
    plan = parse_proposal(proposal(), g, actor, 1)
    assert verify_improvisation(g, plan, revision=2) == "stale_revision"
    resolve(g, Intent("creative", actor=actor, args={"improvisation": plan}))
    old = g.world.here.npcs[0]["disposition"]
    original = g.world.current
    g.world.current = next(sid for sid in g.world.scenes if sid != original)
    _, line = resolve_pending_check(g, 20)
    assert "未生效" in line and g.world.scenes[original].npcs[0]["disposition"] == old


def test_stalls_count_rejections_reset_on_progress_and_obey_cooldown():
    g, state, config = build_demo_game(), {}, HintConfig()
    g.world.here.hidden_items.append(("秘密物件", 1))
    g.world.here.npcs[0]["knows"].append("秘密地點")
    before = progress_key(g, {})
    hints = [track_attempt(state, g, config, before=before, plot={}, eligible=True) for _ in range(6)]
    assert [bool(h) for h in hints] == [False, False, True, False, False, True]
    assert "秘密" not in hints[2]
    g.world.here.npcs[0].setdefault("disclosed", []).append("公開情報")
    assert not track_attempt(state, g, config, before=before, plot={}, eligible=True)
    assert state["stalls"]["count"] == 0
    count = state["stalls"]["attempts"]
    track_attempt(state, g, config, before=progress_key(g, {}), plot={}, eligible=False)
    assert state["stalls"]["attempts"] == count


def test_rejected_attempt_hints_survive_restart(tmp_path):
    endpoint = Endpoint(router=[router_answer(category="unknown", ambiguous="yes")] * 6,
                        action=[{"action": "unknown"}] * 6)
    s, _ = routed(tmp_path, endpoint)
    for index in range(3):
        lines, _ = run(s.handle("試圖處理同一個難題", user_id="u1"))
        assert any("💡" in line for line in lines) == (index == 2)
    from core.service import V4Service
    restored = V4Service(str(tmp_path), channel_id="t", gateway=s.gateway)
    lines, _ = run(restored.handle("試圖處理同一個難題", user_id="u1"))
    assert not any("💡" in line for line in lines)
    assert restored._operator_state["stalls"]["count"] == 4


def test_old_snapshot_and_pending_effects_still_decode():
    g = build_demo_game()
    blob = deepcopy(encode_game(g))
    blob["v"] = 7
    blob["effects"].pop("_improv_rewards")
    blob["effects"]["_pending_check"] = {"actor": next(iter(g.party)), "dc": 10, "mod": 0,
        "ability": "DEX", "skill": "", "effect": {"kind": "search"}}
    restored = decode_game(blob, [])
    assert restored._pending_check["effect"]["kind"] == "search"
    resolve_pending_check(restored, 20)


@pytest.mark.parametrize("payload", [None, [], "invalid", {"actor": "someone"}])
def test_malformed_improvisation_is_denied_without_effects(payload):
    g = build_demo_game()
    before = deepcopy(encode_game(g))
    result = resolve(g, Intent("creative", actor=next(iter(g.party)), args={"improvisation": payload}))
    assert not result.accepted and encode_game(g) == before


def test_settling_creative_check_resets_stall_counter_without_counting_an_attempt(tmp_path):
    s, _ = routed(tmp_path, creative_endpoint())
    s._operator_state["stalls"] = {"scene": "dock", "count": 3, "attempts": 3, "last_hint": 3}
    run(s.handle("用帽子演恐怖短劇", user_id="u1"))
    assert s.game._pending_check
    attempts = s._operator_state["stalls"]["attempts"]
    run(s.settle_roll(20, user_id="u1"))
    assert s._operator_state["stalls"]["count"] == 0
    assert s._operator_state["stalls"]["attempts"] == attempts
