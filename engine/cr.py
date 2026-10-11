"""v5: CR budgeting — encounters scale with the party.

Authored encounters stay authored; the ENGINE scales them at spawn so
the same scene stays meaningful as the party levels (the v4 honest
boundary "difficulty scaling: none" closes). Plot spawns may also ask
for a monster by CR instead of raw stats.
"""


def party_tier(party: dict) -> int:
    """1 (Lv1-3) / 2 (Lv4-6) / 3 (Lv7+)."""
    levels = [int(e.get("level", 1) or 1) for e in party.values()
              if isinstance(e, dict)]
    avg = sum(levels) / len(levels) if levels else 1
    if avg <= 3:
        return 1
    if avg <= 6:
        return 2
    return 3


_TIER_MULT = {1: 1.0, 2: 1.25, 3: 1.5}
_TIER_BONUS = {1: 0, 2: 1, 3: 2}


def scale_for_party(foe, party: dict):
    """Mutate the encounter enemy to the party's tier (HP/attack)."""
    t = party_tier(party)
    foe.hp = max(1, round(foe.hp * _TIER_MULT[t]))
    foe.hp_max = max(foe.hp_max, foe.hp)
    foe.attack_bonus += _TIER_BONUS[t]
    return foe


# CR -> (hp, ac, attack_bonus, dmg) — coarse SRD-ish bands
CR_STATS = {
    "0": (6, 12, 2, "1d4"), "1/2": (9, 13, 3, "1d6"),
    "1": (13, 13, 3, "1d8"), "2": (18, 14, 4, "2d6"),
    "3": (26, 15, 4, "2d8"), "4": (36, 15, 5, "3d6"),
    "5": (49, 16, 5, "3d8"),
}


def make_by_cr(name: str, cr: str):
    """Enemy from a CR band (plot spawns may cite CR instead of stats)."""
    from core.world import Enemy
    hp, ac, bonus, dmg = CR_STATS.get(str(cr), CR_STATS["1"])
    return Enemy.make(name, hp, ac, bonus, dmg)
