"""Engine-owned vocabulary for improvised actions. No model or transport access."""
from dataclasses import asdict, dataclass

from engine.charlib import SKILL_ABILITY
from .director import DirectorProposal, canonize_scene, verify_proposal

DIFFICULTIES = {"easy": 10, "moderate": 14, "hard": 18}
REWARD_CATALOG = {"salvage": "繩索"}  # one mundane reward per scene, for the whole party
HEALING_ITEM = "治療藥水"
EFFECTS = ("damage", "heal", "advantage", "cover", "npc_reaction", "prop", "scene", "reward")


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
    if type(raw) is not dict or set(raw) != {"method", "skill", "difficulty", "costs", "success", "failure"}:
        raise ValueError("Invalid improvisation fields")
    proposal = asdict(ImprovisationProposal(revision, g.world.current, actor, **raw))
    reason = verify_improvisation(g, proposal, revision=revision)
    if reason:
        raise ValueError(reason)
    return proposal


def verify_improvisation(g, plan, *, revision=None, settling=False):
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
        g.ledger.add(actor, "improvisation_cancelled", "花招條件已改變", reason=reason)
        return
    from .rules_core import _dispo_step, _on_foe_death
    for effect in plan["success"] if ok else plan["failure"]:
        kind, target = effect["kind"], effect.get("target", "")
        if kind == "damage":
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
    g.ledger.add(actor, "improvisation", "花招成功。" if ok else "花招沒有奏效，還可以換個方法。",
                 ok=ok, method=plan["method"])


def resolve_improvisation(g, plan):
    from .rules_core import ResolveResult, ability_check, _post_rotation
    actor = plan["actor"]
    for item in plan["costs"]:
        g.take_item(actor, item, 1)
        g.ledger.add(actor, "item", f"{actor} 消耗了 {item}", item=item, qty=-1)
    in_combat = g.combat.active
    _, line = ability_check(g, actor, SKILL_ABILITY[plan["skill"]], DIFFICULTIES[plan["difficulty"]],
                            plan["skill"], auto=in_combat,
                            effect={"kind": "improvisation", "plan": plan})
    lines = [line]
    if g.combat.active:
        lines += _post_rotation(g, actor)
    return ResolveResult(lines)
