"""General operator review, ordinary possessions and engine-owned conditions."""
from copy import deepcopy
import json

import httpx
import pytest

from core.cli import build_demo_game
from core.creative_effects import active_conditions, modifier, item_properties
from core.debug_capture import DebugCapture
from core.improvisation import parse_attempt, verify_improvisation
from core.intent import Intent
from core.memory import build_memory
from core.persistence import StorageError
from core.rules_core import resolve, resolve_pending_check, _enemy_turn
from core.service import V4Service
from core.state import decode_game, encode_game
from core.trace_eval import read_traces, replay
from core.turn import Game
from core.world import Enemy
from test_campaign import run
from test_operators import Endpoint, roles, supervised, audits
from test_system_one import routed, router_answer


ACTOR, NPC = "依思", "老船長"


def attempt(**updates):
    return {"method": "用普通物件分散船長注意力", "resolution": "check", "skill": "performance",
            "difficulty": "hard", "prerequisites": [], "setup": [], "costs": [],
            "success": [{"kind": "npc_reaction", "target": NPC}], "failure": [], **updates}


def decision(proposal=None, **updates):
    return {"decision": "attempt", "reason": "符合現場環境，可按此方法嘗試。",
            "proposal": None if updates.get("decision", "attempt") != "attempt" else proposal if proposal is not None else attempt(), **updates}


def fact(owner=NPC, item="破襯衫", template="clothing"):
    return {"owner": owner, "item": item, "template": template,
            "basis": "這是現場人物穿著的一件普通衣物；本次嘗試只處理這件衣物。"}


def transfer(owner=NPC, item="破襯衫", recipient=ACTOR):
    return {"kind": "transfer", "owner": owner, "item": item, "recipient": recipient}


def condition(target=ACTOR, template="cover", expires="scene_exit"):
    return {"kind": "condition", "target": target, "template": template,
            "description": "躲在臨時木板掩體後" if template == "cover" else "被繩索束縛",
            "expires": expires, "removal": "離開掩體" if template == "cover" else "解開或切斷繩索"}


def ep(raw, *, parsed=None, route=None, approve=True):
    return Endpoint(router=[route or router_answer(category="creative", improvisation="yes", creative_detail="yes")],
                    action=[parsed or {"action": "creative", "utterance": "照玩家原話嘗試"}],
                    director=[raw], evaluator=[{"approved": approve}], main=[{"approved": True}])


@pytest.mark.parametrize("text,item", [
    ("偷取老船長的破襯衫", "破襯衫"),
    ("船長のシャツを盗む", "シャツ"),
    ("steal an old shirt from 老船長", "old shirt"),
])
def test_recognized_missing_possessions_receive_review_and_preserve_exact_words(tmp_path, text, item):
    raw = attempt(method=text, setup=[fact(item=item)], success=[transfer(item=item)])
    endpoint = ep(decision(raw))
    s, _ = routed(tmp_path, endpoint)
    before = deepcopy(s.game.inventory)
    lines, prose = run(s.handle(text, user_id="u1"))
    assert "director" in roles(endpoint) and "evaluator" in roles(endpoint) and "main" in roles(endpoint)
    assert not endpoint.unexpected
    assert s.game._pending_check["effect"]["plan"]["player_words"] == text
    assert s.game.inventory == before and not s.game._creative_provenance
    assert "尚未" in prose and "🎒" not in "".join(lines)
    prompt = next(p for r, p in endpoint.calls if r == "director")
    assert text in prompt and ACTOR in prompt and "匕首" in prompt and "combat" in prompt
    assert "owner_id" not in prompt and '"u1"' not in prompt
    run(s.settle_roll(20, user_id="u1"))
    assert [item, 1] in s.game.inventory[ACTOR]
    assert item not in s.game.scene_npcs()[0]["loot"]
    assert s.game._creative_provenance[0]["owner"] == NPC
    assert s.game._creative_provenance[0]["properties"]
    assert s.game._creative_provenance[0]["event_id"]
    assert "flammable" in item_properties(s.game, item)


def test_unknown_action_category_still_receives_scene_review(tmp_path):
    endpoint = ep(decision(), parsed={"action": "distraction"}, route=router_answer(category="unknown", ambiguous="yes"))
    s, _ = routed(tmp_path, endpoint)
    run(s.handle("I distract the captain by imitating a gull", user_id="u1"))
    assert s.game._pending_check and "director" in roles(endpoint)
    assert not endpoint.unexpected


def test_unrecorded_ordinary_weapon_can_be_proposed_but_not_recreated_after_removal(tmp_path):
    raw = attempt(skill="sleight of hand", setup=[fact(NPC, "匕首", "knife")], success=[transfer(NPC, "匕首")])
    endpoint = ep(decision(raw))
    s, _ = routed(tmp_path, endpoint)
    s.game.scene_npcs()[0].pop("loot")
    run(s.handle("船長の武器を盗む", user_id="u1"))
    run(s.settle_roll(20, user_id="u1"))
    assert ["匕首", 1] in s.game.inventory[ACTOR]
    s.game.take_item(ACTOR, "匕首")
    with pytest.raises(ValueError, match="possession_already_established|established_absence"):
        parse_attempt(raw, s.game, ACTOR, s._revision + 1, "steal again")


def test_possession_owner_cannot_replenish_by_changing_scene_or_template():
    g = build_demo_game()
    raw = attempt(setup=[fact()], success=[transfer()])
    plan = parse_attempt(raw, g, ACTOR, 1, "steal shirt")
    resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
    resolve_pending_check(g, 20)
    captain = g.world.here.npcs.pop(0)
    g.world.current = "tavern"
    g.world.here.npcs.append(captain)
    raw["setup"][0]["item"] = raw["success"][0]["item"] = "another shirt"
    with pytest.raises(ValueError, match="possession_already_established"):
        parse_attempt(raw, g, ACTOR, 2, "steal another shirt")
    raw["setup"][0]["template"] = "prop"
    with pytest.raises(ValueError, match="ordinary_item_not_supported"):
        parse_attempt(raw, g, ACTOR, 2, "steal another shirt")


def test_operator_cannot_drop_known_companion_requirement(tmp_path):
    raw = attempt(skill="sleight of hand", success=[transfer(NPC, "匕首")])
    endpoint = ep(decision(raw))
    s, _ = routed(tmp_path, endpoint)
    before = deepcopy(encode_game(s.game))
    text = "仲間が船長の注意を引いている間に船長の物を盗む。"
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "/help" in "".join(lines) and encode_game(s.game) == before
    assert not getattr(s.game, "_pending_check", None)


def test_unfamiliar_linked_method_serving_one_goal_receives_general_review(tmp_path):
    raw = attempt(method="繞到船長背後，用叫聲分散注意力", success=[condition(NPC, "distracted", "next_action")])
    endpoint = ep(decision(raw), parsed={"relationship": "linked", "goal": "distract", "steps": [
        {"action": "position", "target": NPC}, {"action": "distract", "target": NPC}]},
        route=router_answer(category="creative", compound="yes", linked="yes", improvisation="yes"))
    s, _ = routed(tmp_path, endpoint)
    text = "Circle behind the captain and mimic a gull to distract him"
    run(s.handle(text, user_id="u1"))
    assert s.game._pending_check and not endpoint.unexpected
    assert s.game._pending_check["effect"]["plan"]["player_words"] == text


def test_cantonese_cover_and_japanese_crafting_have_committed_persistent_effects(tmp_path):
    raw = attempt(method="用木板搭掩體", costs=["木板"], success=[condition()])
    endpoint = ep(decision(raw))
    s, _ = routed(tmp_path, endpoint)
    s.game.inventory[ACTOR] = [["木板", 1]]
    run(s.handle("我用木板整個掩體匿埋", user_id="u1"))
    assert not s.game.inventory[ACTOR] and not active_conditions(s.game)
    run(s.settle_roll(20, user_id="u1"))
    assert modifier(s.game, ACTOR, "attacks_against_disadvantage")
    assert "臨時木板" in build_memory(s.game).render()
    # A different request can craft an ordinary item from real materials.
    raw = attempt(method="布と紐を編んで袋を作る", costs=["布條", "繩索"],
                  prerequisites=[{"kind": "possession", "owner": ACTOR, "item": "布條"}],
                  setup=[fact("scene", "布袋", "container")], success=[transfer("scene", "布袋")])
    endpoint = ep(decision(raw))
    s.gateway.transport = httpx.MockTransport(endpoint)
    s.game.inventory[ACTOR] = [["布條", 1], ["繩索", 1]]
    run(s.handle("布と紐を編んで袋を作る", user_id="u1"))
    run(s.settle_roll(20, user_id="u1"))
    assert s.game.inventory[ACTOR] == [["布袋", 1]]
    assert not endpoint.unexpected


def test_corpse_clothing_is_automatic_and_cannot_replenish_after_restart(tmp_path, monkeypatch):
    raw = attempt(method="從屍體取下外套", resolution="automatic", skill="", difficulty="",
                  setup=[fact("守衛", "舊外套")], success=[transfer("守衛", "舊外套")])
    endpoint = ep(decision(raw))
    s, _ = routed(tmp_path, endpoint)
    s.game.encounters["dock"] = [Enemy.make("守衛", 0, 12)]
    monkeypatch.setattr(Game, "d20", lambda self: pytest.fail("automatic outcome rolled a die"))
    lines, prose = run(s.handle("Take the coat off the guard's corpse", user_id="u1"))
    assert ["舊外套", 1] in s.game.inventory[ACTOR]
    assert not getattr(s.game, "_pending_check", None) and "移至" in prose
    assert "成功" not in "".join(lines[:1])  # only the resolved effects establish success
    s = V4Service(str(tmp_path), channel_id="t", gateway=s.gateway)
    s.game.take_item(ACTOR, "舊外套")
    raw["setup"][0]["item"] = raw["success"][0]["item"] = "另一件外套"
    with pytest.raises(ValueError, match="possession_already_established"):
        parse_attempt(raw, s.game, ACTOR, s._revision, "take another coat")


@pytest.mark.parametrize("die", [1, 20])
def test_failed_or_successful_check_reuses_existing_items_and_settles_once(die):
    g = build_demo_game()
    g.party[ACTOR]["stats"]["DEX"] = 1
    raw = attempt(skill="sleight of hand", success=[transfer(NPC, "匕首")])
    plan = parse_attempt(raw, g, ACTOR, 1, "steal the existing dagger")
    resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
    restored = decode_game(encode_game(g), [])
    resolve_pending_check(restored, die)
    assert any(n == "匕首" for n, q in restored.inventory[ACTOR]) == (die == 20)
    assert restored.scene_npcs()[0]["loot"].count("匕首") == (0 if die == 20 else 1)
    before = deepcopy(encode_game(restored))
    assert resolve_pending_check(restored, 20) == (False, "")
    assert not resolve(restored, Intent("creative", actor=ACTOR, args={"improvisation": plan})).accepted
    assert encode_game(restored) == before


def test_failed_setup_never_grants_items_and_cancellation_is_owned(tmp_path):
    raw = attempt(setup=[fact()], success=[transfer()], costs=["布條"])
    endpoint = ep(decision(raw))
    s, _ = routed(tmp_path, endpoint)
    s.game.inventory[ACTOR] = [["布條", 1]]
    run(s.handle("偷取老船長的破襯衫", user_id="u1"))
    plan_id = s.game._pending_check["effect"]["plan"]["id"]
    run(s.action_plan("cancel", user_id="u2"))
    assert s.game._pending_check
    run(s.action_plan("cancel", user_id="u1"))
    assert not s.game._pending_check and s.game._creative_attempts[plan_id] == "cancelled"
    assert not s.game._creative_provenance and not s.game.inventory[ACTOR]
    endpoint = ep(decision(raw))
    s.gateway.transport = httpx.MockTransport(endpoint)
    s.game.inventory[ACTOR] = [["布條", 1]]
    run(s.handle("偷取老船長的破襯衫", user_id="u1"))
    _, prose = run(s.settle_roll(1, user_id="u1"))
    assert not s.game._creative_provenance and not s.game.inventory[ACTOR]
    assert "移至" not in prose
    assert "破襯衫" not in s.game.scene_npcs()[0]["loot"]


@pytest.mark.parametrize("change", ["item", "target", "scene", "actor", "life_state"])
def test_pending_transfers_revalidate_custody_targets_and_scene(change):
    g = build_demo_game()
    raw = attempt(success=[transfer(NPC, "匕首")])
    plan = parse_attempt(raw, g, ACTOR, 1, "steal")
    assert verify_improvisation(g, plan, revision=2) == "stale_revision"
    resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
    if change == "item":
        g.scene_npcs()[0]["loot"].clear()
    elif change == "target":
        g.world.here.npcs.clear()
    elif change == "scene":
        g.world.current = "tavern"
    elif change == "actor":
        g.party[ACTOR]["hp_now"] = 0
    else:
        g.encounters["dock"] = [Enemy.make(NPC, 0, 13, loot=["匕首"])]
    inventory = deepcopy(g.inventory)
    _, line = resolve_pending_check(g, 20)
    assert "未生效" in line and g.inventory == inventory
    assert g._creative_attempts[plan["id"]] == "cancelled"


@pytest.mark.parametrize("problem", ["rare", "absence", "existing", "other_player", "duplicate", "automatic_theft"])
def test_invalid_proposals_change_neither_resources_nor_world(problem):
    g = build_demo_game()
    raw = attempt(setup=[fact()], success=[transfer()], costs=["治療藥水"])
    if problem == "rare":
        raw["setup"][0]["item"] = raw["success"][0]["item"] = "gold enchanted shirt"
    elif problem == "absence":
        g.scene_npcs()[0]["absent_items"] = ["破襯衫"]
    elif problem == "existing":
        g.world.here.hidden_items = [("破襯衫", 1)]
    elif problem == "other_player":
        raw["setup"][0]["owner"] = raw["success"][0]["owner"] = "大力蕉"
    elif problem == "duplicate":
        raw["setup"].append(fact(item="另一件襯衫"))
    else:
        raw.update(resolution="automatic", skill="", difficulty="")
    before = deepcopy(encode_game(g))
    with pytest.raises(ValueError):
        parse_attempt(raw, g, ACTOR, 1, "try")
    assert encode_game(g) == before


def test_existing_prop_effect_gets_provenance_and_reuses_instead_of_replenishing():
    g = build_demo_game()
    raw = attempt(success=[{"kind": "prop", "content": {"name": "破舊木雕"}}])
    plan = parse_attempt(raw, g, ACTOR, 1, "carve a small prop")
    resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
    resolve_pending_check(g, 20)
    assert ("破舊木雕", 1) in g.world.here.ground_items
    assert g._creative_provenance[0]["owner"] == "scene"
    resolve(g, Intent("take", actor=ACTOR, item="破舊木雕"))
    g.take_item(ACTOR, "破舊木雕")
    with pytest.raises(ValueError, match="possession_previously_removed|possession_already_established"):
        parse_attempt(raw, g, ACTOR, 2, "create the same prop again")


def test_duplicate_or_interdependent_effects_are_rejected_before_costs():
    g = build_demo_game()
    foe = Enemy.make("守衛", 1, 10, loot=["匕首"])
    g.enemies[foe.name] = foe
    raw = attempt(costs=["治療藥水"], success=[{"kind": "damage", "target": foe.name}, transfer(foe.name, "匕首")])
    before = deepcopy(encode_game(g))
    with pytest.raises(ValueError, match="dependent_effects_not_supported"):
        parse_attempt(raw, g, ACTOR, 1, "stab and steal")
    raw["success"] = [condition(), condition()]
    with pytest.raises(ValueError, match="duplicate_effect"):
        parse_attempt(raw, g, ACTOR, 1, "cover")
    assert encode_game(g) == before


def test_failed_attempt_can_apply_only_an_approved_actor_condition():
    g = build_demo_game()
    raw = attempt(setup=[fact()], success=[transfer()], failure=[condition(ACTOR, "restrained", "released")])
    plan = parse_attempt(raw, g, ACTOR, 1, "try an awkward theft")
    resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
    resolve_pending_check(g, 1)
    assert not g._creative_provenance and active_conditions(g, ACTOR)
    assert not resolve(g, Intent("move", actor=ACTOR, destination="酒館")).accepted


def test_malformed_setup_and_custom_condition_rules_never_spend_resources():
    g = build_demo_game()
    before = deepcopy(encode_game(g))
    raw = attempt(setup=[fact(), None], success=[transfer()], costs=["治療藥水"])
    with pytest.raises(ValueError, match="invalid_possession_fields"):
        parse_attempt(raw, g, ACTOR, 1, "try")
    raw = attempt(success=[{**condition(), "modifiers": ["automatic_success"]}], costs=["治療藥水"])
    with pytest.raises(ValueError, match="invalid_condition_fields"):
        parse_attempt(raw, g, ACTOR, 1, "try")
    raw = attempt(setup=[fact(NPC, "rope and sword", "cord")], success=[transfer(NPC, "rope and sword")])
    with pytest.raises(ValueError, match="ordinary_item_not_supported"):
        parse_attempt(raw, g, ACTOR, 1, "try")
    assert encode_game(g) == before


def test_successful_restraint_blocks_attacks_persists_and_can_be_released(tmp_path):
    raw = attempt(method="用繩索綁住守衛雙手", costs=["繩索"], success=[condition("守衛", "bound", "released")])
    endpoint = ep(decision(raw))
    s, _ = routed(tmp_path, endpoint)
    s.game.inventory[ACTOR] = [["繩索", 1]]
    foe = Enemy.make("守衛", 30, 10)
    s.game.enemies[foe.name] = foe
    s.game.encounters["dock"] = [foe]
    s.game.combat.order = [{"name": n, "npc": False} for n in s.game.party] + [{"name": foe.name, "npc": True}]
    s.game.d20 = lambda: 20
    run(s.handle("Bind the guard's hands with rope", user_id="u1"))
    assert s.game.combat.acted == [ACTOR] and not s.game.inventory[ACTOR]
    assert active_conditions(s.game, foe.name)
    hp = {n: p["hp_now"] for n, p in s.game.party.items()}
    assert "束縛" in "".join(_enemy_turn(s.game, foe))
    assert hp == {n: p["hp_now"] for n, p in s.game.party.items()}
    restored = decode_game(encode_game(s.game), [])
    identifier = next(iter(restored._creative_conditions))
    restored.combat.acted.clear()
    release = parse_attempt(attempt(resolution="automatic", skill="", difficulty="",
        success=[{"kind": "clear_condition", "condition_id": identifier}]), restored, ACTOR, 20, "untie the guard")
    resolve(restored, Intent("creative", actor=ACTOR, args={"improvisation": release}))
    assert not active_conditions(restored)


def test_conditions_modify_later_rolls_and_expire_on_action_scene_or_combat():
    g = build_demo_game()
    g.party[ACTOR]["stats"]["STR"] = 10
    plan = parse_attempt(attempt(resolution="automatic", skill="", difficulty="",
        success=[condition(ACTOR, "distracted", "next_action")]), g, ACTOR, 1, "distracted")
    resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
    assert active_conditions(g, ACTOR)
    resolve(g, Intent("check", actor=ACTOR, ability="STR"))
    assert g._pending_check["disadvantaged"] and not active_conditions(g, ACTOR)
    g.d20 = lambda: 1
    ok, line = resolve_pending_check(g, 20)
    assert not ok and "狀態劣勢" in line
    for expiry in ("scene_exit", "end_combat"):
        raw = attempt(resolution="automatic", skill="", difficulty="", success=[condition(expires=expiry)])
        plan = parse_attempt(raw, g, ACTOR, g.ledger.turn + 10, expiry)
        resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
        if expiry == "scene_exit":
            resolve(g, Intent("move", actor=ACTOR, destination="酒館"))
        else:
            g.end_combat()
        assert not active_conditions(g)


def test_released_condition_follows_actor_and_cannot_be_bypassed_by_creative_transfer():
    g = build_demo_game()
    plan = parse_attempt(attempt(resolution="automatic", skill="", difficulty="",
        success=[condition(ACTOR, "bound", "released")]), g, ACTOR, 1, "bind own hands")
    resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
    resolve(g, Intent("move", actor=ACTOR, destination="酒館"))
    assert active_conditions(g, ACTOR)
    g.world.here.ground_items.append(("布條", 1))
    with pytest.raises(ValueError, match="condition_restricts_transfer"):
        parse_attempt(attempt(resolution="automatic", skill="", difficulty="",
            success=[transfer("scene", "布條")]), g, ACTOR, 2, "pick up cloth")
    identifier = next(iter(g._creative_conditions))
    release = parse_attempt(attempt(resolution="automatic", skill="", difficulty="",
        success=[{"kind": "clear_condition", "condition_id": identifier}]), g, ACTOR, 3, "untie my hands")
    resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": release}))
    assert not active_conditions(g, ACTOR)


def test_condition_attack_modifiers_apply_to_ordinary_and_thrown_attacks():
    for thrown in (False, True):
        g = build_demo_game()
        foe = Enemy.make("守衛", 30, 15)
        g.enemies[foe.name] = foe
        g.encounters["dock"] = [foe]
        plan = parse_attempt(attempt(resolution="automatic", skill="", difficulty="",
            success=[condition(ACTOR, "distracted", "next_action")]), g, ACTOR, 1, "distracted")
        resolve(g, Intent("creative", actor=ACTOR, args={"improvisation": plan}))
        unconfirmed = resolve(g, Intent("attack", actor=ACTOR, target=foe.name, confidence=.2))
        assert unconfirmed.confirm and active_conditions(g, ACTOR)
        dice = iter([20, 1])
        g.d20 = lambda: next(dice)
        g.inventory[ACTOR] = [["石頭", 1]]
        result = resolve(g, Intent("attack", actor=ACTOR, target=foe.name,
            args={"thrown_item": "石頭"} if thrown else {}))
        assert result.accepted and foe.hp == 30
        assert not active_conditions(g, ACTOR)
        assert next(e for e in reversed(g.ledger.entries) if e.kind == "attack").data["hit"] is False


@pytest.mark.parametrize("outcome,reason", [
    ("clarification_needed", "附近有兩名守衛，請指定要束縛哪一名。"),
    ("not_plausible", "對方已離開碼頭，現在無法取下他的衣服。"),
])
def test_reviewed_non_attempts_explain_actual_constraint_without_mutation(tmp_path, outcome, reason):
    endpoint = ep(decision(decision=outcome, reason=reason, proposal=None))
    s, _ = routed(tmp_path, endpoint)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle("取下那位守衛的衣服", user_id="u1"))
    assert reason in "".join(lines) and encode_game(s.game) == before
    assert roles(endpoint) == ["router", "action", "director", "evaluator", "main"]


def test_operator_outage_and_unsupported_effect_are_not_implausibility(tmp_path):
    endpoint = ep(httpx.Response(503))
    s, _ = routed(tmp_path, endpoint)
    lines, _ = run(s.handle("I improvise a distraction", user_id="u1"))
    assert "操作員服務" in "".join(lines) and "不合理" not in "".join(lines)
    endpoint = ep(decision(attempt(success=[{"kind": "execute", "code": "g.inventory.clear()"}])))
    s.gateway.transport = httpx.MockTransport(endpoint)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle("I improvise a distraction", user_id="u1"))
    assert "未支援" in "".join(lines) and encode_game(s.game) == before


def test_reviewed_effects_roll_back_with_the_snapshot_and_replay_offline(tmp_path, monkeypatch):
    raw = attempt(setup=[fact()], success=[transfer()])
    endpoint = ep(decision(raw))
    s, _ = routed(tmp_path, endpoint, native=True, native_error=503)
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.handle("偷取老船長的破襯衫", user_id="u1"))
    rows = next(iter(read_traces(tmp_path / "debug").values()))
    replayed = run(replay(rows))
    assert replayed["output_matches"] and replayed["snapshot_matches"] and replayed["all_calls_consumed"]
    assert not replayed["request_mismatches"]
    before = deepcopy(encode_game(s.game))
    commit = s.repository.commit
    monkeypatch.setattr(s.repository, "commit", lambda *args, **kwargs: (_ for _ in ()).throw(StorageError("disk")))
    lines, _ = run(s.settle_roll(20, user_id="u1"))
    assert "未能儲存" in "".join(lines)
    assert encode_game(s.game) == before
    monkeypatch.setattr(s.repository, "commit", commit)
    run(s.settle_roll(20, user_id="u1"))
    assert ["破襯衫", 1] in s.game.inventory[ACTOR]


def test_older_saves_default_new_fields_to_empty_and_old_creative_plans_still_work():
    g = build_demo_game()
    for version in range(4, 10):
        blob = deepcopy(encode_game(g))
        blob["v"] = version
        for field in ("_creative_conditions", "_creative_provenance", "_creative_attempts", "_corpse_possessions"):
            del blob["effects"][field]
        restored = decode_game(blob, [])
        assert not restored._creative_conditions and not restored._creative_provenance
        assert not restored._creative_attempts and not restored._corpse_possessions


def test_routing_disabled_still_reviews_missing_objects(tmp_path):
    endpoint = Endpoint(director=[decision(attempt(setup=[fact()], success=[transfer()]))],
                        evaluator=[{"approved": True}], main=[{"approved": True}])
    s = supervised(tmp_path, endpoint)
    run(s.handle("船長の破襯衫を盗む", user_id="u1"))
    assert s.game._pending_check and roles(endpoint) == ["director", "evaluator", "main"]
