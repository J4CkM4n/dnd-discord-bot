"""Complete, versioned game snapshots. Event history lives in the repository."""

from copy import deepcopy
from dataclasses import asdict

from .ledger import Entry
from .turn import Game
from .world import Enemy, Scene, World

STATE_VERSION = 7
TRANSIENT_FIELDS = ("_pending_check", "_aid", "_dodge", "_stealth", "_help_check")


def clone_game(game: Game) -> Game:
    return deepcopy(game, {id(game.ledger): game.ledger.fork()})


def encode_game(g: Game) -> dict:
    return {
        "v": STATE_VERSION,
        "party": g.party,
        "world": {"scenes": {sid: asdict(s) for sid, s in g.world.scenes.items()},
                  "current": g.world.current},
        "encounters": {sid: [asdict(f) for f in foes]
                       for sid, foes in g.encounters.items()},
        "enemies": {n: asdict(f) for n, f in g.enemies.items()},
        "combat": vars(g.combat),
        "inventory": g.inventory,
        "turn": g.ledger.turn,
        "rng": g._random.getstate(),
        "director_scene_count": getattr(g, "director_scene_count", 0),
        "retired_demo_names": getattr(g, "retired_demo_names", []),
        "effects": {key: getattr(g, key, None) for key in TRANSIENT_FIELDS},
    }


def _tuples(value):
    return tuple(_tuples(v) for v in value) if isinstance(value, list) else value


def decode_game(blob: dict, entries: list[dict]) -> Game:
    """Load v4–v7 snapshots; malformed saves fail rather than reset a campaign."""
    if not isinstance(blob, dict) or type(blob.get("v")) is not int \
            or blob["v"] not in (4, 5, 6, STATE_VERSION):
        raise ValueError("Unsupported game state version")
    if not isinstance(blob["party"], dict) or not isinstance(blob["world"], dict):
        raise ValueError("Invalid party/world in save")
    world = World()
    for sid, raw in blob["world"]["scenes"].items():
        s = dict(raw)
        s.pop("id", None)
        for key in ("ground_items", "hidden_items"):
            s[key] = [tuple(x) for x in s.get(key, [])]
        world.scenes[sid] = Scene(sid, **s)
    world.current = blob["world"]["current"]
    if world.current not in world.scenes or any(
            dest not in world.scenes for s in world.scenes.values() for dest in s.exits):
        raise ValueError("Invalid scene reference in save")
    g = Game(deepcopy(blob["party"]), world)
    for sid, foes in blob.get("encounters", {}).items():
        g.encounters[sid] = [Enemy(**f) for f in foes]
    for name, foe in blob.get("enemies", {}).items():
        # Active enemies and their encounter must share identity, as in live play.
        shared = next((f for f in g.encounters.get(world.current, [])
                       if f.name == name), None)
        active = Enemy(**foe)
        if shared is not None:
            shared.__dict__.update(vars(active))
            active = shared
        g.enemies[name] = active
    combat = blob.get("combat") or {}
    for key, default in (("order", []), ("idx", 0), ("round", 1), ("acted", [])):
        setattr(g.combat, key, combat.get(key, default))
    g.inventory = {n: [list(stack) for stack in stacks]
                   for n, stacks in blob.get("inventory", {}).items()}
    for name in g.party:
        g.inventory.setdefault(name, [])
    g.ledger.turn = blob.get("turn", 0)
    for seq, raw in enumerate(entries, 1):
        e = dict(raw)
        # Historical JSON entries did not have IDs. Preserve ordered import.
        e["event_id"] = e.get("event_id") or seq
        if e["event_id"] != seq:
            raise ValueError("Non-contiguous event IDs in save")
        g.ledger.entries.append(Entry(**e))
    for key in TRANSIENT_FIELDS:
        value = (blob.get("effects") or {}).get(key)
        if value is not None:
            setattr(g, key, value)
    if blob.get("rng") is not None:
        g._random.setstate(_tuples(blob["rng"]))
    g.director_scene_count = blob.get("director_scene_count", sum(
        e.kind == "director_scene" for e in g.ledger.entries))
    g.retired_demo_names = list(blob.get("retired_demo_names", []))
    return g
