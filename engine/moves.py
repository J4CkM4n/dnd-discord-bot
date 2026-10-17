"""Shared attack-move math — the single source of truth for what moves
each class gets, their spell-slot requirements, and how inventory items
become improvised weapons.

Used by: v4 engine (/combat, /inventory), dnd-health (/moves reference).
"""

import re


class_moves = {
        "Barbarian": [("手斧劈擊", "STR", "1d6", False),
                      ("狂暴巨斧", "STR", "1d12", False)],
        "Bard": [("細劍突刺", "DEX", "1d8", False),
                 ("惡毒嘲諷", "CHA", "1d4", False)],
        "Cleric": [("戰錘重擊", "STR", "1d8", False),
                   ("聖火術", "WIS", "1d8", False)],
        "Druid": [("詛咒木杖", "WIS", "1d8", False),
                  ("荊棘鞭打", "WIS", "1d6", False),
                  ("月光束（範圍）", "WIS", "2d10", True)],
        "Fighter": [("長劍斬擊", "STR", "1d8", False),
                    ("盾擊", "STR", "1d4", False)],
        "Monk": [("徒手連擊", "DEX", "1d4", False),
                 ("飛鏢", "DEX", "1d4", False)],
        "Paladin": [("長劍斬擊", "STR", "1d8", False),
                    ("神聖痛擊", "STR", "2d8", False)],
        "Ranger": [("短弓射擊", "DEX", "1d6", False),
                   ("雙匕首", "DEX", "1d4", False)],
        "Rogue": [("短劍背刺", "DEX", "1d6", False),
                  ("匕首投擲", "DEX", "1d4", False)],
        "Sorcerer": [("火焰箭", "CHA", "1d10", False),
                     ("灼熱之手（範圍）", "CHA", "3d6", True)],
        "Warlock": [("魔能爆", "CHA", "1d10", False),
                    ("地獄斥喝", "CHA", "2d10", False)],
        "Wizard": [("火焰箭", "INT", "1d10", False),
                   ("燃燒之手（範圍）", "INT", "3d6", True)],
}

move_spell_level = {"月光束（範圍）": 2, "地獄斥喝": 1, "灼熱之手（範圍）": 1,
                        "燃燒之手（範圍）": 1, "神聖痛擊": 1}

_WEAPON_GLYPHS = "劍斧弓矛叉棍杖匕鏢錘刀槍鞭"
_WEAPON_NAMES = re.compile(
    r"剑|锤|剣|銃|ナイフ|ダガー|ソード|"
    r"\b(?:swords?|longswords?|shortswords?|greatswords?|rapiers?|scimitars?|"
    r"daggers?|knives|knife|axes?|greataxes?|handaxes?|bows?|shortbows?|longbows?|"
    r"crossbows?|spears?|javelins?|tridents?|maces?|hammers?|warhammers?|clubs?|"
    r"quarterstaffs?|staff|staves|darts?|slings?|whips?|guns?|rifles?|blades?)\b", re.I)


def is_weapon(name: str) -> bool:
    """Recognize named weapons; throwable props and category labels are not weapons."""
    return any(g in name for g in _WEAPON_GLYPHS) or bool(_WEAPON_NAMES.search(name))

# Physical starting equipment is recorded once, when /pc creates the character.
STARTER_EQUIPMENT = {
    "Barbarian": ("手斧", "巨斧"), "Bard": ("細劍",), "Cleric": ("戰錘", "盾牌"),
    "Druid": ("木杖",), "Fighter": ("長劍", "盾牌"), "Monk": ("飛鏢",),
    "Paladin": ("長劍", "盾牌"), "Ranger": ("短弓", "匕首"), "Rogue": ("短劍", "匕首"),
    "Sorcerer": ("匕首",), "Warlock": ("匕首",), "Wizard": ("木杖",),
}


def compute_attack_moves(occupation: str, level: int, inventory: list,
                         max_slot_level: int = 99) -> list[dict]:
    """Pure move math (no LLM, no DB): class signature attacks gated by the
    character's granted spell-slot levels, plus carried weapons. Shared by
    the DM engine (/attack, /act panels) and the dnd-health /moves reference
    so the two can never drift apart."""
    occ = occupation or ""
    out = [{"name": n, "ability": ab, "dmg": d, "aoe": a}
           for n, ab, d, a in class_moves.get(occ, [])]
    out = [m for m in out
           if move_spell_level.get(m["name"], 0) <= max_slot_level]
    known = [m["name"] for m in out]
    for kind, name, _qty in inventory or []:
        # only weapons become moves: equipment-kind or a weapon glyph in the
        # name — rope and potions are not attack moves
        weaponish = kind == "equipment" or is_weapon(name)
        if weaponish and name not in known and "[" not in name:
            # finesse weapons (匕首/飛鏢) strike with DEX
            ab = "DEX" if ("匕" in name or "鏢" in name) else "STR"
            out.append({"name": f"{name} 打擊", "ability": ab,
                        "dmg": "1d6", "aoe": False, "improvised": True})
            known.append(name)
    return out
