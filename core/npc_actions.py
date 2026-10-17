"""Exact NPC request grammars, retaining ordering and existing identities."""
import re

from .intent import Intent, _action_text


def _name(token, names):
    exact = [n for n in names if token == n]
    matches = exact or [n for n in names if token and token in n]
    return matches[0] if len(matches) == 1 else ""


def observation_plan(g, text):
    """Observe then ask is two actions; the question stays player dialogue."""
    _, body = _action_text(text, g.party)
    hit = re.fullmatch(r"(?P<target>[^。！？、，]+?)を(?:よく)?観察してから(?P<question>.+?)を聞く[。.!！]*", body)
    if not hit:
        return None
    target = _name(hit["target"].strip(), [n["name"] for n in g.scene_npcs()])
    if not target:
        return None
    return {"relationship": "linked", "goal": "talk", "steps": [
        {"action": "observe", "target": target},
        {"action": "talk", "target": target, "utterance": hit["question"].strip()}]}


def return_to_npc(g, text):
    actor, body = _action_text(text, g.party)
    hit = re.fullmatch(r"(?:一人で)?(?P<target>[^。！？、，]+?)の(?:所|ところ)に戻る[。.!！]*", body)
    if not hit:
        return None
    locations = {}
    for sid in g.world.scenes:
        for name in [n["name"] for n in g.scene_npcs(sid)] + list(g.scene_enemies(sid)):
            locations.setdefault(name, set()).add(sid)
    name = _name(hit["target"].strip(), locations)
    if not name or len(locations[name]) != 1:
        return None
    sid = next(iter(locations[name]))
    if sid != g.world.current and sid in g.world.here.exits:
        return Intent("move", actor=actor, destination=sid, raw=text)
    return Intent("meta", actor=actor, raw=text, args={"locate_npc": name, "location": sid})
