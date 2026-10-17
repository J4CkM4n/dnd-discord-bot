"""Bounded possessions and conditions for reviewed creative attempts.

Models select templates and describe fiction. Custody, item properties, modifiers,
expiry and all mutations belong to this module and the ordinary rules engine.
"""
import re
from copy import deepcopy

from .world import scene_key

# Each template occupies one durable establishment slot per original owner/scene.
# A different spelling of a removed shirt cannot create another shirt.
ORDINARY_ITEMS = {
    "clothing": {"names": r"衣|衫|褲|袍|帽|服|外套|背心|披風|披风|斗篷|シャツ|上着|ズボン|帽子|\b(?:shirts?|coats?|trousers|hats?|cloaks?|jackets?|pants|clothes|clothing)\b",
                 "properties": ["wearable", "flammable", "absorbent", "throwable"]},
    "cloth": {"names": r"布|帆|rag|cloth|canvas", "properties": ["flammable", "absorbent", "throwable"]},
    "cord": {"names": r"繩|绳|縄|紐|rope|cord|string", "properties": ["flammable", "throwable", "binding"]},
    "debris": {"names": r"石|木|枝|板|砂|沙|stone|rock|wood|plank|sand", "properties": ["throwable"]},
    "container": {"names": r"瓶|袋|桶|箱|bottle|bag|bucket|box", "properties": ["container", "throwable"]},
    "knife": {"names": r"^匕首$", "properties": ["throwable", "pry"]},
    "prop": {"names": r".+", "properties": ["throwable"]},
}
_EXTRAORDINARY = re.compile(
    r"魔|神|聖|圣|秘|寶|宝|金|銀|银|珍|鑽|钻|珠|藥|药|毒|爆|火藥|火药|"
    r"鍵|鑰|鎖匙|鍵|呪|伝説|伝承|貴|レア|ポーション|"
    r"magic|enchanted|holy|rare|legendary|treasure|gold|silver|gem|jewel|potion|poison|explosive|bomb|key|adamant|mithr", re.I)

CONDITION_TEMPLATES = {
    "restrained": {"restrictions": ["move", "escape"], "modifiers": ["attack_disadvantage", "attacks_against_advantage"]},
    "bound": {"restrictions": ["attack", "cast", "use", "give", "steal", "take"], "modifiers": ["attacks_against_advantage"]},
    "distracted": {"restrictions": [], "modifiers": ["check_disadvantage", "attack_disadvantage"]},
    "cover": {"restrictions": [], "modifiers": ["attacks_against_disadvantage"]},
}
EXPIRIES = ("next_action", "scene_exit", "end_combat", "released")


def _text(value, limit=240):
    return type(value) is str and bool(value.strip()) and len(value) <= limit


def ordinary_template(item):
    return next((name for name in ("knife", "clothing", "container", "cord", "cloth", "debris")
                 if re.search(ORDINARY_ITEMS[name]["names"], item, re.I)), "prop")


def owner_state(g, owner):
    """Exact current identity and life state. Never fuzzy-select another owner."""
    if owner == "scene":
        return "scene"
    if owner in g.party:
        return "party" if g.alive(owner) else "downed"
    foe = g.scene_enemies().get(owner)
    if foe:
        return "corpse" if foe.dead or foe.hp <= 0 else "enemy"
    if any(n["name"] == owner for n in g.scene_npcs()):
        return "npc"
    return None


def _owner(g, owner):
    foe = g.scene_enemies().get(owner)
    return foe if foe else next((n for n in g.scene_npcs() if n["name"] == owner), None)


def _field(owner, name, default=None):
    return owner.get(name, default) if isinstance(owner, dict) else getattr(owner, name, default)


def item_count(g, owner, item):
    state = owner_state(g, owner)
    if state == "scene":
        return sum(q for n, q in g.world.here.ground_items if n == item)
    if state in ("party", "downed"):
        return sum(q for n, q in g.inventory.get(owner, []) if n == item)
    if state == "corpse":
        # Death loot already moved to the ground; Enemy.loot is historical.
        return g._corpse_possessions.get(g.world.current, {}).get(owner, []).count(item)
    return (_field(_owner(g, owner), "loot", []) or []).count(item) if state else 0


def _absence(g, owner, item, template):
    obj = g.world.here if owner == "scene" else g.party.get(owner) or _owner(g, owner)
    metadata = _field(obj, "npc", {}) or {}
    absent = list(_field(obj, "absent_items", []) or []) + list(metadata.get("absent_items", []))
    if any(scene_key(n) == scene_key(item) for n in absent):
        return "established_absence"
    if _field(obj, "possessions_complete", False) or metadata.get("possessions_complete"):
        return "established_absence"
    if template == "knife" and (_field(obj, "loot_fixed", False) or metadata.get("loot_fixed")):
        return "established_absence"
    def same_possession(name):
        if template == "prop":
            return bool(scene_key(name)) and (scene_key(name) in scene_key(item) or scene_key(item) in scene_key(name))
        return re.search(ORDINARY_ITEMS[template]["names"], name, re.I)
    if owner_state(g, owner) == "corpse" and any(same_possession(n) for n in _field(obj, "loot", [])):
        return "possession_previously_removed"
    for record in g._creative_provenance:
        if record["owner"] == owner and (owner != "scene" or record["scene"] == g.world.current) \
                and (record["template"] == template or scene_key(record["item"]) == scene_key(item)):
            return "possession_already_established"
    for event in g.ledger.entries:
        if event.kind in ("theft", "item_transfer") and event.data.get("target", event.data.get("owner")) == owner \
                and event.data.get("outcome") == "transferred" \
                and scene_key(event.data.get("item", "")) == scene_key(item):
            return "possession_previously_removed"
        if owner == "scene" and event.kind == "item" and event.data.get("source") == "ground" \
                and event.data.get("scene") == g.world.current \
                and same_possession(event.data.get("item", "")):
            return "possession_previously_removed"
        if owner in g.party and event.actor == owner and event.kind == "item" \
                and event.data.get("item") == item:
            return "possession_previously_recorded"
    return None


def verify_setup(g, fact, actor):
    if type(fact) is not dict or set(fact) != {"owner", "item", "template", "basis"}:
        return "invalid_possession_fields"
    owner, item, template = fact["owner"], fact["item"], fact["template"]
    if not _text(owner, 100) or not _text(item, 80) or not _text(fact["basis"]):
        return "invalid_possession_detail"
    if type(template) is not str or template not in ORDINARY_ITEMS \
            or _EXTRAORDINARY.search(item) or not re.search(ORDINARY_ITEMS[template]["names"], item, re.I):
        return "ordinary_item_not_supported"
    from .director import _BLOCKED, _NON_PROP
    canonical = ordinary_template(item)
    blocked_name = re.sub(ORDINARY_ITEMS["cord"]["names"], "", item, flags=re.I) if template == "cord" else item
    # Retain the semantic prop exclusions, without the old ten-character
    # Chinese-name limit: ordinary English/Japanese names use this schema's cap.
    if template != canonical or _NON_PROP.search(item) \
            or (template != "knife" and _BLOCKED.search(blocked_name)):
        return "ordinary_item_not_supported"
    if owner in g.party and owner != actor:
        return "other_player_possessions"
    if not owner_state(g, owner):
        return "owner_unavailable"
    if reason := _absence(g, owner, item, template):
        return reason
    # Reuse exact/typographic identities anywhere already tracked in the world.
    names = [n for stacks in g.inventory.values() for n, q in stacks if q > 0]
    names += [n for s in g.world.scenes.values() for n, q in s.ground_items + s.hidden_items if q > 0]
    names += [n for sid in g.world.scenes for f in g.scene_enemies(sid).values() if not f.dead and f.hp > 0 for n in f.loot]
    names += [n for sid in g.world.scenes for npc in g.scene_npcs(sid) for n in npc.get("loot", [])]
    names += [n for stacks in g._corpse_possessions.values() for items in stacks.values() for n in items]
    if any(scene_key(n) == scene_key(item) for n in names):
        return "reuse_existing_possession"
    return None


def has_help(g, actor, helper):
    if helper == actor or helper not in g.party or not g.alive(helper) \
            or not (getattr(g, "_help_check", None) or {}).get(actor):
        return False
    event = next((e for e in reversed(g.ledger.entries) if e.kind == "help" and e.data.get("target") == actor), None)
    return bool(event and event.actor == helper and event.data.get("scene") == g.world.current)


def verify_prerequisites(g, plan, *, settling=False):
    for req in plan["prerequisites"]:
        if type(req) is not dict or type(req.get("kind")) is not str:
            return "invalid_prerequisite"
        if req["kind"] == "possession" and set(req) == {"kind", "owner", "item"}:
            if not _text(req["owner"], 100) or not _text(req["item"], 80):
                return "invalid_prerequisite"
            virtual = any(f["owner"] == req["owner"] and f["item"] == req["item"] for f in plan["setup"])
            if settling and req["owner"] == plan["actor"] and req["item"] in plan["costs"]:
                continue  # the approved cost was paid before the saved check
            if not item_count(g, req["owner"], req["item"]) and not virtual:
                return "prerequisite_possession_missing"
        elif req["kind"] == "help" and set(req) == {"kind", "helper"} and _text(req["helper"], 100):
            if not settling and not has_help(g, plan["actor"], req["helper"]):
                return "prerequisite_help_missing"
        else:
            return "invalid_prerequisite"
    return None


def verify_effect(g, effect, plan, *, failure=False):
    actor, kind = plan["actor"], effect.get("kind")
    if kind == "transfer":
        if failure or set(effect) != {"kind", "owner", "item", "recipient"}:
            return "invalid_transfer_fields"
        owner, item, recipient = effect["owner"], effect["item"], effect["recipient"]
        if not _text(owner, 100) or not _text(item, 80) or type(recipient) is not str:
            return "invalid_transfer_fields"
        if recipient not in (actor, "scene") or owner == recipient or (owner in g.party and owner != actor):
            return "unauthorized_transfer"
        if restriction(g, actor, "give" if owner == actor else "take"):
            return "condition_restricts_transfer"
        virtual = any(f["owner"] == owner and f["item"] == item for f in plan["setup"])
        if not item_count(g, owner, item) and not virtual:
            return "transfer_possession_missing"
        if plan["resolution"] == "automatic" and owner_state(g, owner) in ("npc", "enemy"):
            return "opposed_transfer_requires_check"
        if owner == actor and item in plan["costs"]:
            return "resource_already_consumed"
    elif kind == "condition":
        if set(effect) != {"kind", "target", "template", "description", "expires", "removal"}:
            return "invalid_condition_fields"
        target, template = effect["target"], effect["template"]
        if type(target) is not str or (target in g.party and target != actor):
            return "other_player_condition"
        if owner_state(g, target) not in ("party", "npc", "enemy"):
            return "condition_subject_unavailable"
        if type(template) is not str or template not in CONDITION_TEMPLATES \
                or type(effect["expires"]) is not str or effect["expires"] not in EXPIRIES \
                or not _text(effect["description"]) or not _text(effect["removal"]):
            return "unsupported_condition"
        if failure and target != actor:
            return "failure_condition_must_affect_actor"
        if plan["resolution"] == "automatic" and target != actor:
            return "opposed_condition_requires_check"
    elif kind == "clear_condition":
        if failure or set(effect) != {"kind", "condition_id"} or type(effect["condition_id"]) is not str:
            return "invalid_condition_fields"
        condition = g._creative_conditions.get(effect["condition_id"])
        if not condition or (condition["scene"] != g.world.current and condition["subject"] != actor):
            return "condition_unavailable"
        if condition["subject"] in g.party and condition["subject"] != actor:
            return "other_player_condition"
    return None


def establish(g, plan, fact, lines):
    owner, item = fact["owner"], fact["item"]
    if owner == "scene":
        g.world.here.ground_items.append((item, 1))
    elif owner in g.party:
        g.give_item(owner, item)
    elif owner_state(g, owner) == "corpse":
        g._corpse_possessions.setdefault(g.world.current, {}).setdefault(owner, []).append(item)
    else:
        obj = _owner(g, owner)
        if isinstance(obj, dict):
            obj.setdefault("loot", []).append(item)
        else:
            obj.loot.append(item)
    record_provenance(g, plan, owner, item, fact["template"], fact["basis"],
                      ORDINARY_ITEMS[fact["template"]]["properties"], lines)


def record_provenance(g, plan, owner, item, template, basis, properties, lines):
    record = {"plan_id": plan["id"], "scene": g.world.current, "owner": owner, "item": item,
              "template": template, "properties": sorted(properties), "basis": basis,
              "source_revision": plan["source_revision"]}
    event = g.ledger.add(plan["actor"], "possession_established", f"確認 {owner} 處的一件普通物品：{item}。", **record)
    record["event_id"] = event.event_id
    g._creative_provenance.append(record)
    lines.append("📌 " + event.text)


def transfer(g, actor, effect, lines):
    owner, item, recipient = effect["owner"], effect["item"], effect["recipient"]
    if owner == "scene":
        stacks = g.world.here.ground_items
        idx = next(i for i, (n, q) in enumerate(stacks) if n == item and q > 0)
        n, q = stacks[idx]
        if q == 1:
            stacks.pop(idx)
        else:
            stacks[idx] = (n, q - 1)
    elif owner in g.party:
        if not g.take_item(owner, item):
            raise ValueError("Transfer source changed after validation")
    elif owner_state(g, owner) == "corpse":
        g._corpse_possessions[g.world.current][owner].remove(item)
    else:
        obj = _owner(g, owner)
        _field(obj, "loot").remove(item)
        if isinstance(obj, dict):
            obj["loot_fixed"] = True
        else:
            obj.loot_fixed = True
    if recipient == "scene":
        g.world.here.ground_items.append((item, 1))
    else:
        g.give_item(recipient, item)
    text = f"{actor} 將 {owner} 處的 {item}×1 移至 {'地上' if recipient == 'scene' else recipient + ' 的背包'}。"
    g.ledger.add(actor, "item_transfer", text, owner=owner, target=owner, recipient=recipient,
                 item=item, qty=1, outcome="transferred", scene=g.world.current)
    lines.append("🎒 " + text)


def apply_condition(g, plan, effect, index, lines):
    if effect["kind"] == "clear_condition":
        condition = g._creative_conditions.pop(effect["condition_id"])
        text = f"{condition['subject']} 的狀態解除：{condition['description']}"
    else:
        identifier = f"{plan['id']}:{index}"
        condition = {"id": identifier, "subject": effect["target"], "template": effect["template"],
                     "description": effect["description"], "expires": effect["expires"], "removal": effect["removal"],
                     "scene": g.world.current, "source": plan["actor"], "created_turn": g.ledger.turn,
                     **deepcopy(CONDITION_TEMPLATES[effect["template"]])}
        g._creative_conditions[identifier] = condition
        text = f"{condition['subject']}：{condition['description']}；解除：{condition['removal']}。"
    g.ledger.add(plan["actor"], "condition", text, condition=condition, scene=g.world.current)
    lines.append("✨ " + text)


def active_conditions(g, subject=None):
    return [c for c in g._creative_conditions.values()
            if (c["scene"] == g.world.current or c["subject"] in g.party)
            and (subject is None or c["subject"] == subject)]


def modifier(g, subject, name):
    return any(name in c["modifiers"] for c in active_conditions(g, subject))


def item_properties(g, item):
    from engine.itemlib import affordances
    properties = affordances(item)
    for record in g._creative_provenance:
        if record["item"] == item:
            properties.update(record["properties"])
    return properties


def restriction(g, subject, action):
    condition = next((c for c in active_conditions(g, subject) if action in c["restrictions"]), None)
    return f"{subject}：{condition['description']}；{condition['removal']}。" if condition else None


def expire_conditions(g, *, acted="", left_scene="", combat_ended=False, existing=None):
    lines = []
    for identifier, condition in list(g._creative_conditions.items()):
        expire = (condition["expires"] == "next_action" and condition["subject"] == acted
                  and (existing is None or identifier in existing))
        expire |= bool(left_scene and condition["scene"] == left_scene and condition["expires"] != "released")
        expire |= combat_ended and condition["expires"] == "end_combat"
        if expire:
            del g._creative_conditions[identifier]
            text = f"{condition['subject']} 的狀態結束：{condition['description']}"
            g.ledger.add("engine", "condition_expired", text, condition_id=identifier)
            lines.append(text)
    return lines
