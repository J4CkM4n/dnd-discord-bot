"""Version-one authored adventures: strict engine schema and private conversion."""

import re
from copy import deepcopy

from .turn import Game
from .world import Enemy, Scene, World

SCHEMA_VERSION = 1
CHUNK_CHARS = 3000
MAX_BYTES = 1024 * 1024
MAX_CHARS = 100_000
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_DICE = re.compile(r"(?:[1-9]|1[0-9]|20)d(?:4|6|8|10|12|20)(?:[+-][0-9]{1,3})?")


def decode_upload(filename, data):
    from .script_store import ScriptError
    if not isinstance(filename, str) or not filename.lower().endswith((".txt", ".md")):
        raise ScriptError("只接受 UTF-8 .txt 或 .md 附件。")
    if len(data) > MAX_BYTES:
        raise ScriptError("附件超過 1 MiB，未有截斷或處理。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ScriptError("附件不是有效的 UTF-8 文字；請另存為 UTF-8 後重試。") from None
    if len(text) > MAX_CHARS:
        raise ScriptError("劇本超過 100,000 字元，未有截斷或處理。")
    if not text.strip() or "\x00" in text:
        raise ScriptError("劇本必須是非空的 UTF-8 純文字。")
    return text


def chunks(text):
    """Preserve every character, preferring paragraph boundaries."""
    result = []
    while len(text) > CHUNK_CHARS:
        cut = text.rfind("\n", CHUNK_CHARS // 2, CHUNK_CHARS)
        cut = cut + 1 if cut >= 0 else CHUNK_CHARS
        result.append(text[:cut])
        text = text[cut:]
    if text:
        result.append(text)
    return result


def empty_definition():
    return {"schema_version": 1, "complete": True, "title": "", "starting_scene": "",
            "public_setting": {"tone": "", "premise": "", "facts": []},
            "scenes": [], "encounters": {},
            "plot": {"objectives": [], "clocks": [], "beats": {}, "triggers": []},
            "secrets": [], "private_author_notes": [], "conversion_issues": []}


class _Validator:
    def __init__(self, *, allow_unresolved=False):
        self.issues = []
        self.allow_unresolved = allow_unresolved

    def fail(self, path, reason):
        # Paths are schema labels/indices, never supplied values or source text.
        self.issues.append(f"{path}: {reason}")

    def obj(self, value, keys, path, required=()):
        if type(value) is not dict or set(value) - set(keys) or set(required) - set(value):
            self.fail(path, "invalid or unsupported fields")
            return {}
        return value

    def seq(self, value, path, limit=1000):
        if type(value) is not list or len(value) > limit:
            self.fail(path, "expected a bounded list")
            return []
        return value

    def text(self, value, path, *, blank=False, limit=10000):
        if not isinstance(value, str) or len(value) > limit or (not blank and not value.strip()):
            self.fail(path, "expected text" if not blank else "expected text (may be empty)")
            return ""
        return value

    def ident(self, value, path):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            self.fail(path, "invalid identifier (use lowercase ASCII letters, digits, _ or -)")
            return ""
        return value

    def number(self, value, path, lo, hi):
        if type(value) is not int or not lo <= value <= hi:
            self.fail(path, f"expected integer from {lo} to {hi}")

    def ref(self, value, choices, path):
        if not isinstance(value, str) or (not self.allow_unresolved and value not in choices):
            self.fail(path, "unresolved reference")

    def texts(self, value, path):
        for i, text in enumerate(self.seq(value, path)):
            self.text(text, f"{path}[{i}]")

    def items(self, value, path):
        for i, stack in enumerate(self.seq(value, path)):
            if type(stack) not in (list, tuple) or len(stack) != 2:
                self.fail(path, "items need [name,quantity]")
                continue
            self.text(stack[0], f"{path}[{i}].name", limit=160)
            self.number(stack[1], f"{path}[{i}].quantity", 1, 999)

    def enemy(self, value, path):
        fields = ("name", "hp", "ac", "attack_bonus", "dmg", "loot")
        e = self.obj(value, fields, path, fields)
        self.text(e.get("name"), path + ".name", limit=100)
        for field, lo, hi in (("hp", 1, 10000), ("ac", 1, 30), ("attack_bonus", -10, 30)):
            self.number(e.get(field), path + "." + field, lo, hi)
        if not isinstance(e.get("dmg"), str) or not _DICE.fullmatch(e["dmg"]):
            self.fail(path + ".dmg", "unsupported damage dice")
        self.texts(e.get("loot"), path + ".loot")
        return e.get("name")

    def condition(self, cond, path, scenes, enemies, npcs, items, depth=0):
        cond = self.obj(cond, ("all", "turn_gte", "scene", "item", "enemy_dead", "npc_gone"), path)
        if len(cond) != 1 or depth > 8:
            self.fail(path, "unsupported or empty condition")
            return
        key, value = next(iter(cond.items()))
        if key == "all":
            children = self.seq(value, path)
            if not children:
                self.fail(path, "empty all condition")
            for i, child in enumerate(children):
                self.condition(child, f"{path}.all[{i}]", scenes, enemies, npcs, items, depth + 1)
        elif key == "turn_gte":
            self.number(value, path, 0, 1000000)
        else:
            self.ref(value, {"scene": scenes, "enemy_dead": enemies, "npc_gone": npcs, "item": items}[key], path)


def _strings(value):
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _contains_text(value, text):
    """Compare decoded text; JSON escaping must not hide multiline/quoted secrets."""
    if isinstance(value, str):
        return text in value
    if isinstance(value, dict):
        return any(_contains_text(child, text) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_text(child, text) for child in value)
    return False


def validate_definition(data, *, partial=False):
    """Validate types before merging; partial chunks may defer global references.

    Only complete definitions pass the default approval checks. Diagnostics
    contain schema paths and reasons, never supplied values or private text.
    """
    v = _Validator(allow_unresolved=partial)
    root = empty_definition()
    d = v.obj(data, root, "definition", root)
    if not d:
        return v.issues
    if type(d.get("schema_version")) is not int or d["schema_version"] != SCHEMA_VERSION:
        v.fail("schema_version", "unsupported definition version")
    if type(d.get("complete")) is not bool or (not partial and d["complete"] is not True):
        v.fail("complete", "conversion is incomplete")
    v.text(d.get("title"), "title", blank=partial, limit=200)
    setting_fields = ("tone", "premise", "facts")
    setting = v.obj(d.get("public_setting"), setting_fields, "public_setting", setting_fields)
    for key in ("tone", "premise"):
        v.text(setting.get(key), "public_setting." + key, blank=True)
    v.texts(setting.get("facts"), "public_setting.facts")
    v.texts(d.get("private_author_notes"), "private_author_notes")
    v.texts(d.get("conversion_issues"), "conversion_issues")
    if not partial and d.get("conversion_issues"):
        v.fail("conversion_issues", "resolve all reported conversion issues before approval")
    scenes, npcs, npc_ids, enemies, items = set(), set(), set(), set(), set()
    public = [setting, d.get("title")]
    scene_list = v.seq(d.get("scenes"), "scenes", 300)
    if not partial and not scene_list:
        v.fail("scenes", "at least one scene is required")
    scene_fields = ("id", "name", "description", "exits", "npcs", "ground_items", "hidden_items", "search_dc")
    for i, raw in enumerate(scene_list):
        path = f"scenes[{i}]"
        s = v.obj(raw, scene_fields, path, scene_fields)
        sid = v.ident(s.get("id"), path + ".id")
        if sid in scenes:
            v.fail(path, "duplicate scene identifier")
        scenes.add(sid)
        for key in ("name", "description"):
            v.text(s.get(key), path + "." + key)
        v.number(s.get("search_dc"), path + ".search_dc", 1, 30)
        for key in ("ground_items", "hidden_items"):
            v.items(s.get(key), path + "." + key)
            stacks = s.get(key) if isinstance(s.get(key), list) else []
            items.update(x[0] for x in stacks if type(x) in (list, tuple) and len(x) == 2 and isinstance(x[0], str))
        public.append({k: s.get(k) for k in ("id", "name", "description", "exits", "ground_items")})
        for j, raw_npc in enumerate(v.seq(s.get("npcs"), path + ".npcs")):
            p = f"{path}.npcs[{j}]"
            fields = ("id", "name", "desc", "disposition", "knows", "loot")
            n = v.obj(raw_npc, fields, p, fields)
            nid = v.ident(n.get("id"), p + ".id")
            name = v.text(n.get("name"), p + ".name", limit=100)
            if name in npcs or nid in npc_ids:
                v.fail(p, "duplicate NPC identity")
            npcs.add(name)
            npc_ids.add(nid)
            v.text(n.get("desc"), p + ".desc", blank=True)
            if n.get("disposition") not in ("friendly", "neutral", "wary", "hostile"):
                v.fail(p, "unsupported NPC disposition")
            v.texts(n.get("knows"), p + ".knows")
            v.texts(n.get("loot"), p + ".loot")
            items.update(_strings(n.get("loot")))
            public.append({k: n.get(k) for k in ("id", "name", "desc")})
    v.ref(d.get("starting_scene"), scenes, "starting_scene")
    for i, s in enumerate(scene_list):
        if not isinstance(s, dict):
            continue
        exits = s.get("exits")
        if type(exits) is not dict:
            v.fail(f"scenes[{i}].exits", "expected scene-to-label map")
        else:
            for dest, label in exits.items():
                v.ref(dest, scenes, f"scenes[{i}].exits")
                v.text(label, f"scenes[{i}].exits.label", limit=200)
    encounters = d.get("encounters")
    if type(encounters) is not dict:
        v.fail("encounters", "expected scene-to-enemies map")
        encounters = {}
    for i, (sid, foes) in enumerate(encounters.items()):
        v.ref(sid, scenes, f"encounters[{i}].scene")
        for j, foe in enumerate(v.seq(foes, f"encounters[{i}]")):
            name = v.enemy(foe, f"encounters[{i}][{j}]")
            if isinstance(name, str):
                if name in enemies:
                    v.fail(f"encounters[{i}][{j}]", "duplicate enemy name")
                enemies.add(name)
                public.append(name)  # /scene can show authored encounter identities.
            if isinstance(foe, dict):
                items.update(_strings(foe.get("loot")))
    plot_fields = ("objectives", "clocks", "beats", "triggers")
    plot = v.obj(d.get("plot"), plot_fields, "plot", plot_fields)
    beats = plot.get("beats")
    if type(beats) is not dict:
        v.fail("plot.beats", "expected beat map")
        beats = {}
    clocks = set()
    clock_list = v.seq(plot.get("clocks"), "plot.clocks")
    for i, c in enumerate(clock_list):
        p = f"plot.clocks[{i}]"
        fields = ("id", "start", "max", "on_expire")
        c = v.obj(c, fields, p, fields)
        cid = v.ident(c.get("id"), p + ".id")
        if cid in clocks:
            v.fail(p, "duplicate clock identifier")
        clocks.add(cid)
        v.number(c.get("start"), p + ".start", 0, 1000000)
        v.number(c.get("max"), p + ".max", 1, 1000000)
        if type(c.get("start")) is int and type(c.get("max")) is int and c["start"] >= c["max"]:
            v.fail(p, "clock start must precede max")
        v.ref(c.get("on_expire"), beats, p + ".on_expire")
    objectives = set()
    for i, o in enumerate(v.seq(plot.get("objectives"), "plot.objectives")):
        p = f"plot.objectives[{i}]"
        fields = ("id", "text", "public")
        o = v.obj(o, fields, p, fields)
        oid = v.ident(o.get("id"), p + ".id")
        if oid in objectives:
            v.fail(p, "duplicate objective identifier")
        objectives.add(oid)
        v.text(o.get("text"), p + ".text")
        if type(o.get("public")) is not bool:
            v.fail(p, "public must be a boolean")
        if o.get("public") is True:
            public.append(o.get("text"))
    # Register spawn identities first so triggers and secrets can reference them.
    for beat in beats.values():
        if not isinstance(beat, dict) or not isinstance(beat.get("effects"), list):
            continue
        for eff in beat["effects"]:
            if not isinstance(eff, dict):
                continue
            spawn = eff.get("npc_spawn")
            if isinstance(spawn, dict) and isinstance(spawn.get("name"), str):
                if spawn["name"] in npcs:
                    v.fail("plot.beats", "duplicate spawned NPC name")
                npcs.add(spawn["name"])
                items.update(_strings(spawn.get("loot")))
            spawn = eff.get("spawn")
            if isinstance(spawn, dict) and isinstance(spawn.get("enemy"), dict):
                name = spawn["enemy"].get("name")
                if isinstance(name, str):
                    if name in enemies:
                        v.fail("plot.beats", "duplicate spawned enemy name")
                    enemies.add(name)
                    items.update(_strings(spawn["enemy"].get("loot")))
    for i, (bid, raw) in enumerate(beats.items()):
        p = f"plot.beats[{i}]"
        v.ident(bid, p + ".id")
        b = v.obj(raw, ("text", "effects", "ending"), p, ("text", "effects"))
        v.text(b.get("text"), p + ".text", blank=True)
        if b.get("ending") is not None:
            v.text(b["ending"], p + ".ending", limit=200)
        for j, effect in enumerate(v.seq(b.get("effects"), p + ".effects")):
            _validate_effect(v, effect, f"{p}.effects[{j}]", scenes, npcs, clocks, items)
    trigger_list = v.seq(plot.get("triggers"), "plot.triggers")
    for i, t in enumerate(trigger_list):
        p = f"plot.triggers[{i}]"
        t = v.obj(t, ("beat", "when"), p, ("beat", "when"))
        v.ref(t.get("beat"), beats, p + ".beat")
        v.condition(t.get("when"), p + ".when", scenes, enemies, npcs, items)
    fired_by = [c.get("on_expire") for c in clock_list if isinstance(c, dict)]
    fired_by += [t.get("beat") for t in trigger_list if isinstance(t, dict)]
    if not partial and any(b not in fired_by for b in beats):
        v.fail("plot.beats", "some beats have no supported trigger or clock")
    secret_ids = set()
    for i, secret in enumerate(v.seq(d.get("secrets"), "secrets")):
        p = f"secrets[{i}]"
        fields = ("id", "text", "reveal")
        sec = v.obj(secret, fields, p, fields)
        sid = v.ident(sec.get("id"), p + ".id")
        if sid in secret_ids:
            v.fail(p, "duplicate secret identifier")
        secret_ids.add(sid)
        text = v.text(sec.get("text"), p + ".text")
        reveal = v.obj(sec.get("reveal"), ("npc", "beat"), p + ".reveal")
        if len(reveal) != 1:
            v.fail(p, "a secret needs one supported NPC or beat disclosure")
        for key, value in reveal.items():
            v.ref(value, npcs if key == "npc" else beats, p + ".reveal")
        if text and _contains_text(public, text):
            v.fail(p, "private secret appears in public setting or scene data")
    runtime = {k: value for k, value in d.items() if k not in ("private_author_notes", "conversion_issues")}
    for note in _strings(d.get("private_author_notes")):
        if note and _contains_text(runtime, note):
            v.fail("private_author_notes", "private note appears in runtime data")
    return list(dict.fromkeys(v.issues))


def _validate_effect(v, effect, p, scenes, npcs, clocks, items):
    keys = ("consume_item", "npc_move", "npc_disposition", "spawn", "clock", "npc_spawn", "cond", "inspire")
    eff = v.obj(effect, keys, p)
    if len(eff) != 1:
        v.fail(p, "each effect must contain one supported mechanic")
        return
    key, raw = next(iter(eff.items()))
    fields = {"consume_item": ("name", "qty"), "npc_move": ("npc", "to_scene"),
              "npc_disposition": ("npc", "to"), "spawn": ("scene", "enemy"),
              "clock": ("id", "set"), "npc_spawn": ("scene", "name", "desc", "disposition", "knows", "loot"),
              "cond": ("who", "name", "rounds"), "inspire": ("char",)}[key]
    spec = v.obj(raw, fields, p, fields)
    for field in ("scene", "to_scene"):
        if field in fields:
            v.ref(spec.get(field), scenes, p + "." + field)
    if "npc" in fields:
        v.ref(spec.get("npc"), npcs, p + ".npc")
    if key == "consume_item":
        v.ref(spec.get("name"), items, p + ".name")
        v.number(spec.get("qty"), p + ".qty", 1, 999)
    elif key in ("npc_spawn", "npc_disposition"):
        if spec.get("disposition" if key == "npc_spawn" else "to") not in ("friendly", "neutral", "wary", "hostile"):
            v.fail(p, "unsupported disposition")
        if key == "npc_spawn":
            v.text(spec.get("name"), p + ".name", limit=100)
            v.text(spec.get("desc"), p + ".desc", blank=True)
            v.texts(spec.get("knows"), p + ".knows")
            v.texts(spec.get("loot"), p + ".loot")
    elif key == "spawn":
        v.enemy(spec.get("enemy"), p + ".enemy")
    elif key == "clock":
        v.ref(spec.get("id"), clocks, p + ".id")
        v.number(spec.get("set"), p + ".set", 0, 1000000)
    elif key in ("cond", "inspire"):
        if spec.get("who" if key == "cond" else "char") not in ("", "party"):
            v.fail(p, "preauthored effects must target the party")
        if key == "cond":
            if spec.get("name") not in ("poisoned", "inspired"):
                v.fail(p, "unsupported condition")
            if spec.get("rounds") is not None:
                v.number(spec["rounds"], p + ".rounds", 1, 1000)


def build_adventure(approved):
    """Only validated definitions become runtime state; notes stay out of prompts."""
    d = deepcopy(approved["definition"])
    if validate_definition(d):
        from .persistence import StorageError
        raise StorageError("Invalid approved adventure definition")
    world = World()
    for raw in d["scenes"]:
        s = deepcopy(raw)
        for field in ("ground_items", "hidden_items"):
            s[field] = [tuple(stack) for stack in s[field]]
        for npc in s["npcs"]:
            npc.pop("id")
        world.add_scene(Scene(**s))
    world.current = d["starting_scene"]
    plot = deepcopy(d["plot"])
    for secret in d["secrets"]:
        reveal = secret["reveal"]
        if "npc" in reveal:
            holders = [n for s in world.scenes.values() for n in s.npcs if n["name"] == reveal["npc"]]
            holders += [e["npc_spawn"] for b in plot["beats"].values() for e in b["effects"]
                        if "npc_spawn" in e and e["npc_spawn"]["name"] == reveal["npc"]]
            for npc in holders:
                if secret["text"] not in npc["knows"]:
                    npc["knows"].append(secret["text"])
        else:
            beat = plot["beats"][reveal["beat"]]
            if secret["text"] not in beat["text"]:
                beat["text"] += ("\n" if beat["text"] else "") + secret["text"]
    encounters = {sid: [Enemy.make(**e) for e in foes] for sid, foes in d["encounters"].items()}
    g = Game({}, world, encounters)
    g.campaign_version = approved["version_id"]
    setting = d["public_setting"]
    rules = {"tone": setting["tone"], "world": {"canon": [setting["premise"], *setting["facts"]]}}
    return g, rules, plot
