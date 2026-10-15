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

# These verbs identify a method, so they are safe degraded-mode candidates.
# Robbery alone does not: it could mean stealth, coercion, or violence.
_SKILL_VERBS = {
    "sleight of hand": ("偷偷扒竊", "偷偷扒窃", "扒竊", "扒窃", "偷竊", "偷窃"),
    "intimidation": ("威嚇", "威吓", "恐嚇", "恐吓"),
    "persuasion": ("說服", "说服", "遊說", "游说"),
    "stealth": ("潛行", "潜行", "匿埋"),
}
_ROBBERY = re.compile(r"^(?:洗劫|搶劫|抢劫|打劫|掠奪|掠夺|rob\b|mug\b)\s*(.*)$", re.I)
_DUEL_VERB = r"(?:決鬥|决斗|單挑|单挑|交戰|交战|戰鬥|战斗|duel\b)"
_DUEL_PATTERNS = (
    re.compile(r"^(?:與|与|和|跟|同|向|對|对)\s*(?P<target>[^，。！？,;；]+?)\s*"
               r"(?:(?:發起|发起|進行|进行)\s*)?" + _DUEL_VERB + r"[。！!]*$", re.I),
    re.compile(r"^" + _DUEL_VERB + r"\s*(?P<target>[^，。！？,;；]+?)[。！!]*$", re.I),
)
# A request for practice or a hypothetical fight must not become a damaging attack.
_DUEL_QUALIFIER = re.compile(
    r"不|沒|没|唔|別|别|拒絕|拒绝|如果|假如|是否|[?？]|"
    r"練習|练习|切磋|模擬|模拟|友誼|友谊|非致命|點到為止|点到为止")
_ENTRY_MOVE = re.compile(
    r"^(?:登上|走上|進入|进入|走進|走进|踏入|爬上|climb aboard\b|board\b|enter\b)"
    r"\s*(?P<destination>[^，。！？,;；]+?)[。！!]*$", re.I)
_ENTRY_QUALIFIER = re.compile(r"不|沒|没|唔|別|别|如果|假如|是否|[?？]|然後|然后|並|\b(?:and|then|if)\b", re.I)


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


def _action_text(text: str, party_names=()) -> tuple[str, str]:
    t = (text or "").strip()
    actor = ""
    for name in sorted(party_names, key=len, reverse=True):
        if t.startswith(name):
            actor = name
            t = t[len(name):].lstrip(" ：:,，,")
            break
    t = re.sub(r"^我(?:(?:想要|想|要|嘗試|尝试|試下|試吓)\s*)?用?", "", t).lstrip()
    return actor, t


def robbery_clarification(text: str, party_names=(), npc_names=()) -> str:
    """Ask for a method for a bare robbery request; infer no roll or loot."""
    _, t = _action_text(text, party_names)
    hit = _ROBBERY.fullmatch(t.rstrip("。.!！?？"))
    if not hit:
        return ""
    target = hit[1].strip()
    matches = [name for name in npc_names if target and target in name]
    if target and len(matches) != 1:
        return ""  # Let the action operator interpret richer descriptions.
    name = matches[0] if matches else "對方"
    return (f"❓ 你想怎樣洗劫{name}：偷偷扒竊，還是威嚇對方？"
            f"請描述方法，例如 `/explore 偷偷扒竊{name}` 或 `/explore 威嚇{name}`。"
            "尚未擲骰，也未取得任何財物。")


def parse_command(text: str, party_names=()) -> Intent | None:
    """Deterministic keyword parser (degraded mode). Grammar:
        [角色] 動詞 賓語...
    e.g.「大力蕉 攻擊 哥布林①」「依思 執起把劍」「go 酒窖」
    Handles Cantonese particles (咗/緊/吓) and mixed English."""
    actor, t = _action_text(text, party_names)
    if not t:
        return None
    if _ROBBERY.match(t):
        return None  # In particular, 打劫 must never match the attack verb 打.
    entry = _ENTRY_MOVE.fullmatch(t) if not _ENTRY_QUALIFIER.search(t) else None
    if entry:
        return Intent("move", actor=actor, destination=entry["destination"].strip(), raw=text)
    if not _DUEL_QUALIFIER.search(t):
        for pattern in _DUEL_PATTERNS:
            duel = pattern.fullmatch(t)
            if duel:
                return Intent("attack", actor=actor, target=duel["target"].strip(), raw=text)
    for skill, verbs in _SKILL_VERBS.items():
        for verb in verbs:
            if t.startswith(verb):
                return Intent("skill", actor=actor, skill=skill,
                              target=t[len(verb):].lstrip(" ：:,，,"), raw=text)
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
