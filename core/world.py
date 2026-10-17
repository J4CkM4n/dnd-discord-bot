"""v4 world model: the structured world the ENGINE owns and validates against.

Scenes form a graph (exits); NPCs and ground items belong to scenes;
encounters spawn combatants. The director (P2+) may PROPOSE new nodes —
they enter this model only through engine/admin gates, never directly.
"""

from dataclasses import dataclass, field
import unicodedata


def scene_key(name: str) -> str:
    """Ignore typography, not words, when comparing place identities."""
    return "".join(c for c in unicodedata.normalize("NFKC", name).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


@dataclass
class Enemy:
    """A combatant the engine can resolve without any LLM."""
    name: str
    hp: int
    hp_max: int
    ac: int
    attack_bonus: int = 3
    dmg: str = "1d6"          # dice expression
    initiative: int = 0
    dead: bool = False
    loot: list = field(default_factory=list)   # guaranteed drops (quest items)
    loot_fixed: bool = False  # an emptied authored inventory stays empty
    npc: dict = field(default_factory=dict)  # identity retained after hostility
    spawned: bool = False  # encounter scaling runs once, never on return
    absent_items: list = field(default_factory=list)
    possessions_complete: bool = False

    @classmethod
    def make(cls, name: str, hp: int, ac: int, attack_bonus: int = 3,
             dmg: str = "1d6", loot: list = None) -> "Enemy":
        return cls(name=name, hp=hp, hp_max=hp, ac=ac,
                   attack_bonus=attack_bonus, dmg=dmg,
                   loot=loot or [])


@dataclass
class Scene:
    id: str
    name: str
    description: str = ""
    exits: dict = field(default_factory=dict)      # scene_id -> label
    npcs: list = field(default_factory=list)        # {name, desc, disposition}
    ground_items: list = field(default_factory=list)  # (name, qty)
    search_dc: int = 12
    hidden_items: list = field(default_factory=list)  # revealed by search
    fire: bool = False        # v5 A1: the scene is burning (engine state)
    absent_items: list = field(default_factory=list)
    possessions_complete: bool = False


@dataclass
class World:
    scenes: dict = field(default_factory=dict)      # id -> Scene
    current: str = ""

    def add_scene(self, scene: Scene, links: dict = None) -> None:
        """links: {other_id: label_from_other_back} — bidirectional helper."""
        self.scenes[scene.id] = scene
        for other, back_label in (links or {}).items():
            scene.exits[other] = back_label
            if other in self.scenes:
                self.scenes[other].exits[scene.id] = back_label

    @property
    def here(self) -> Scene:
        return self.scenes[self.current]

    def find_exit(self, destination: str) -> str | None:
        """Prefer exact identities; accept only unambiguous normalized aliases."""
        d = (destination or "").strip()
        if not d:
            return None
        aliases = {sid: (label, self.scenes[sid].name) for sid, label in self.here.exits.items()
                   if sid in self.scenes}
        if d in aliases:
            return d
        for match in (lambda name: d == name,
                      lambda name: scene_key(d) == scene_key(name),
                      lambda name: bool(scene_key(d)) and (scene_key(d) in scene_key(name)
                                                          or scene_key(name) in scene_key(d))):
            found = [sid for sid, names in aliases.items() if any(match(n) for n in names)]
            if found:
                return found[0] if len(found) == 1 else None
        return None

    def describe(self) -> str:
        s = self.here
        lines = [f"📍 {s.name}", s.description]
        if s.exits:
            lines.append("通道：" + "、".join(s.exits.values()))
        if s.npcs:
            lines.append("人物：" + "、".join(n["name"] for n in s.npcs))
        return "\n".join(lines)
