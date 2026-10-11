"""v4 intent layer: structured intents + the deterministic (no-LLM) parser.

The digestor LLM (P2) will produce the same Intent objects from free-form
text; the command syntax here is the degraded-mode / test-harness path and
the ground truth for the schema.
"""

import re
from dataclasses import dataclass, field

# canonical action vocabulary the engine understands
ACTIONS = ("attack", "move", "use", "cast", "talk", "search",
           "rest", "give", "check", "pass", "creative", "chat",
           "take", "claim", "meta", "skill", "defend", "escape",
           "observe", "help")

_VERBS = {
    "attack": ("攻擊", "打", "劈", "斬", "砍", "揼", "踢", "篤", "射",
               "捅", "殺", "郁手", "attack", "hit"),
    "take": ("拿起", "拾起", "撿起", "執起", "拿", "拾", "撿", "執", "攞",
             "take", "pick"),
    "move": ("去", "前往", "走到", "go", "move"),
    "use": ("使用", "用", "use"),
    "cast": ("施展", "施法", "施放", "cast"),
    "talk": ("說", "喊", "講", "say", "talk", "shout"),
    "search": ("搜索", "搜尋", "調查", "檢查", "search", "周圍望"),
    "rest": ("休息", "rest"),
    "give": ("給", "give"),
    "check": ("檢定", "骰", "roll", "check"),
    "pass": ("等待", "跳過", "pass", "wait"),
    "skill": ("技能", "skill", "使出"),
    "defend": ("防禦", "防御", "閃避", "defend", "dodge"),
    "escape": ("撤退", "逃走", "走為上著", "escape", "flee", "retreat"),
    "observe": ("觀察", "observe", "study", "研究敵人"),
    "help": ("幫助", "幫手", "支援", "aid", "help"),
}

# Cantonese aspect markers and particles to strip before verb matching
_PARTICLES = "咗緊吓啦喎啩嘅囉咯嘛"


@dataclass
class Intent:
    action: str
    actor: str = ""          # "" = the caller's / current acting character
    target: str = ""
    item: str = ""
    qty: int = 1
    spell: str = ""
    destination: str = ""
    utterance: str = ""
    ability: str = ""
    skill: str = ""
    raw: str = ""
    confidence: float = 1.0  # digestor fills this; low -> engine asks back
    wants_help: bool = False  # digestor: player is asking for guidance
    args: dict = field(default_factory=dict)


def _strip_particles(text: str) -> str:
    """Remove Cantonese aspect markers so verb matching works:
    執咗把劍 → 執把劍, 打緊佢 → 打佢."""
    return re.sub(f"[{_PARTICLES}]", "", text)


def _starts_with_verb(text: str):
    """(action, rest) if text opens with a known verb, else None.
    Empty tails are fine — object-less verbs like 搜索/rest/pass are legal.
    Cantonese particles are stripped before matching."""
    cleaned = _strip_particles(text)
    for action, words in _VERBS.items():
        for w in words:
            if cleaned.startswith(w):
                # find the original position to get the right rest
                idx = text.find(w)
                if idx >= 0:
                    return action, text[idx + len(w):].lstrip(" ：:,，,")
                return action, cleaned[len(w):].lstrip(" ：:,，,")
    return None


def parse_command(text: str, party_names=()) -> Intent | None:
    """Deterministic keyword parser (degraded mode). Grammar:
        [角色] 動詞 賓語...
    e.g.「大力蕉 攻擊 哥布林①」「依思 執起把劍」「go 酒窖」
    Handles Cantonese particles (咗/緊/吓) and mixed English."""
    t = (text or "").strip()
    if not t:
        return None
    actor = ""
    for name in party_names:
        if t.startswith(name):
            actor = name
            t = t[len(name):].lstrip(" ：:,，,")
            break
    # strip 我用/我 prefix before the verb
    t = re.sub(r"^我用?", "", t).lstrip()
    hit = _starts_with_verb(t)
    if hit is None:
        return None
    action, rest = hit
    it = Intent(action=action, actor=actor, raw=text)
    if action == "move":
        it.destination = rest
    elif action == "talk":
        it.utterance = rest
    elif action in ("use", "give", "take"):
        it.item = rest
        if action == "use":
            # 「使用 治療藥水 依思」— a trailing party member = target
            # (administer a potion, works on downed allies)
            for name in party_names:
                if rest.endswith(name) and len(rest) > len(name):
                    it.item = rest[:-len(name)].strip(" ：:,，,給餵喂")
                    it.args["to"] = name
                    break
    elif action == "cast":
        it.spell = rest
    elif action in ("attack",):
        it.target = rest
    elif action == "rest":
        it.args["kind"] = "long" if rest in ("長休", "long") else "short"
    elif action == "check":
        it.ability = rest
    elif action == "skill":
        # 「技能 潛行」／「技能 洞察 船長」— skill word (+ optional target)
        parts = rest.split(None, 1) if rest else []
        if len(parts) == 2:
            it.skill, it.target = parts
        elif parts:
            it.skill = parts[0]
    elif action == "observe":
        it.target = rest  # 「觀察 哥布林①」— find the weak spot on X
    elif action == "help":
        it.target = rest  # 「幫助 依思」— aid that character
    return it
