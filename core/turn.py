"""v4 turn machine: the Game object — engine-owned state + rotation.

The Game is the single source of truth. LLMs never mutate it (P2 digestor
produces Intents; rules_core is the only writer beside the Game itself).
"""

import random

from engine.charlib import default_ac, prof_bonus, slots_for
from engine.dice import roll_expr

from .intent import Intent
from .ledger import Ledger
from .world import World, Enemy


class Combat:
    def __init__(self):
        self.order: list[dict] = []   # {name, init, npc} — initiative, flavour
        self.idx = 0
        self.round = 1
        # v5 async-friendly: per-round ACTION SLOTS — any PC may act in
        # any order, once per round; enemies resolve when all acted
        self.acted: list[str] = []

    @property
    def active(self) -> bool:
        return bool(self.order)

    def current(self) -> dict | None:
        return self.order[self.idx % len(self.order)] if self.order else None


class Game:
    """Party + world + combat + inventory + ledger."""

    def __init__(self, party: dict, world: World, encounters: dict = None):
        # party: {name: {occupation, stats, hp_now, hp_max, level, slots?}}
        self.party = party
        for name, e in self.party.items():
            e.setdefault("level", 1)
            if "slots" not in e:
                e["slots"] = {str(k): v for k, v in slots_for(
                    e.get("occupation", ""), int(e.get("level", 1))).items()}
            e.setdefault("hp_max", 10)
            e.setdefault("hp_now", e["hp_max"])
        self.world = world
        self.encounters = encounters or {}   # scene_id -> [Enemy,...]
        self.enemies: dict[str, Enemy] = {}  # active combatants (by name)
        self.combat = Combat()
        self.inventory: dict[str, list] = {n: [] for n in party}
        # inventory[char] = [(name, qty)] — stacks
        self.ledger = Ledger()
        self._random = random.Random()

    # ---------- helpers ----------

    def char(self, name: str):
        return self.party.get(name)

    def ac_of(self, name: str) -> int:
        e = self.party[name]
        try:
            return int(e.get("ac") or default_ac(e.get("occupation", ""),
                                                 e.get("stats") or {}))
        except (TypeError, ValueError):
            return 13

    def alive(self, name: str) -> bool:
        e = self.party[name]
        if (e.get("death") or {}).get("dead"):
            return False  # perma-dead: three failed death saves
        hp = e.get("hp_now")
        return hp is None or int(hp) > 0

    def downed(self, name: str) -> bool:
        e = self.party.get(name) or {}
        return (not (e.get("death") or {}).get("dead")
                and int(e.get("hp_now", 1) or 0) <= 0)

    # ---------- conditions (v5 seed 3) ----------
    # e["conds"] = {name: rounds_left or None (= no expiry, e.g. 靈感)}

    def conds(self, name: str) -> dict:
        e = self.party[name]
        if "conds" not in e or not isinstance(e["conds"], dict):
            e["conds"] = {}
        return e["conds"]

    def add_cond(self, name: str, cond: str, rounds=None) -> None:
        self.conds(name)[cond] = rounds

    def has_cond(self, name: str, cond: str) -> bool:
        return cond in self.conds(name)

    def drop_cond(self, name: str, cond: str) -> bool:
        """Remove and report presence — None is a VALID value (no-expiry
        conditions like inspiration), so use a sentinel, not None."""
        c = self.conds(name)
        if cond in c:
            del c[cond]
            return True
        return False

    def tick_conds(self) -> list[str]:
        """Round change: expire timed conditions. Returns notes."""
        out = []
        for n, e in self.party.items():
            for cond in list(self.conds(n)):
                left = e["conds"][cond]
                if left is None:
                    continue
                e["conds"][cond] = int(left) - 1
                if e["conds"][cond] <= 0:
                    del e["conds"][cond]
                    out.append(f"{n} 的{cond}效果結束了")
        return out

    def give_item(self, char: str, item: str, qty: int = 1) -> None:
        for stack in self.inventory[char]:
            if stack[0] == item:
                stack[1] += qty
                return
        self.inventory[char].append([item, qty])

    def take_item(self, char: str, item: str, qty: int = 1) -> bool:
        for stack in self.inventory[char]:
            if stack[0] == item and stack[1] >= qty:
                stack[1] -= qty
                if stack[1] <= 0:
                    self.inventory[char].remove(stack)
                return True
        return False

    def max_slot(self, char: str) -> int:
        mx = slots_for(self.party[char].get("occupation", ""),
                       int(self.party[char].get("level", 1)))
        return max(mx, default=0)

    def spend_slot(self, char: str, level: int) -> bool:
        key = str(level)
        cur = self.party[char]["slots"].get(key, 0)
        if int(cur) <= 0:
            return False
        self.party[char]["slots"][key] = int(cur) - 1
        return True

    # ---------- combat ----------

    def start_encounter(self, seed: int = None) -> list[dict]:
        """Spawn this scene's encounter (once) and roll initiative.
        v5: enemies scale to the party tier (CR budgeting); DEAD
        encounter entries are never respawned, and a fully-cleared
        encounter is consumed."""
        from engine.cr import scale_for_party
        scene = self.world.current
        foes = [f for f in self.encounters.get(scene, [])
                if not f.dead and f.hp > 0]
        if not foes:
            self.encounters.pop(scene, None)   # consumed / all dead
            return self.combat.order
        if self.combat.active:
            return self.combat.order
        rng = self._random if seed is None else random.Random(seed)
        order = []
        for name, e in self.party.items():
            dex = int(e.get("stats", {}).get("DEX", 10))
            order.append({"name": name, "init": rng.randint(1, 20) + (dex - 10) // 2,
                          "npc": False})
        for foe in foes:
            scale_for_party(foe, self.party)
            foe.initiative = rng.randint(1, 20) + 2
            self.enemies[foe.name] = foe
            order.append({"name": foe.name, "init": foe.initiative, "npc": True})
        order.sort(key=lambda o: (-o["init"], o["npc"]))
        self.combat = Combat()
        self.combat.order = order
        return order

    def end_combat(self) -> None:
        self.combat = Combat()
        self.enemies.clear()

    def advance(self) -> dict | None:
        c = self.combat
        if not c.order:
            return None
        c.idx += 1
        if c.idx >= len(c.order):
            c.idx = 0
            c.round += 1
        return c.current()

    def check_end(self) -> bool:
        """All enemies down -> auto end. Returns True if combat ended."""
        if self.enemies and all(f.dead or f.hp <= 0 for f in self.enemies.values()):
            self.end_combat()
            return True
        return False

    # ---------- dice (engine-only write path) ----------

    def roll(self, expr: str):
        return roll_expr(expr)

    def d20(self) -> int:
        return self._random.randint(1, 20)
