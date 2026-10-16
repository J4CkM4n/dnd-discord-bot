"""Complete object commands and unambiguous, state-owned item resolution."""
import re

from .intent import Intent, _action_text

_COMPLEX = re.compile(r"[?？;；]|如果|假如|不要|唔|別|别|不想|然後|然后|之後|之后|再去|並且|切開|切开|點燃|点燃|放火|混合|攻擊|攻击|演|威嚇|恐嚇|掟|投擲|包紮|救返|救醒|\b(?:if|not|then|and|except|but|attack|mix|burn)\b", re.I)
_OBJECT = re.compile(r"^(?P<verb>拿出|取出|攞出|take\s+out\b|檢查|检查|查看|端詳|inspect\b|examine\b|使用|用|use\b|給|给|交給|give\b)\s*(?P<tail>.*)$", re.I)
_QUANTITY = re.compile(r"^(.*?)\s*(?:[×x*]\s*|\s+)(-?\d+|null|None)\s*$", re.I)


class ObjectError(ValueError):
    pass


def resolve_name(value, names, kind="物品"):
    value = (value or "").strip(" 「」『』\"'")
    names = list(dict.fromkeys(names))
    matches = [value] if value in names else [n for n in names if value and value.casefold() in n.casefold()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ObjectError(f"「{value}」對應多個{kind}，請指定：{'、'.join(matches)}。")
    raise ObjectError(f"找不到{kind}「{value}」；請用完整名稱或查看 `/inventory`、`/scene`。")


def carried(g, actor, value):
    return resolve_name(value, [n for n, q in g.inventory.get(actor, []) if q > 0])


def visible_item(g, actor, value):
    return resolve_name(value, [n for n, q in g.inventory.get(actor, []) + list(g.world.here.ground_items) if q > 0])


def positive_quantity(qty):
    if type(qty) is not int or not 1 <= qty <= 20:
        raise ObjectError("數量必須是 1 至 20 的整數；尚未消耗或轉移物品。")
    return qty


def inventory_request(g, it):
    """Preflight the entire operation before dice, spending or a transfer."""
    positive_quantity(it.qty)
    name = carried(g, it.actor, it.item)
    if it.action == "give":
        target = it.args.get("to") or it.target
        targets = [resolve_name(target, g.party, "角色")] if target else []
        if not targets and not it.args.get("drop"):
            raise ObjectError("請指定要把物品交給誰；尚未轉移物品。")
    else:
        if it.args.get("to_all"):
            if not re.search(r"治療[藥药]水|healing potion", name, re.I):
                raise ObjectError("只有治療藥水可以指定全隊；其他物品請指定一個目標。")
            targets = [n for n, e in g.party.items() if not (e.get("death") or {}).get("dead")]
        else:
            target = it.args.get("to") or it.target
            targets = [resolve_name(target, g.party, "角色") if target else it.actor]
        if any((g.party[n].get("death") or {}).get("dead") for n in targets):
            raise ObjectError("目標已死亡，治療藥水救不回來；尚未消耗物品。")
    count = it.qty * max(1, len(targets))
    if g.combat.active and count > 1:
        raise ObjectError("戰鬥中一次行動只可使用或交付一份物品；請指定一位角色。")
    available = sum(q for n, q in g.inventory.get(it.actor, []) if n == name)
    if available < count:
        raise ObjectError(f"{name} 需要 {count} 份，目前只有 {available} 份；尚未消耗或轉移物品。")
    return name, targets


def parse_object_command(text, party_names=()):
    """No prefix execution: qualified, compound requests stay with the operator."""
    actor, action = _action_text(text, party_names)
    action = action.rstrip("。.!！")
    if _COMPLEX.search(action):
        return None
    # 「把石頭給依思」 and 「給依思石頭」 are transfers, never item use.
    action = re.sub(r"^(?:把|將)(.+?)(?:交給|給|给)(.+)$", r"給 \1 給 \2", action)
    hit = _OBJECT.fullmatch(action)
    if not hit:
        return None
    verb, tail = hit["verb"].lower(), hit["tail"].strip()
    if verb in ("拿出", "取出", "攞出") or verb.startswith("take"):
        return Intent("inspect", actor=actor, item=tail, raw=text, args={"take_out": True})
    if verb in ("檢查", "检查", "查看", "端詳", "inspect", "examine"):
        if re.search(r"屍體|尸体", tail):
            return None
        return Intent("inspect", actor=actor, item=tail, raw=text)
    giving = verb in ("給", "给", "交給", "give")
    it = Intent("give" if giving else "use", actor=actor, item=tail, raw=text)
    for name in sorted(party_names, key=len, reverse=True):
        if tail.endswith(name) and len(tail) > len(name):
            it.item = tail[:-len(name)].rstrip(" 給给餵喂對对:：")
            it.args["to"] = name
            break
        if giving and tail.startswith(name):
            it.item = tail[len(name):].lstrip(" ：:,，,")
            it.args["to"] = name
            break
    if not giving and not it.args.get("to"):
        all_targets = re.fullmatch(r"(.+?)(?:給|给|幫|帮|餵|喂|對|对|\s)*(?:所有人|大家|全隊|全队|每人|每個人|everyone|all)(?:喝|飲|治療|治疗|補血)?", tail, re.I)
        if all_targets:
            it.item = all_targets[1].strip()
            it.args["to_all"] = True
    quantity = _QUANTITY.fullmatch(it.item)
    if quantity:
        it.item = quantity[1].strip()
        raw = quantity[2]
        it.qty = int(raw) if re.fullmatch(r"-?\d+", raw) else None
    return it


def object_intent(g, text):
    it = parse_object_command(text, list(g.party))
    if it:
        if it.action == "use":
            from engine.charlib import normalize_skill
            if normalize_skill(it.item)[1]:
                return None
        return it
    # Observing a carried/visible object is a read, not a room search or tactic.
    from .intent import parse_command
    parsed = parse_command(text, list(g.party))
    if parsed and parsed.action == "observe" and not _COMPLEX.search(parsed.target):
        try:
            visible_item(g, parsed.actor, parsed.target)
        except ObjectError:
            # Caller identity may not yet be attached; include all owned items
            # only for classification. Execution still checks the actor's bag.
            if not any(parsed.target and parsed.target in n for stacks in g.inventory.values() for n, q in stacks if q > 0):
                return None
        return Intent("inspect", actor=parsed.actor, item=parsed.target, raw=text)
    return None
