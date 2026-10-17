"""Engine-owned vocabulary for improvised actions. No model or transport access."""
from dataclasses import asdict, dataclass
from copy import deepcopy
import hashlib
import json

from engine.charlib import SKILL_ABILITY
from .director import DirectorProposal, canonize_scene, verify_proposal

DIFFICULTIES = {"easy": 10, "moderate": 14, "hard": 18}
REWARD_CATALOG = {"salvage": "繩索"}  # one mundane reward per scene, for the whole party
HEALING_ITEM = "治療藥水"
EFFECTS = ("damage", "heal", "advantage", "cover", "npc_reaction", "prop", "scene", "reward",
           "transfer", "condition", "clear_condition")


@dataclass(frozen=True)
class ImprovisationProposal:
    source_revision: int
    origin_scene: str
    actor: str
    method: str
    skill: str
    difficulty: str
    costs: list
    success: list
    failure: list
    version: int = 1


def parse_proposal(raw, g, actor, revision):
    if type(raw) is dict and "resolution" in raw:
        return parse_attempt(raw, g, actor, revision)
    if type(raw) is not dict or set(raw) != {"method", "skill", "difficulty", "costs", "success", "failure"}:
        raise ValueError("Invalid improvisation fields")
    proposal = asdict(ImprovisationProposal(revision, g.world.current, actor, **raw))
    reason = verify_improvisation(g, proposal, revision=revision)
    if reason:
        raise ValueError(reason)
    return proposal


ATTEMPT_FIELDS = {"method", "resolution", "skill", "difficulty", "prerequisites", "setup", "costs", "success", "failure"}


def parse_attempt(raw, g, actor, revision, player_words=""):
    """Version two carries explicit automatic/check resolution and ordinary facts."""
    from .creative_effects import owner_state
    if type(raw) is not dict or set(raw) != ATTEMPT_FIELDS:
        raise ValueError("invalid_attempt_fields")
    identifier = hashlib.sha256(json.dumps([actor, revision, player_words, raw], ensure_ascii=False,
                                          sort_keys=True, allow_nan=False).encode()).hexdigest()[:20]
    plan = {"version": 2, "id": identifier, "source_revision": revision, "origin_scene": g.world.current,
            "actor": actor, "player_words": player_words, **deepcopy(raw)}
    # Validate container/field types before collecting trusted identity bindings.
    reason = verify_attempt(g, plan, revision=revision, binding=False)
    if reason:
        raise ValueError(reason)
    owners = {f["owner"] for f in plan["setup"]}
    owners.update(r["owner"] for r in plan["prerequisites"] if r["kind"] == "possession")
    for effect in plan["success"] + plan["failure"]:
        owners.update(effect[k] for k in ("owner", "target") if k in effect)
    plan["bindings"] = {name: owner_state(g, name) for name in sorted(owners)}
    reason = verify_attempt(g, plan, revision=revision)
    if reason:
        raise ValueError(reason)
    return plan


def verify_attempt(g, plan, *, revision=None, settling=False, binding=True):
    from .creative_effects import owner_state, verify_setup, verify_prerequisites, verify_effect, restriction, ordinary_template
    fields = ATTEMPT_FIELDS | {"version", "id", "source_revision", "origin_scene", "actor", "player_words"}
    if binding:
        fields.add("bindings")
    if type(plan) is not dict or set(plan) != fields or type(plan.get("version")) is not int or plan["version"] != 2 \
            or type(plan.get("source_revision")) is not int or type(plan.get("id")) is not str:
        return "invalid_attempt"
    if revision is not None and plan["source_revision"] != revision:
        return "stale_revision"
    if type(plan["actor"]) is not str or plan["actor"] not in g.party or not g.alive(plan["actor"]):
        return "actor_unavailable"
    if plan["origin_scene"] != g.world.current:
        return "scene_changed"
    status = g._creative_attempts.get(plan["id"])
    if status is not None and (not settling or status != "pending"):
        return "attempt_already_resolved"
    if type(plan["method"]) is not str or not 1 <= len(plan["method"].strip()) <= 300 \
            or type(plan["player_words"]) is not str or len(plan["player_words"]) > 1500:
        return "invalid_method"
    if plan["resolution"] == "check":
        if type(plan["skill"]) is not str or plan["skill"] not in SKILL_ABILITY \
                or type(plan["difficulty"]) is not str or plan["difficulty"] not in DIFFICULTIES:
            return "invalid_check_template"
    elif plan["resolution"] != "automatic" or plan["skill"] != "" or plan["difficulty"] != "":
        return "invalid_resolution"
    for name, limit in (("prerequisites", 4), ("setup", 2), ("costs", 2), ("success", 2), ("failure", 1)):
        if type(plan[name]) is not list or len(plan[name]) > limit:
            return "invalid_" + name
    if not plan["success"] or (plan["resolution"] == "automatic" and plan["failure"]):
        return "invalid_effect_count"
    costs = plan["costs"]
    if any(type(n) is not str for n in costs) or len(set(costs)) != len(costs):
        return "invalid_costs"
    if not settling and any(not any(n == item and q > 0 for n, q in g.inventory.get(plan["actor"], [])) for item in costs):
        return "missing_resource"
    slots = set()
    setup_items = set()
    for fact in plan["setup"]:
        if reason := verify_setup(g, fact, plan["actor"]):
            return reason
        slot = (fact["owner"], fact["template"])
        if slot in slots or fact["item"] in setup_items:
            return "duplicate_possession_setup"
        slots.add(slot)
        setup_items.add(fact["item"])
    if reason := verify_prerequisites(g, plan, settling=settling):
        return reason
    # Run the established effect verifier too, preserving all existing budgets.
    legacy = {k: plan[k] for k in ("source_revision", "origin_scene", "actor", "method", "costs")}
    legacy.update(version=1, skill=plan["skill"] or "athletics", difficulty=plan["difficulty"] or "moderate")
    legacy["success"] = []
    legacy["failure"] = []
    transfers = set()
    effect_keys = set()
    damaged = {e.get("target") for e in plan["success"] if isinstance(e, dict)
               and e.get("kind") == "damage" and isinstance(e.get("target"), str)}
    for branch in ("success", "failure"):
        for effect in plan[branch]:
            if type(effect) is not dict or type(effect.get("kind")) is not str or effect["kind"] not in EFFECTS:
                return "unknown_effect"
            if effect["kind"] in ("damage", "heal") and restriction(g, plan["actor"],
                    "attack" if effect["kind"] == "damage" else "use"):
                return "condition_restricts_effect"
            if plan["resolution"] == "automatic" and effect["kind"] in ("damage", "advantage"):
                return "opposed_effect_requires_check"
            try:
                key = (branch, json.dumps(effect, sort_keys=True, ensure_ascii=False, allow_nan=False))
            except (TypeError, ValueError):
                return "invalid_effect_fields"
            if key in effect_keys:
                return "duplicate_effect"
            effect_keys.add(key)
            if branch == "success" and effect["kind"] != "damage" \
                    and any(effect.get(k) in damaged for k in ("owner", "target") if isinstance(effect.get(k), str)):
                return "dependent_effects_not_supported"
            if effect["kind"] in ("transfer", "condition", "clear_condition"):
                if reason := verify_effect(g, effect, plan, failure=branch == "failure"):
                    return reason
                if effect["kind"] == "transfer":
                    key = (effect["owner"], effect["item"])
                    if key in transfers:
                        return "duplicate_transfer"
                    transfers.add(key)
            else:
                legacy[branch].append(effect)
    # Establishments on the acting character must be used in the attempt's
    # consequences, not an unrelated grant accompanying a failed distraction.
    for fact in plan["setup"]:
        if not any(e.get("kind") == "transfer" and e.get("owner") == fact["owner"]
                   and e.get("item") == fact["item"] for e in plan["success"]) \
                and not any(r.get("kind") == "possession" and r.get("owner") == fact["owner"]
                            and r.get("item") == fact["item"] for r in plan["prerequisites"]):
            return "unrelated_possession_setup"
    if legacy["success"] or legacy["failure"]:
        if not legacy["success"]:
            legacy["success"] = [{"kind": "cover", "target": plan["actor"]}]
        if reason := verify_improvisation(g, legacy, settling=settling):
            return reason
        for effect in legacy["success"]:
            if effect["kind"] == "prop":
                template = ordinary_template(effect["content"]["name"])
                fact = {"owner": "scene", "item": effect["content"]["name"], "template": template, "basis": plan["method"]}
                if reason := verify_setup(g, fact, plan["actor"]):
                    return reason
                if ("scene", template) in slots:
                    return "duplicate_possession_setup"
                slots.add(("scene", template))
    if binding:
        owners = {f["owner"] for f in plan["setup"]}
        owners.update(r["owner"] for r in plan["prerequisites"] if r["kind"] == "possession")
        for effect in plan["success"] + plan["failure"]:
            owners.update(effect[k] for k in ("owner", "target") if k in effect)
        if type(plan["bindings"]) is not dict or set(plan["bindings"]) != owners or any(
                type(n) is not str or type(s) is not str or owner_state(g, n) != s for n, s in plan["bindings"].items()):
            return "target_changed"
    return None


def verify_improvisation(g, plan, *, revision=None, settling=False):
    if isinstance(plan, dict) and plan.get("version") == 2:
        return verify_attempt(g, plan, revision=revision, settling=settling)
    if type(plan) is not dict or set(plan) != {"version", "source_revision", "origin_scene", "actor",
                                             "method", "skill", "difficulty", "costs", "success", "failure"}:
        return "invalid_improvisation"
    if type(plan["version"]) is not int or plan["version"] != 1 \
            or type(plan["source_revision"]) is not int:
        return "invalid_version"
    if revision is not None and plan["source_revision"] != revision:
        return "stale_revision"
    actor = plan["actor"]
    if not isinstance(actor, str) or actor not in g.party or not g.alive(actor):
        return "actor_unavailable"
    if plan["origin_scene"] != g.world.current:
        return "scene_changed"
    if type(plan["method"]) is not str or not 1 <= len(plan["method"]) <= 300 \
            or type(plan["skill"]) is not str or plan["skill"] not in SKILL_ABILITY \
            or type(plan["difficulty"]) is not str or plan["difficulty"] not in DIFFICULTIES:
        return "invalid_check_template"
    costs, success, failure = plan["costs"], plan["success"], plan["failure"]
    if type(costs) is not list or len(costs) > 2 or any(type(c) is not str for c in costs) \
            or len(set(costs)) != len(costs):
        return "invalid_costs"
    if not settling and any(not any(n == item and q > 0 for n, q in g.inventory.get(actor, [])) for item in costs):
        return "missing_resource"
    if type(success) is not list or not 1 <= len(success) <= 2 or type(failure) is not list or len(failure) > 1:
        return "invalid_effect_count"
    if sum(isinstance(e, dict) and e.get("kind") in ("damage", "heal", "reward", "scene") for e in success) > 1:
        return "effect_budget_exceeded"
    npcs = {n["name"] for n in g.scene_npcs()}
    for effect in success + failure:
        if type(effect) is not dict or type(effect.get("kind")) is not str or effect["kind"] not in EFFECTS:
            return "unknown_effect"
        kind = effect["kind"]
        if kind in ("transfer", "condition", "clear_condition"):
            return "effect_requires_attempt_review"
        if effect in failure and kind != "npc_reaction":
            return "failure_effect_not_allowed"
        keys = {"kind", "content"} if kind in ("scene", "prop") else {"kind", "catalog"} if kind == "reward" else {"kind", "target"}
        if set(effect) != keys:
            return "invalid_effect_fields"
        target = effect.get("target")
        if kind in ("damage", "advantage"):
            if type(target) is not str or target not in g.enemies or g.enemies[target].dead or g.enemies[target].hp <= 0:
                return "enemy_unavailable"
        elif kind in ("heal", "cover"):
            if type(target) is not str or target not in g.party or g.party[target].get("death", {}).get("dead"):
                return "party_target_unavailable"
            if kind == "heal" and HEALING_ITEM not in costs:
                return "healing_requires_potion"
        elif kind == "npc_reaction":
            if type(target) is not str or target not in npcs:
                return "npc_unavailable"
        elif kind in ("scene", "prop"):
            if kind == "scene" and g.combat.active:
                return "combat_requires_flee"
            reason = verify_proposal(g, DirectorProposal("scene" if kind == "scene" else "item",
                                                        g.world.current, effect["content"]))
            if reason:
                return reason
        elif kind == "reward":
            if type(effect["catalog"]) is not str or effect["catalog"] not in REWARD_CATALOG:
                return "unknown_reward"
            if g.combat.active or getattr(g, "_improv_rewards", {}).get(g.world.current):
                return "reward_allowance_exhausted"
    return None


def apply_effects(g, actor, plan, ok, lines):
    """Revalidate the approved plan when a pending check settles; apply once."""
    reason = verify_improvisation(g, plan, settling=True)
    if reason:
        lines.append("原先花招的條件已改變，額外效果未生效。")
        g.ledger.add(actor, "improvisation_cancelled", "花招條件已改變", reason=reason, plan_id=plan.get("id", ""))
        if plan.get("version") == 2 and g._creative_attempts.get(plan["id"]) == "pending":
            g._creative_attempts[plan["id"]] = "cancelled"
        return
    if plan.get("version") == 2 and ok:
        from .creative_effects import establish
        for fact in plan["setup"]:
            establish(g, plan, fact, lines)
    from .rules_core import _dispo_step, _on_foe_death
    for index, effect in enumerate(plan["success"] if ok else plan["failure"]):
        kind, target = effect["kind"], effect.get("target", "")
        if kind == "transfer":
            from .creative_effects import transfer
            transfer(g, actor, effect, lines)
        elif kind in ("condition", "clear_condition"):
            from .creative_effects import apply_condition
            apply_condition(g, plan, effect, index, lines)
        elif kind == "damage":
            foe = g.enemies[target]
            damage, _ = g.roll("1d4")
            foe.hp = max(0, foe.hp - damage)
            lines.append(f"💥 花招擊中 {target}，造成 {damage} 傷害。")
            g.ledger.add(actor, "damage", lines[-1], target=target, dmg=damage)
            if foe.hp == 0:
                foe.dead = True
                g.ledger.add(actor, "death", f"{target} 倒下了", target=target)
                _on_foe_death(g, foe, lines)
                g.check_end()
        elif kind == "heal":
            pc = g.party[target]
            amount, _ = g.roll("2d4+2")
            pc["hp_now"] = min(pc["hp_max"], pc["hp_now"] + amount)
            pc["death"] = {}
            lines.append(f"🩹 {target} 喝下藥水，恢復至 {pc['hp_now']} HP。")
            g.ledger.add(actor, "heal", lines[-1], target=target)
        elif kind in ("advantage", "cover"):
            field = "_aid" if kind == "advantage" else "_dodge"
            values = getattr(g, field, None) or {}
            values[target] = True
            setattr(g, field, values)
            lines.append(f"✨ {target}：" + ("下一次受到攻擊時，攻擊者有優勢。" if kind == "advantage" else "下次行動前，攻擊此角色有劣勢。"))
            g.ledger.add(actor, "tactic", lines[-1], target=target, effect=kind)
        elif kind == "npc_reaction":
            npc = next(n for n in g.scene_npcs() if n["name"] == target)
            npc["disposition"] = _dispo_step(npc.get("disposition", "neutral"), 1 if ok else -1)
            lines.append(f"🤝 {target} 的態度{'軟化了' if ok else '變得警惕'}。")
            g.ledger.add(actor, "talk", lines[-1], npc=target, disposition=npc["disposition"], ok=ok)
        elif kind == "prop":
            name = effect["content"]["name"].strip()
            if plan.get("version") == 2:
                from .creative_effects import establish, ordinary_template
                establish(g, plan, {"owner": "scene", "item": name, "template": ordinary_template(name), "basis": plan["method"]}, lines)
            else:
                g.world.here.ground_items.append((name, 1))
                lines.append(f"🎭 場景裡多了 **{name}**。")
                g.ledger.add(actor, "item", lines[-1], item=name)
        elif kind == "scene":
            canonize_scene(g, effect["content"])
            lines.append(f"🧭 新的路徑通往 {effect['content']['name']}；你仍在原地。")
        elif kind == "reward":
            name = REWARD_CATALOG[effect["catalog"]]
            g.give_item(actor, name, 1)
            if not hasattr(g, "_improv_rewards"):
                g._improv_rewards = {}
            g._improv_rewards[g.world.current] = True
            lines.append(f"🎒 找到可用的 **{name}**。")
            g.ledger.add(actor, "item", lines[-1], item=name, qty=1)
            if plan.get("version") == 2:
                from .creative_effects import record_provenance
                from engine.itemlib import affordances
                record_provenance(g, plan, actor, name, "reward:" + effect["catalog"], plan["method"], affordances(name), lines)
    g.ledger.add(actor, "improvisation", "花招成功。" if ok else "花招沒有奏效，還可以換個方法。",
                 ok=ok, method=plan["method"], plan_id=plan.get("id", ""))
    if plan.get("version") == 2:
        g._creative_attempts[plan["id"]] = "succeeded" if ok else "failed"


def resolve_improvisation(g, plan):
    from .rules_core import ResolveResult, ability_check, _post_rotation
    actor = plan["actor"]
    if reason := verify_improvisation(g, plan):
        return ResolveResult(["花招條件已改變：" + reason], accepted=False)
    if plan.get("version") == 2:
        g._creative_attempts[plan["id"]] = "pending"
        g.ledger.add(actor, "creative_attempt", f"{actor} 嘗試{plan['method']}；尚未取得物品或建立狀態。",
                     plan_id=plan["id"], scene=g.world.current, resolution=plan["resolution"])
    for item in plan["costs"]:
        g.take_item(actor, item, 1)
        g.ledger.add(actor, "item", f"{actor} 消耗了 {item}", item=item, qty=-1)
    in_combat = g.combat.active
    if plan.get("resolution") == "automatic":
        if any(r["kind"] == "help" for r in plan["prerequisites"]):
            g._help_check.pop(actor, None)
        lines = []
        apply_effects(g, actor, plan, True, lines)
        if in_combat:
            lines += _post_rotation(g, actor)
        return ResolveResult(lines)
    _, line = ability_check(g, actor, SKILL_ABILITY[plan["skill"]], DIFFICULTIES[plan["difficulty"]],
                            plan["skill"], auto=in_combat,
                            effect={"kind": "improvisation", "plan": plan})
    lines = [line]
    if g.combat.active:
        lines += _post_rotation(g, actor)
    return ResolveResult(lines)
