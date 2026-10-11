"""v4 guards: v3's hard-won defenses, adapted for the engine-driven
architecture. Each guard is a pure function — deterministic, no LLM.

Ported from v3's DESIGN.md §3.7 and dm.py, prioritized by the pain that
taught each lesson.
"""

import re

# ---- placeholder names (v3: 名諱防漂移) ----
# Real character names never reach the narrator; [PCn] comes back as the
# DB name before anything is shown to players.


def make_placeholder_map(party_names: list) -> dict:
    """{real_name: '[PCn]'} longest-first, for outbound substitution."""
    m = {}
    for i, name in enumerate(party_names, 1):
        m[name] = f"[PC{i}]"
    return dict(sorted(m.items(), key=lambda kv: -len(kv[0])))


def make_restore_map(party_names: list) -> dict:
    """{slot_number: real_name} for inbound restoration (int keys,
    matching the regex capture group)."""
    return {i: n for i, n in enumerate(party_names, 1)}


def map_out(text: str, pmap: dict) -> str:
    for name, tok in pmap.items():
        text = text.replace(name, tok)
    return text


_PC_TOKEN_RE = re.compile(r"(?:\[\s*|\b)PC\s*(\d{1,2})(?:\s*\]|\b)")


def map_in(text: str, rmap: dict) -> str:
    """Restore [PCn] back to real names (tolerantly spaced/bracketed)."""
    def sub(m):
        return rmap.get(int(m.group(1)), m.group(0))
    return _PC_TOKEN_RE.sub(sub, text)


# ---- fake-dice scrubbing (v3: 反 vibe 骰網) ----
# The narrator must NEVER contain dice notation, numbers-as-results, or
# verdict arithmetic — those are engine-only vocabulary.

_FAKE_DICE = re.compile(
    r"\s*\*{0,2}(?:the )?(?:dice|die|roll) result is? \d{1,3}\*{0,2}"
    r"|\brolls?(?:ed)? (?:a |an )?\d{1,2}\b"
    r"|(?:骰|擲)出(?:了)?[:：]?\s*\d{1,3}"
    r"|(?:骰|擲)了[:：]?\s*\d{1,3}"
    r"|🎲\s*\d{1,3}\b", re.IGNORECASE)

_FAKE_DICE_LINE = re.compile(r"^[ \t]*🎲\s*\d{1,3}\s*$", re.MULTILINE)

_FAKE_VERDICT = re.compile(
    r"^[ \t]*🎲?\s*(?:攻擊判定|擲骰結果|攻擊骰|檢定結果)[：:]\s*"
    r"\d{1,2}\s*\+\s*\d{1,2}\s*=\s*\d{1,2}[^\n]*$"
    r"|^[ \t]*🎲\s*\d{1,2}\s*\+\s*\d{1,2}\s*=\s*\d{1,2}[^\n]*$"
    r"|^[ \t]*🎲?\s*\d*d\d{1,3}"
    r"(?:[ \t]*[+-][ \t]*\d{1,3})*[ \t]*(?:→|->|=)[ \t]*\d{1,4}[ \t]*$",
    re.MULTILINE)

_DICE_NOTATION = re.compile(
    r"\[{2}[^\]]+\]{2}"
    r"|\b\d?d\d{1,3}(?:[+-]\d{1,2})?\b")


def scrub_narration(text: str) -> tuple[str, bool]:
    """Remove all dice/verdict/mechanical notation from narrator output.
    Returns (clean_text, was_scrubbed)."""
    orig = text
    text = _FAKE_DICE.sub("", text)
    text = _FAKE_DICE_LINE.sub("", text)
    text = _FAKE_VERDICT.sub("", text)
    text = _DICE_NOTATION.sub("", text)
    text = re.sub(r"\*{2,}\s*\*{2,}", "", text)  # emptied ** wrappers
    return text.strip(), text != orig


# ---- repetition guard (v3: 重複死亡螺旋) ----


def is_repetition(new_text: str, recent_texts: list,
                  threshold: float = 0.80) -> bool:
    """True if the new narration is near-identical to a recent one."""
    if not new_text or not recent_texts:
        return False
    from difflib import SequenceMatcher
    for old in recent_texts[-3:]:
        if len(old) < 80 or len(new_text) < 80:
            continue  # short texts can't meaningfully repeat-spiral
        if SequenceMatcher(None, new_text[:400], old[:400]).ratio() > threshold:
            return True
    return False


# ---- language check (v3: 三層語言防禦) ----
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def is_chinese(text: str) -> bool:
    if not text:
        return False
    return len(_CJK_RE.findall(text)) > len(_LATIN_RE.findall(text))


# ---- player input scrubbing (v3: scrub_player_input) ----
_INJECTION = [
    (re.compile(r"(?:\[\s*|\b)PC\s*\d{1,2}(?:\s*\]|\b)",
                re.IGNORECASE), "〔玩家角色〕"),
    (re.compile(r"SYSTEM\s*(?:CHECK\s*)?VERDICT", re.IGNORECASE), "〔已過濾〕"),
    (re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)",
                re.IGNORECASE), "〔filtered〕"),
    (re.compile(r"忽略(?:上面|以上|先前|之前)的?(?:指示|指令|規則|設定)"),
     "〔已過濾〕"),
]


def scrub_input(text: str) -> tuple[str, bool]:
    """Neutralize injection attempts in player freeform text."""
    t = text or ""
    changed = False
    for rx, rep in _INJECTION:
        new = rx.sub(rep, t)
        if new != t:
            changed = True
            t = new
    return t, changed


# ---- item inventory guard (post-narration) ----
# The narrator is told which items exist. If it bolds an item name,
# the engine validates it against the known inventory. Unknown bolded
# items are stripped (generic terms stay as prose).

_BOLD_ITEM_RE = re.compile(r"\*\*([^*]{2,40})\*\*")


def validate_narration_items(text: str, known_items: list) -> tuple[str, list]:
    """Check bolded item mentions against the known inventory.
    Returns (clean_text, [valid_item_names_found]).
    Unknown bold items are unbolded (kept as prose, not stripped)."""
    valid = []

    def check(m):
        name = m.group(1).strip()
        # is this a known item (fuzzy match)?
        for k in known_items:
            if name == k or name in k or k in name:
                valid.append(k)
                return m.group(0)  # keep the bold — it's a real item
        return name  # unbold: not a real item, keep as prose

    clean = _BOLD_ITEM_RE.sub(check, text)
    return clean, valid
