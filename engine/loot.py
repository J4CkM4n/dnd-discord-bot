"""v5: loot drops + XP — the v2 mechanics that v4 dropped.

Enemy deaths roll loot into the scene (players pick it up — looting is
half the fun) and grant XP to the whole party; level-ups are engine
events (HP up, spell slots refreshed to the new level's table — every
derived stat follows automatically through charlib).

Enemies may carry AUTHORED loot (Enemy.loot) — that is how quest items
enter the world legitimately: a plot-spawned 巴鐸 carrying 海妖女王的項鍊.
"""

import random

# weighted generic drops ( monsters without authored loot)
LOOT_TABLE = [
    ("治療藥水", 25),
    ("火把", 15),
    ("繩索", 15),
    ("匕首", 10),
    ("草藥包", 10),
    ("生鏽的鐵牌", 10),
    ("木盾", 5),
    ("魚叉", 5),
    (None, 25),  # nothing — not every body has pockets
]

_LOOT_ITEMS = [(n, w) for n, w in LOOT_TABLE]


def drop_for(foe, rng=None):
    """(name, qty) list this enemy leaves behind."""
    if getattr(foe, "loot", None) or getattr(foe, "loot_fixed", False):
        return [(n, 1) for n in foe.loot]        # authored = guaranteed
    roll = (rng or random).choices([n for n, _ in _LOOT_ITEMS],
                          weights=[w for _, w in _LOOT_ITEMS], k=1)[0]
    return [(roll, 1)] if roll else []


def xp_for(foe) -> int:
    return 10 * int(getattr(foe, "hp_max", 8) or 8) + 20


def add_xp(g, xp: int, lines: list = None, char: str = "") -> None:
    """Grant XP (whole party, or one character) with inline level-ups:
    HP cap up (hit-die-ish), spell slots refreshed, proficiency follows
    via charlib. The DM lever (/xp-admin) and kill-XP share this path.
    lines may be None for silent administration."""
    lines = lines if lines is not None else []
    who = [char] if char and char in g.party else list(g.party)
    for name in who:
        e = g.party[name]
        if not isinstance(e, dict):
            continue
        e["xp"] = int(e.get("xp", 0)) + xp
        while True:  # possibly multiple levels at once
            need = 100 * int(e.get("level", 1))
            if int(e["xp"]) < need:
                break
            e["xp"] -= need
            e["level"] = int(e.get("level", 1)) + 1
            gain = 2 + g._random.randint(1, 6)     # hit-die-ish HP
            e["hp_max"] = int(e.get("hp_max", 10)) + gain
            if int(e.get("hp_now", 0)) > 0:
                e["hp_now"] = int(e["hp_now"]) + gain
            from .charlib import slots_for
            e["slots"] = {str(k): v for k, v in slots_for(
                e.get("occupation", ""), int(e["level"])).items()}
            lines.append(f"🎉 **{name} 升到 Lv{e['level']}！**"
                         f" HP 上限 +{gain}、法術格已按新等級更新")
    g.ledger.add("engine", "xp", f"獲得 {xp} XP"
                 + (f"（{char}）" if char else "（全隊）"), xp=xp)


def grant_xp(g, foe, lines: list):
    """Party-wide XP for a kill; level-ups fire inline."""
    add_xp(g, xp_for(foe), lines)
