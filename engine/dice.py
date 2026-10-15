"""Dice rolling for the D&D DM bot.

Supports standard notation: d20, 2d6+3, 4d6kh3 (keep highest), 4d6kl1,
adv / dis shorthand for d20, and combined expressions like 2d6+1d4+2.
"""

import random
import re

# A single die term: [N]d<SIDES>[kh|kl][K]  e.g. "2d6", "d20", "4d6kh3"
_TERM = re.compile(
    r"(?P<count>\d*)d(?P<sides>\d{1,4})(?:(?P<keep>kh|kl)(?P<keepn>\d{1,3})?)?",
    re.IGNORECASE,
)
# Full expression: terms separated/modified by + and -
_EXPR = re.compile(r"(?i)([+-]?)\s*(\d*d\d{1,4}(?:kl\d{1,3}|kh\d{1,3}|kh|kl)?|\d+)")

_ADV_WORDS = {"adv": "d20kh2", "advantage": "d20kh2", "dis": "d20kl2", "disadvantage": "d20kl2"}


def _roll_term(count: int, sides: int, keep: str | None, keepn: str | None,
               rng=None) -> tuple[int, list[int]]:
    count = max(1, count)
    sides = min(max(2, sides), 1000)
    rng = rng or random
    dice = [rng.randint(1, sides) for _ in range(count)]
    if keep:
        n = int(keepn) if keepn else 1
        if keep.lower() == "kh":
            kept = sorted(dice, reverse=True)[:n]
        else:
            kept = sorted(dice)[:n]
        return sum(kept), dice
    return sum(dice), dice


def roll_expr(expr: str, *, rng=None) -> tuple[int, str]:
    """Roll a dice expression like '2d6+3' or '4d6kh3'.

    Returns (total, breakdown) e.g. (9, "4 + 3 + 2").
    Raises ValueError for invalid input.
    """
    expr = _ADV_WORDS.get(expr.strip().lower(), expr.strip())
    expr = expr.replace(" ", "")
    if not expr:
        raise ValueError("empty expression")

    total = 0
    parts: list[str] = []
    sign = 1
    pos = 0
    matched_any = False

    for m in _EXPR.finditer(expr):
        if m.start() != pos and m.start() != pos + 1:
            # gap in the expression -> malformed
            if not expr[: m.start()].replace(" ", "").rstrip("+-"):
                pos = m.end()
                continue
            raise ValueError(f"cannot parse: {expr}")
        if m.group(1):  # the captured +/- sign of this term
            sign = -1 if m.group(1) == "-" else 1
        elif m.start() > 0 and expr[m.start() - 1] in "+-":
            sign = -1 if expr[m.start() - 1] == "-" else 1
        else:
            sign = 1
        pos = m.end()
        matched_any = True
        token = m.group(2)
        if "d" not in token.lower():
            value = int(token) * sign
            total += value
            parts.append(f"{'-' if value < 0 else '+'}{abs(value)}".lstrip("+") if parts else str(value))
        else:
            tm = _TERM.fullmatch(token)
            if not tm:
                raise ValueError(f"bad die term: {token}")
            count = int(tm.group("count") or 1)
            if count > 100:
                raise ValueError("too many dice")
            value, dice = _roll_term(
                count, int(tm.group("sides")), tm.group("keep"), tm.group("keepn"), rng
            )
            total += sign * value
            detail = ",".join(str(d) for d in dice)
            label = token.lower()
            if sign < 0:
                label = "-" + label
            parts.append(f"{label} [{detail}]")

    if not matched_any:
        raise ValueError(f"no dice found in: {expr}")
    return total, " + ".join(parts)


# Matches dice the LLM is instructed to emit inline: [[2d6+3]] — also tolerates
# single brackets [2d6+3] and full-width brackets 【2d6+3】; the captured text
# must be pure dice syntax.
_LLM_ROLL = re.compile(
    r"(?:\[{1,2}|【)\s*([+-]?\d*d\d{1,4}(?:kl\d{1,3}|kh\d{1,3}|kh|kl)?(?:\s*[+-]\s*\d+)?)\s*(?:\]{1,2}|】)",
    re.IGNORECASE,
)


def resolve_llm_rolls(text: str) -> tuple[str, list[tuple[str, int]]]:
    """Replace [[dice]] markers in LLM narration with rolled results.

    Returns (new_text, [(expr, total), ...]) so the adapter can append a dice summary.
    """
    results: list[tuple[str, int]] = []

    def repl(m: re.Match) -> str:
        expr = m.group(1).strip()
        try:
            total, breakdown = roll_expr(expr)
        except ValueError:
            return m.group(0)  # leave malformed rolls untouched
        results.append((expr, total))
        return f"🎲 *{expr}* → **{total}**  ({breakdown})"

    return _LLM_ROLL.sub(repl, text), results


def quick_roll(expr: str) -> str:
    total, breakdown = roll_expr(expr)
    return f"🎲 *{expr}* → **{total}**  ({breakdown})"
