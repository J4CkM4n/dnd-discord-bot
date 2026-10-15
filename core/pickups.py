"""Deterministic collection and corpse-search syntax; engine-only preflight."""
from collections import Counter
import re

from .intent import Intent, _action_text

MAX_ITEMS = 20
_VERB = re.compile(r"^(?:撿起|捡起|拾起|執起|拿起|攞起|撿|捡|拾|執|攞|拿|pick\s+up\b|take\b)\s*(.+)$", re.I)
_GROUND = re.compile(r"^(?:(?:地上面?|地下|地面上?|眼前)(?:的|嘅)?|(?:the\s+)?(?:items?\s+)?on\s+the\s+ground\s*)\s*", re.I)
_ALL = re.compile(r"(?:全部|所有|一切)(?:散落(?:的|嘅)?)?(?:物品|東西|东西|物資|物资|道具)?|everything|all(?:\s+(?:the\s+)?(?:scattered\s+)?(?:items?|loot|things?))?", re.I)
_SEPARATOR = re.compile(r"\s*(?:[、，,]|以及|同埋|和|與|及|\band\b)\s*", re.I)
# Full requests with another action, questions, alternatives or restrictions
# must still go through semantic interpretation. Never execute their prefix.
_COMPLEX = re.compile(
    r"[?？。；;!！]|如果|假如|唔|不要|別|别|不|未|或|除了|除咗|除外|以外|但|"
    r"然後|然后|之後|之后|接著|接着|再|攻擊|攻击|搜索|搜尋|使用|喝|吃|"
    r"混合|加入|製作|制作|切開|切开|前往|放火|點燃|点燃|給|给|"
    r"\b(?:if|not|don't|except|but|or|then|attack|search|use|drink|mix|give|go)\b", re.I)


class PickupError(ValueError):
    pass


def selection(value):
    """Return None for all, or normalized item names; never invent quantities."""
    value = _GROUND.sub("", (value or "").strip()).strip()
    if _ALL.fullmatch(value):
        return None
    if not value or len(value) > 1500 or _COMPLEX.search(value):
        raise PickupError("請指定地上的物品，或說「拾起地上所有物品」；其他行動請分開提出。")
    names = [_GROUND.sub("", n).strip(" 「」『』\"'") for n in _SEPARATOR.split(value)]
    if any(not n or len(n) > 100 or _ALL.fullmatch(n) for n in names) or len(names) > MAX_ITEMS:
        raise PickupError(f"一次最多拾取 {MAX_ITEMS} 種明確物品；請列出名稱或指定全部。")
    return list(dict.fromkeys(names))


def parse_pickup(text, party_names=()):
    """Recognize only an entire, unqualified collection request."""
    actor, action = _action_text(text, party_names)
    match = _VERB.fullmatch(action.rstrip("。.!！"))
    if not match:
        return None
    try:
        names = selection(match[1])
    except PickupError:
        return None
    return Intent("take", actor=actor, item="所有物品" if names is None else "、".join(names), raw=text)


_CORPSE = r"(?P<body>[^，。！？,;；]+?(?:屍體|尸体))"
_SEARCH = r"(?:搜索|搜尋|搜查|檢查|检查|搜身)"
_LOOT = r"(?:有用(?:的|嘅)?(?:物品|物資|東西)|可用(?:的|嘅)?物品|戰利品|战利品|物品)?"
_CORPSE_PATTERNS = (
    re.compile(r"^(?P<cut>(?:用(?:匕首|小刀|刀))?(?:切開|切开|剖開|剖开))?" + _CORPSE + _SEARCH + _LOOT + r"$"),
    re.compile(r"^" + _SEARCH + _CORPSE + r"$"),
)


def parse_corpse_search(text, party_names=()):
    """An inspection method plus its loot-search goal is one search attempt."""
    actor, action = _action_text(text, party_names)
    if re.search(r"如果|假如|唔|不要|不想|別|别|[?？]|然後|然后|之後|之后|再|攻擊|攻击|喝|使用", action):
        return None
    for pattern in _CORPSE_PATTERNS:
        match = pattern.fullmatch(action.rstrip("。.!！"))
        if match:
            body = _GROUND.sub("", match["body"])
            name = body.removesuffix("屍體").removesuffix("尸体").strip("的嘅 ")
            if name:
                return Intent("search", actor=actor, target=name, raw=text,
                              args={"corpse_search": name, "needs_blade": bool(match.groupdict().get("cut"))})
    return None


def corpse_preflight(g, it):
    name = it.args.get("corpse_search")
    if type(name) is not str or not name or type(it.args.get("needs_blade")) is not bool:
        raise PickupError("請指定要搜查的屍體。")
    foes = {f.name: f for f in g.encounters.get(g.world.current, [])}
    foes.update(g.enemies)
    matches = [f for n, f in foes.items() if name == n or name in n]
    corpses = [f for f in matches if f.dead or f.hp <= 0]
    if not corpses:
        raise PickupError(f"這裡找不到「{name}」的屍體；活著的敵人不能當作屍體搜查。")
    if it.args["needs_blade"] and not any(q > 0 and re.search(r"匕首|刀|劍|剑|dagger|knife|sword", n, re.I)
                                         for n, q in g.inventory.get(it.actor, [])):
        raise PickupError("你沒有可用的刀具；可徒手搜索屍體，或先拾起地上的匕首。")
    return [f.name for f in corpses]


def preflight(g, value):
    """(actual transfers, optional inert prop). Check the whole list first.

    Hidden items never satisfy pickup requests. A single untracked inert prop
    retains the existing director behavior; collections cannot create props.
    """
    names = selection(value)
    ground = Counter()
    for name, qty in g.world.here.ground_items:
        if qty > 0:
            ground[name] += qty
    if names is None:
        if not ground:
            raise PickupError("地上沒有可拾取的物品；可以先搜索這裡。")
        if len(ground) > MAX_ITEMS:
            raise PickupError(f"地上物品太多，一次最多拾取 {MAX_ITEMS} 種，請指定名稱。")
        return list(ground.items()), ""
    actual = []
    for name in names:
        matches = [name] if name in ground else [n for n in ground if name in n]
        if len(matches) > 1:
            raise PickupError(f"「{name}」對應多件物品，請指定：{'、'.join(matches)}。")
        if not matches:
            from .director import classify
            hidden = any(name == n or name in n or n in name for n, _ in g.world.here.hidden_items)
            if len(names) == 1 and not hidden and classify(name) == "prop":
                return [(name, 1)], name
            raise PickupError(f"地上看不到可拾取的「{name}」；尚未拾取任何物品，可以先搜索這裡。")
        if matches[0] not in actual:
            actual.append(matches[0])
    return [(name, ground[name]) for name in actual], ""
