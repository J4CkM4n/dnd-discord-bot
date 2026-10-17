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

    def scene_enemies(self, scene_id=None) -> dict[str, Enemy]:
        """Encounter identities remain present after fleeing, including corpses."""
        sid = self.world.current if scene_id is None else scene_id
        foes = {f.name: f for f in self.encounters.get(sid, [])}
        if sid == self.world.current:
            foes.update(self.enemies)
        return foes

    def scene_npcs(self, scene_id=None) -> list[dict]:
        """The same NPC metadata before, during and after a hostile encounter."""
        sid = self.world.current if scene_id is None else scene_id
        foes = self.scene_enemies(sid)
        npcs = {n["name"]: n for n in self.world.scenes[sid].npcs}
        npcs.update({f.name: f.npc for f in foes.values() if f.npc})
        return [n for name, n in npcs.items()
                if name not in foes or (not foes[name].dead and foes[name].hp > 0)]

    def target_names(self) -> list[str]:
        """Current action candidates; defeated enemies are history, not targets.

        Keep downed party members available for healing and first aid.
        """
        return list(dict.fromkeys(
            [n for n, f in self.scene_enemies().items() if not f.dead and f.hp > 0]
            + [n["name"] for n in self.scene_npcs()] + list(self.party)))

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
        self.director_scene_count = 0
        self.retired_demo_names: list[str] = []

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
        if type(qty) is not int or qty <= 0:
            raise ValueError("Item quantity must be a positive integer")
        for stack in self.inventory[char]:
            if stack[0] == item:
                stack[1] += qty
                return
        self.inventory[char].append([item, qty])

    def take_item(self, char: str, item: str, qty: int = 1) -> bool:
        if type(qty) is not int or qty <= 0:
            return False
        stacks = self.inventory[char]
        if sum(q for n, q in stacks if n == item) < qty:
            return False
        for stack in list(stacks):
            if stack[0] == item and qty:
                amount = min(qty, stack[1])
                stack[1] -= amount
                qty -= amount
                if stack[1] == 0:
                    stacks.remove(stack)
        return True

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
            # Keep identity/history; a cleared encounter never respawns.
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
            if not foe.spawned:
                # Older saves lack the marker; injuries still prove this is
                # an existing combatant and must never be scaled into healing.
                if foe.hp >= foe.hp_max:
                    scale_for_party(foe, self.party)
                foe.spawned = True
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
        if self.combat.active and all(f.dead or f.hp <= 0 for f in self.enemies.values()):
            self.end_combat()
            return True
        return False

    # ---------- dice (engine-only write path) ----------

    def roll(self, expr: str):
        return roll_expr(expr, rng=self._random)

    def d20(self) -> int:
        return self._random.randint(1, 20)
