"""v5 Director (0a): optimistic materialization of untracked props.

The v1 lesson: players experience responsiveness. When a player
interacts with something the narration implied but the engine never
tracked（木雕）, the DEFAULT is now YES — the Director materializes it
into the scene as a mechanically inert prop, the engine validates, the
ledger records the retcon.

The boundary stays engine-owned: anything with dice, value, or plot
weight (weapons, armor, potions, treasure, jewelry, scrolls, keys,
magic) is BLOCKED from materialization — those must come from the DM
or a plot beat. The LLM never writes state; it only prompted the
attempt by describing the world.
"""

import re

# mechanically meaningful / valuable / plot-bearing -> may NOT be
# conjured from thin air (conservative by design)
_BLOCKED = re.compile(
    r"劍|刀|弓|弩|斧|槌|錘|矛|槍|炮|匕|盾|甲|鎧|盔"
    r"|藥水|藥劑|靈藥|丹"
    r"|金幣|銀兩|黃金|銅板|寶石|珍珠|珠寶|玉|瑪瑙|翡翠"
    r"|戒指|項鍊|墜飾|護符"
    r"|捲軸|書卷|法典"
    r"|魔法|魔杖|法杖|法器|神器|詛咒"
    r"|鑰匙|鎖匙"
    r"|sword|blade|axe|bow|gun|rifle|spear|hammer|dagger"
    r"|armor|armour|shield|potion|wand|staff|rod"
    r"|coin|gold|gem|jewel|ring|amulet|necklace|scroll|key"
    r"|artifact",
    re.I)

# props nobody should be able to conjure by naming them
_NON_PROP = re.compile(r"空氣|牆|地板|天空|太陽|月亮|海|水$|^水")

MAX_NAME = 10  # a prop name, not an essay


def classify(name: str) -> str:
    """"prop" if the item is safe to materialize, "blocked" if it
    carries mechanical weight（dice/value/plot）."""
    t = (name or "").strip()
    if not t or len(t) > MAX_NAME or _NON_PROP.search(t):
        return "blocked"
    return "blocked" if _BLOCKED.search(t) else "prop"


def try_materialize(g, actor: str, name: str) -> bool:
    """Engine-gated materialization: add the prop to the CURRENT scene
    and ledger the retcon. Returns False when the item is blocked."""
    if classify(name) != "prop":
        return False
    s = g.world.here
    # already tracked? then this call was unnecessary (harmless no-op)
    if any(name == n or name in n or n in name for n, _ in
           s.ground_items + s.hidden_items):
        return True
    s.ground_items.append((name, 1))
    g.ledger.add("director", "materialize",
                 f"世界補上了 {name}（回應 {actor} 的互動）",
                 item=name, scene=g.world.current, player=actor)
    return True


# A3 organic search: mundane finds for scenes with nothing authored —
# looking around SOMETIMES pays off (the v1 feel), never anything with
# dice/value/plot weight... except the rare real item below
_AMBIENT_PROPS = (
    "纏著海草的空瓶", "生鏽的魚鉤", "乾燥的海星", "斷裂的船槳",
    "佈滿鹽漬的木牌", "被磨圓的玻璃石", "褪色的布條", "完整的貝殼",
    "焦黑的木炭", "磨損的皮繩圈",
    # creative flavour finds (still mechanically inert)
    "一枚刻著奇怪符號的硬幣（用不出去）", "缺頁的航海日誌",
    "藏在貝殼裡的紙條", "停在奇怪時刻的鏽蝕懷錶",
    "魚骨雕成的小梳", "鹽結晶的小雕像", "半截蠟封的信",
    "綴著牙齒的項鍊（不是金的）")

# the rare REAL find (~15% of finds) — usable gear
_REAL_FINDS = ("治療藥水", "火把", "繩索", "匕首", "草藥包")


def ambient_find(g) -> str | None:
    """~30% chance to hide one find in the current scene for the search
    to reveal: mostly flavour props, sometimes a genuinely useful item.
    Ledgered as a materialization."""
    import random
    if random.random() > 0.3:
        return None
    if random.random() < 0.15:
        name = g._random.choice(_REAL_FINDS)
    else:
        name = g._random.choice(_AMBIENT_PROPS)
    s = g.world.here
    if any(name in n or n in name for n, _ in
           s.ground_items + s.hidden_items):
        return None  # already here — don't stack the same prop
    s.hidden_items.append((name, 1))
    g.ledger.add("director", "materialize",
                 f"搜索途中世界浮現了 {name}", item=name,
                 scene=g.world.current)
    return name


def named_find(g, name: str) -> bool:
    """A search that NAMES what it's looking for（「我搵下有冇魚鉤」）:
    ~50% the thing is actually there — intent beats blind RNG when the
    story needs a specific mundane item."""
    import random
    if classify(name) != "prop":
        return False
    s = g.world.here
    if any(name in n or n in name for n, _ in
           s.ground_items + s.hidden_items):
        return True  # already here — the check below will reveal it
    if random.random() > 0.5:
        return False
    s.hidden_items.append((name, 1))
    g.ledger.add("director", "materialize",
                 f"搜索途中世界浮現了 {name}", item=name,
                 scene=g.world.current)
    return True


def scan_prop(text: str) -> str:
    """Known prop/catalog name mentioned in free text — full name first,
    then distinctive TAILS（「魚鉤」matches 生鏽的魚鉤）, longest wins."""
    from engine.itemlib import CATALOG
    t = text or ""
    for n in sorted(set(_AMBIENT_PROPS) | set(CATALOG),
                    key=len, reverse=True):
        if n and n in t:
            return n
    for n in sorted(set(_AMBIENT_PROPS) | set(CATALOG),
                    key=len, reverse=True):
        if not n:
            continue
        for i in range(len(n) - 1, 1, -1):
            tail = n[i:]
            if tail.startswith(("的", "著", "個", "一")):
                continue
            if tail in t:
                return n
    return ""


# cap on LLM-generated scenes: the world stays DM-shaped, not infinite
MAX_DIRECTOR_SCENES = 12


def canonize_scene(g, proposed: dict) -> bool:
    """ENGINE GATE for unplanned places: validate the Director LLM's
    typed proposal, build the scene node with bidirectional exits, and
    ledger the retcon. Returns False when the gate refuses."""
    if not isinstance(proposed, dict):
        return False
    name = str(proposed.get("name", "")).strip()
    desc = str(proposed.get("description", "")).strip()
    npc = str(proposed.get("npc", "")).strip()
    if not (1 < len(name) <= 8) or not desc or len(desc) > 120:
        return False
    made = sum(1 for e in g.ledger.entries
               if e.kind == "director_scene")
    if made >= MAX_DIRECTOR_SCENES:
        return False
    # engine-owned id; exits are ALWAYS bidirectional — nobody gets lost
    sid = f"dir{made + 1}_{abs(hash(name)) % 10000}"
    here = g.world.here
    from .world import Scene
    scene = Scene(sid, name, desc, exits={here.id: here.name})
    if npc and len(npc) <= 8:
        scene.npcs.append({"name": npc, "desc": "陌生的面孔",
                           "disposition": "neutral", "knows": []})
    g.world.scenes[sid] = scene
    here.exits[sid] = name
    g.ledger.add("director", "director_scene",
                 f"世界展開了新角落：{name}（通往 {here.name} 的回頭路仍在）",
                 scene=sid, name=name)
    return True
