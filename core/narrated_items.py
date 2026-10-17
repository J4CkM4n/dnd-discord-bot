"""Persist tangible details introduced by narration before publishing its prose."""

import json
import re
from copy import deepcopy

from .world import scene_key


class NarrationText(str):
    def __new__(cls, text, items=None):
        result = super().__new__(cls, text)
        result.items = items
        return result


ITEM_INSTRUCTIONS = """
輸出先寫一行 JSON：{"items":[{"owner":"scene 或當前 NPC/敵人的精確名稱","name":"物品精確名稱","qty":1,"evidence":"下方敘事中確認物品存在及所在位置的原文"}]}
JSON 後換行，再寫玩家閱讀的敘事；沒有新物品時 items=[]。
可以補充現場實體物品，包括硬幣、鑰匙與普通道具；每件新增物品都必須列入 items，
放在場景地面用 owner=scene，放在人物身上用其精確名稱。保留現有物品的名稱、數量及持有人。
具體數量用整數，數量不明（例如幾枚）用 qty=1 表示一份，不猜測數量。
只記錄敘事肯定在場的實物，不把比喻、否定、願望、傳聞、對白的索求或假設列為物品。
不能替玩家取得物品、複製已移走的物品或創造新機械效果；新鑰匙不自動開啟任何鎖。
evidence 必須逐字出現在敘事中，並交代物品的位置／持有人。每段最多新增八種物品。
"""


def parse_narration(raw):
    """A small JSON header leaves prose streamable even when its tail is cut off.

    Older/plain-text providers remain compatible; common currency/key details
    are recovered separately rather than depending on Markdown emphasis.
    """
    raw = raw.strip()
    if not raw.startswith(("{", "```")):
        return NarrationText(raw)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
    try:
        header, end = json.JSONDecoder().raw_decode(raw)
        if type(header) is not dict or set(header) != {"items"} or type(header["items"]) is not list:
            return NarrationText("", [])
        prose = raw[end:].lstrip()
        if prose.startswith("```"):
            prose = prose[3:].lstrip()
        return NarrationText(prose, header["items"])
    except (ValueError, TypeError):
        return NarrationText("", [])


_COIN_KEY = re.compile(
    r"(?:(?P<count>[一二三四五六七八九十百千萬零兩幾數\d]+)\s*(?:枚|把|串|個)\s*)?"
    r"(?P<name>(?:(?:生鏽|鏽蝕|銹蝕|黃銅|鐵製|古老|破舊)的?)?"
    r"(?:硬幣|金幣|銀幣|銅幣|錢幣|鑰匙|鎖匙))")
_NEGATED = re.compile(r"沒有|並無|並非|不存在|未發現|沒找到|不見|找不到|想要|想像|希望|如果|假如|也許|可能|聽說|據說|需要|索取|要求")
_PRESENT = re.compile(r"有|放著|擺著|躺著|散落|掛著|拿著|握著|帶著|攜帶|露出|藏著|掏出|摸出|除了")
_CUSTODY = re.compile(r"身上|腰間|腰際|口袋|懷裡|手中|手上|掌心|掛著|拿著|握著|攜帶|帶著|掏出|摸出|藏著")
_GROUND = re.compile(r"地上|地面|桌上|桌面|散落|躺著|掉在")


def _quantity(count):
    if not count or count in ("幾", "數"):
        return 1
    if count.isdecimal():
        return int(count)
    digits = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "零": 0}
    total = current = 0
    for char in count:
        if char in digits:
            current = digits[char]
        elif char in "十百千萬":
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000, "萬": 10000}[char]
            current = 0
    return total + current


def legacy_items(g, text):
    """Recover plain-text coin/key assertions, including cross-sentence custody."""
    result = []
    owners = [*g.party, *(n["name"] for n in g.scene_npcs()), *g.scene_enemies()]
    for match in _COIN_KEY.finditer(text):
        # A request/negation is not an assertion. The 'except for' wording in
        # the reported empty-pockets narration still positively asserts coins.
        clause = re.split(r"[。！？!?，,；;\n]", text[:match.start()])[-1]
        if _NEGATED.search(clause) or re.match(r"(?:般|似|一樣|也沒有|都沒有|不在)", text[match.end():]):
            continue
        start = max(0, match.start() - 180)
        preceding = text[start:match.start()]
        mentions = [(preceding.rfind(name), name) for name in owners if name in preceding]
        owner = max(mentions)[1] if mentions else "scene"
        if mentions:
            start += max(mentions)[0]
        prefix = text[start:match.start()]
        if not _PRESENT.search(clause) and not (_CUSTODY.search(prefix) and match["count"]):
            continue
        if _GROUND.search(clause):
            owner = "scene"
        elif not _CUSTODY.search(prefix):
            owner = "scene"
        result.append({"owner": owner, "name": match["name"], "qty": _quantity(match["count"]),
                       "evidence": text[start:match.end()]})
    return result


def prepare_narrated_items(g, text, items, restore):
    """Restore placeholders and recover omitted plain-text currency/key details."""
    if items is None:
        return legacy_items(g, text)
    items = deepcopy(items)
    if type(items) is not list:
        return items
    for item in items:
        if isinstance(item, dict):
            for key in ("owner", "name", "evidence"):
                if isinstance(item.get(key), str):
                    item[key] = restore(item[key])
    for inferred in legacy_items(g, text):
        if not any(isinstance(item, dict) and isinstance(item.get("name"), str)
                   and _same(inferred["name"], item["name"]) for item in items):
            items.append(inferred)
    return items


def _same(a, b):
    a, b = scene_key(a), scene_key(b)
    return bool(a and b) and (a == b or a in b or b in a)


def _owner(g, name):
    if name == "scene":
        return g.world.here
    foe = g.scene_enemies().get(name)
    if foe:
        return foe if not foe.dead and foe.hp > 0 else None
    return next((n for n in g.scene_npcs() if n["name"] == name), None)


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _names(g, owner):
    if owner == "scene":
        return [n for n, qty in g.world.here.ground_items if qty > 0]
    obj = _owner(g, owner)
    return _field(obj, "loot", []) or []


def public_narrated_items(g):
    """Only previously published possessions still held by that exact owner."""
    result = {}
    for event in g.ledger.entries:
        if event.kind != "narration_item":
            continue
        owner, name = event.data["owner"], event.data["item"]
        if owner == "scene" and event.data["scene"] != g.world.current:
            continue
        if name in _names(g, owner) and name not in result.setdefault(owner, []):
            result[owner].append(name)
    return result


def repair_prior_narration(g):
    """Recover recent coin/key assertions from this channel's saved DM prose.

    This makes a previously displayed pocket description usable after upgrading,
    without interpreting player messages or rewriting historical events.
    """
    recent = [e for e in g.ledger.entries if e.kind == "narration"
              and e.data.get("scene") == g.world.current
              and g.ledger.turn - e.turn <= 5][-3:]
    added = 0
    for event in recent:
        try:
            start = len(g.ledger.entries)
            added += canonize_narrated_items(g, event.text, legacy_items(g, event.text))
            for record in g.ledger.entries[start:]:
                record.data["source_event_id"] = event.event_id
        except ValueError:
            continue
    return added


def canonize_narrated_items(g, text, items):
    """Validate the whole batch before adding possessions; never grant PC loot.

    Narration can establish physical currency/keys without the older inert-prop
    restriction. Existing absence, ownership and removal history remain binding.
    """
    if type(items) is not list or len(items) > 8:
        raise ValueError("invalid_narrated_items")
    pending = []
    plain = text.replace("**", "")
    for item in items:
        if type(item) is not dict or set(item) != {"owner", "name", "qty", "evidence"}:
            raise ValueError("invalid_narrated_item")
        owner, name, qty, evidence = (item[k] for k in ("owner", "name", "qty", "evidence"))
        if any(type(v) is not str or not v.strip() for v in (owner, name, evidence)) \
                or len(name) > 80 or len(owner) > 100 or len(evidence) > 600 \
                or type(qty) is not int or not 1 <= qty <= 999 \
                or name not in evidence or evidence.replace("**", "") not in plain \
                or any(c in name for c in "\r\n\x00"):
            raise ValueError("invalid_narrated_evidence")
        if owner != "scene" and owner not in evidence:
            raise ValueError("narrated_owner_not_asserted")
        before_name = re.split(r"[。！？!?，,；;\n]", evidence.rsplit(name, 1)[0])[-1]
        after_name = evidence.rsplit(name, 1)[-1]
        if _NEGATED.search(before_name) or re.match(r"(?:般|似|一樣|也沒有|都沒有|不在)", after_name):
            raise ValueError("narrated_item_not_asserted")
        # Existing PC equipment may be mentioned, but prose never gives a PC
        # equipment or resolves an acquisition on their behalf.
        if owner in g.party:
            if any(_same(name, n) for n, q in g.inventory.get(owner, []) if q > 0):
                continue
            raise ValueError("narrated_player_acquisition")
        obj = _owner(g, owner)
        if obj is None:
            raise ValueError("narrated_owner_not_present")
        if any(_same(name, n) for n in _names(g, owner)):
            continue
        if any(owner == old_owner and _same(name, old_name)
               for old_owner, _, old_name, _ in pending):
            continue
        metadata = _field(obj, "npc", {}) or {}
        absent = (_field(obj, "absent_items", []) or []) + metadata.get("absent_items", [])
        if _field(obj, "possessions_complete", False) or metadata.get("possessions_complete") \
                or any(_same(name, n) for n in absent) \
                or any(_same(name, n) for n, _ in g.world.here.hidden_items):
            raise ValueError("narrated_item_conflicts_with_canon")
        if any(record["owner"] == owner and (owner != "scene" or record["scene"] == g.world.current)
               and _same(name, record["item"]) for record in g._creative_provenance):
            raise ValueError("narrated_item_previously_established")
        for event in g.ledger.entries:
            if event.kind == "narration_item" and event.data.get("owner") == owner \
                    and (owner != "scene" or event.data.get("scene") == g.world.current) \
                    and _same(name, event.data["item"]):
                raise ValueError("narrated_item_previously_removed")
            if owner == "scene" and event.kind == "item" and event.data.get("source") == "ground" \
                    and event.data.get("scene") == g.world.current \
                    and _same(name, event.data.get("item", "")):
                raise ValueError("narrated_item_previously_removed")
            if event.kind in ("theft", "item_transfer") and event.data.get("qty", 0) > 0 \
                    and event.data.get("target", event.data.get("owner")) == owner \
                    and _same(name, event.data.get("item", "")):
                raise ValueError("narrated_item_previously_removed")
        pending.append((owner, obj, name, qty))
    for owner, obj, name, qty in pending:
        if owner == "scene":
            obj.ground_items.append((name, qty))
        elif isinstance(obj, dict):
            obj.setdefault("loot", []).extend([name] * qty)
            obj["loot_fixed"] = True
        else:
            obj.loot.extend([name] * qty)
            obj.loot_fixed = True
        g.ledger.add("narrator", "narration_item", f"{owner} 處的 {name}×{qty} 已記入物品清單。",
                     owner=owner, item=name, qty=qty, scene=g.world.current)
    return len(pending)
