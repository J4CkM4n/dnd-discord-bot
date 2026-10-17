"""Theft attempts transfer recorded possessions; prose cannot create loot."""
import re

from .intent import Intent, _action_text


class TheftError(ValueError):
    pass


# Full request grammars only: never execute the prefix of a qualified request.
_QUALIFIER = re.compile(
    r"不|唔|別|别|如果|假如|然後|然后|並|并|及|同時|同时|接著|接着|但是|但|[?？,，、;；\n]|"
    r"もし|なら|たら|ない|ずに|やめ|それから|その後|そして|"
    r"\b(?:if|would|could|not|don't|then|and)\b", re.I)
_GENERIC = {"", "物", "物品", "東西", "东西", "財物", "财物", "item", "something"}
_WEAPONS = {"武器", "weapon", "weapons"}


def parse_theft(text, party_names=()):
    actor, body = _action_text(text, party_names)
    body = body.strip().rstrip("。.!！")
    requires_help = False
    distraction = re.fullmatch(r"仲間が(?P<target>[^。！？]+?)の注意を引いている間に(?P<body>.+)", body)
    if distraction:
        body, requires_help = distraction["body"], True
    if _QUALIFIER.search(body):
        return None
    target = item = ""
    hit = re.fullmatch(r"(?P<target>[^。！？]+?)の(?P<item>[^。！？]+?)を(?:こっそり)?盗む", body)
    if not hit:
        hit = re.fullmatch(r"(?P<target>[^。！？]+?)から(?P<item>[^。！？]+?)を盗む", body)
    if hit:
        target, item = hit["target"], hit["item"]
    elif hit := re.fullmatch(r"steal\s+(?P<item>.+?)\s+from\s+(?P<target>.+)", body, re.I):
        target, item = hit["target"], hit["item"]
    elif hit := re.fullmatch(r"(?:偷偷扒竊|偷偷扒窃|扒竊|扒窃|偷竊|偷窃|偷取)\s*(?P<target>.+?)(?:的(?P<item>.+))?", body):
        target, item = hit["target"], hit["item"] or ""
    elif hit := re.fullmatch(r"偷\s*(?P<target>.+?)的(?P<item>.+)", body):
        target, item = hit["target"], hit["item"]
    else:
        return None
    if distraction and distraction["target"].strip() != target.strip():
        return None
    return Intent("steal", actor=actor, target=target.strip(), item=item.strip(), raw=text,
                  args={"requires_help": True} if requires_help else {})


def _match(value, names, label):
    names = list(dict.fromkeys(names))
    matches = [n for n in names if value == n]
    if not matches and value:
        matches = [n for n in names if value in n]
    if len(matches) != 1:
        detail = "多個符合的" if matches else "找不到"
        raise TheftError(f"{detail}{label}「{value}」；請指定精確名稱，尚未擲骰或取得物品。")
    return matches[0]


def possession(g, target, item):
    """Return the actual owner and one exact recorded item without mutation."""
    if type(target) is not str or type(item) is not str:
        raise TheftError("請指定明確的偷竊對象與物品名稱。")
    foes = g.scene_enemies()
    npcs = {n["name"]: n for n in g.scene_npcs()}
    name = _match(target, [*npcs, *foes], "偷竊對象")
    foe = foes.get(name)
    if foe and (foe.dead or foe.hp <= 0):
        raise TheftError(f"{name} 已倒下；請搜索屍體，不能再扒竊。")
    owner = foe if foe else npcs[name]
    loot = owner.loot if foe else owner.get("loot", [])
    # Carried possessions are unit entries, matching authored combat loot.
    names = list(dict.fromkeys(n for n in loot if type(n) is str and n))
    if not names:
        raise TheftError(f"{name} 沒有已記錄、可偷取的物品；尚未擲骰，也不會憑空產生武器。")
    if item.casefold() in _WEAPONS:
        from engine.moves import compute_attack_moves
        names = [n for n in names if compute_attack_moves("", 1, [("item", n, 1)])
                 or re.search(r"dagger|knife|sword|blade|axe|spear|bow", n, re.I)]
        request = ""
    else:
        request = "" if item.casefold() in _GENERIC else item
        # An indefinite narrated lot (e.g. 幾枚硬幣) is one recorded portion.
        # Exact inventory names still win, including names with a classifier.
        if request not in names:
            request = re.sub(r"^(?:一|1|幾|几|數|数)\s*(?:枚|把|個|件|顆|張|條)\s*", "", request)
    if not request:
        if len(names) != 1:
            raise TheftError(f"請指定{name}身上的一件實際物品：{'、'.join(names) or '沒有可偷取的武器'}。尚未擲骰。")
        chosen = names[0]
    else:
        chosen = _match(request, names, "可偷取物品")
    return name, chosen, owner


def preflight(g, it):
    if type(it.qty) is not int or it.qty != 1:
        raise TheftError("一次只能嘗試偷取一件物品。")
    result = possession(g, it.target, it.item)
    if it.args.get("requires_help"):
        help_event = next((e for e in reversed(g.ledger.entries)
                           if e.kind == "help" and e.data.get("target") == it.actor), None)
        if not (getattr(g, "_help_check", None) or {}).get(it.actor) or not help_event \
                or help_event.actor == it.actor or help_event.actor not in g.party \
                or not g.alive(help_event.actor) or help_event.data.get("scene") != g.world.current:
            raise TheftError("這次偷竊要等同伴分散注意力；請同伴先用 `/help` 協助你，再重試。尚未擲骰，也未替同伴行動。")
    return result


def settle(g, actor, effect, ok, lines):
    target, item = effect["target"], effect["item"]
    outcome = "failed"
    reason = "檢定失敗"
    if ok:
        try:
            if effect["scene"] != g.world.current or actor not in g.party or not g.alive(actor):
                raise TheftError("偷竊條件已改變")
            name, chosen, owner = possession(g, target, item)
            if (name, chosen) != (target, item):
                raise TheftError("原先的對象或物品已不在場")
        except TheftError as exc:
            outcome, reason = "cancelled", str(exc)
        else:
            if isinstance(owner, dict):
                owner["loot_fixed"] = True
                owner["loot"].remove(item)
            else:
                owner.loot_fixed = True
                owner.loot.remove(item)
            g.give_item(actor, item)
            outcome = "transferred"
    text = (f"{actor} 從 {target} 身上偷得 {item}×1，物品已放入 {actor} 的背包；沒有造成傷害。"
            if outcome == "transferred" else
            f"{actor} 未能從 {target} 身上取得 {item}（{reason}）；沒有物品轉移，也沒有造成傷害。")
    lines.append("🎒 " + text)
    g.ledger.add(actor, "theft", text, target=target, item=item, qty=1 if outcome == "transferred" else 0,
                 recipient=actor, outcome=outcome, scene=effect["scene"], damage=0)
