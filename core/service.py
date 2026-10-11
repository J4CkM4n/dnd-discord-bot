"""v4 service: digestor → engine → narrator, with full state persistence.

Hosted inside the dm-bot process (one Discord token = one gateway); the
V4_CHANNEL_ID env routes a dedicated playground channel to this flow
while every other channel keeps the v3 DM.
"""

import json
import logging
import os
import re
from dataclasses import asdict

log = logging.getLogger("dnd-bot")

# player words that mean "I need help / what now" — triggers a narrator
# directive to summarize and give concrete next steps
_HELP_RE = re.compile(
    r"幫|怎樣|怎麼|如何|點算|點樣|點做|做(咩|什麼|麽|么)|下一步|提示|線索|"
    r"意見|建議|去哪|去邊|邊度|hint|help|what now|what should|now what|"
    r"should we|advice", re.I)


def load_gamerules(channel_id: str = "", data_dir: str = "") -> dict:
    """gamerules.json — the single source of truth for world tone/canon
    and narrative constraints. Per-channel override first
    (data/gamerules_<channel>.json), then the repo-root default."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = []
    if channel_id and data_dir:
        paths.append(os.path.join(data_dir,
                                  f"gamerules_{channel_id}.json"))
    paths.append(os.path.join(root, "gamerules.json"))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


def world_block(rules: dict) -> str:
    """Compact prompt block distilled from gamerules.json: the tone the
    narration must carry and the canon facts it must never contradict."""
    if not rules:
        return ""
    bits = []
    if rules.get("tone"):
        bits.append(f"世界基調：{rules['tone']}")
    canon = (rules.get("world") or {}).get("canon") or []
    if canon:
        bits.append("世界事實（必須遵守，絕不得矛盾）：\n"
                    + "\n".join(f"- {c}" for c in canon))
        bits.append(
            "⚠️ 世界事實只是背景知識——不代表那些人／物在當前場景！"
            "劇情物品（項鍊、寶物、武器、藥水等）的位置由引擎決定："
            "絕不得讓它們在敘事中出現、掉落、滾出、或看似可被取得——"
            "玩家因此撲空＝敘事的錯。")
    return "\n".join(bits)

from .cli import META, build_demo_game
from .digestor import Digestor
from .director import canonize_scene, classify
from .director_llm import DirectorLLM
from .intent import Intent
from .ledger import Ledger, Entry
from .narrator import Narrator
from .plot import load_plot, new_state, tick as plot_tick
from .rules_core import resolve
from .turn import Game
from .world import Enemy, Scene, World

# a narration that ends WITHOUT one of these was cut mid-sentence
# (max_tokens cutoff / stream drop) -> /continue must finish it
_END_PUNCT = "。．.!！?？…」』）)\"'"


class V4Service:
    def __init__(self, data_dir: str, llm_url: str,
                 digest_model: str = "qwen2.5:7b-instruct",
                 narr_model: str = "qwen3.5:35b",
                 channel_id: str = "",
                 narr_think: bool | None = False,
                 digest_think: bool | None = None):
        # per-channel state: each Discord table gets its own game
        fname = f"v4_state_{channel_id}.json" if channel_id else "v4_state.json"
        self.path = os.path.join(data_dir, fname)
        self.channel_id = channel_id
        self.digestor = Digestor(llm_url, digest_model, think=digest_think)
        self.narrator = Narrator(llm_url, narr_model, think=narr_think)
        # the generative Director (same small fast model as digestor)
        self.director = DirectorLLM(llm_url, digest_model)
        # narration that never finished (narrator silent, or prose cut
        # mid-sentence): /continue resumes it before anything else.
        # (init BEFORE _load — the saved blob may restore it)
        self._unfinished = None
        # game rules — per-channel override (data/gamerules_<cid>.json)
        # over the repo-root default; world tone + canon for the narrator
        self.rules = load_gamerules(channel_id, data_dir)
        # plot spine (plot.json) — clocks + trigger-fired beats
        self.plot = load_plot()
        self.game: Game = self._load() or build_demo_game()
        self.plot_state = self._plot_state()
        self.pending = None  # Intent awaiting the player's /confirm
        self._recent_narrations: list[str] = []  # v3: repetition guard

    # ---------- persistence ----------

    def _save(self) -> None:
        g = self.game
        blob = {
            "party": g.party,
            "world": {
                "scenes": {sid: {
                    "name": s.name, "description": s.description,
                    "exits": s.exits, "npcs": s.npcs,
                    "ground_items": [list(x) for x in s.ground_items],
                    "search_dc": s.search_dc,
                    "hidden_items": [list(x) for x in s.hidden_items],
                    "fire": s.fire}
                    for sid, s in g.world.scenes.items()},
                "current": g.world.current},
            "encounters": {sid: [asdict(f) for f in foes]
                           for sid, foes in g.encounters.items()},
            "enemies": {n: asdict(f) for n, f in g.enemies.items()},
            "combat": {"order": g.combat.order, "idx": g.combat.idx,
                       "round": g.combat.round, "acted": g.combat.acted},
            "inventory": g.inventory,
            "turn": g.ledger.turn,
            "entries": [asdict(e) for e in g.ledger.entries],
            "unfinished": self._unfinished,
            "plot": self.plot_state,
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False, indent=1)

    def _load(self) -> Game | None:
        if not os.path.exists(self.path):
            return None
        with open(self.path, encoding="utf-8") as f:
            blob = json.load(f)
        world = World()
        for sid, s in blob["world"]["scenes"].items():
            world.scenes[sid] = Scene(
                sid, s["name"], s["description"], exits=s["exits"],
                npcs=s.get("npcs", []),
                ground_items=[tuple(x) for x in s.get("ground_items", [])],
                search_dc=s.get("search_dc", 12),
                hidden_items=[tuple(x) for x in s.get("hidden_items", [])],
                fire=s.get("fire", False))
        world.current = blob["world"]["current"]
        g = Game(blob["party"], world)
        for sid, foes in blob.get("encounters", {}).items():
            g.encounters[sid] = [Enemy(**f) for f in foes]
        for n, f in blob.get("enemies", {}).items():
            g.enemies[n] = Enemy(**f)
        c = blob.get("combat") or {}
        g.combat.order = c.get("order", [])
        g.combat.idx = c.get("idx", 0)
        g.combat.round = c.get("round", 1)
        g.combat.acted = c.get("acted", [])
        g.inventory = blob.get("inventory") or {n: [] for n in g.party}
        g.ledger.turn = blob.get("turn", 0)
        for e in blob.get("entries", []):
            g.ledger.entries.append(Entry(**e))
        self._unfinished = blob.get("unfinished")
        self._loaded_plot = blob.get("plot")
        return g

    def _plot_state(self) -> dict:
        st = getattr(self, "_loaded_plot", None)
        if isinstance(st, dict) and "clocks" in st and "fired" in st:
            return st
        return new_state(self.plot)

    # ---------- admin helpers ----------

    def admin_give(self, char: str, item: str, qty: int = 1) -> str:
        """Admin grants items directly (ledgered). Returns the render."""
        g = self.game
        if char not in g.party:
            return f"❓ 沒有角色「{char}」"
        g.give_item(char, item, max(1, int(qty or 1)))
        g.ledger.add("admin", "item", f"管理員給了 {char} {item}×{qty}")
        self._save()
        inv = "、".join(f"{n}×{q}" for n, q in g.inventory.get(char, []))
        return f"🎒 {char} 現在攜帶：{inv or '（空）'}"

    def table_talk(self, text: str, author: str = "") -> None:
        """Plain channel chatter: ignore completely. No ledger, no save."""
        pass

    # ---------- one turn ----------

    async def handle(self, text: str, author: str = "",
                     user_id: str = "",
                     structured: bool = False,
                     on_delta=None,
                     on_resolved=None,
                     admin_actor: str = "") -> tuple[list, str]:
        """Free-form text in → (engine lines, narration). Never raises.
        user_id: Discord uid — the engine enforces that only the character's
        owner can act as that character.
        structured: True = the text came from /combat (dropdown selections
        are already unambiguous) — skip the digestor entirely, construct
        the Intent deterministically, confidence=1.0, no confirmation.
        admin_actor: admin commands act as ANY character — overrides the
        parsed actor and skips the ownership check.
        on_resolved: async callback(lines) fired the moment the engine
        verdict exists — BEFORE narration starts, so the adapter can post
        the verdict instantly and stream the prose into its own message
        (v3 word-by-word UX)."""
        g = self.game
        t = (text or "").strip()
        if not t:
            return [], ""
        if t in META:  # deterministic queries skip the LLM entirely
            return META[t](g), ""
        if admin_actor and admin_actor not in g.party:
            return ([f"❓ 沒有角色「{admin_actor}」"], "")
        it = None
        if self.pending is not None:
            low = t.lower().strip(" ！!。.")
            if low in ("確認", "确认", "confirm", "對", "对", "yes", "y",
                       "好", "ok"):
                it = self.pending
                it.args["confirmed"] = True
            self.pending = None  # anything else = changed their mind
        # scrub input BEFORE the digestor sees it (fix: was after)
        from .guards import scrub_input
        clean_t, was_scrubbed = scrub_input(t)
        if was_scrubbed:
            g.ledger.add(author or "?", "deny",
                         "已過濾可疑指令文字", reason="injection")
        t = clean_t
        if t.startswith(("((", "//")):  # explicit out-of-character
            from .intent import Intent
            it = Intent(action="chat", utterance=t.lstrip("(/ "), raw=t)
        elif structured:
            # /combat selections: deterministic parse, confidence=1.0
            from .intent import parse_command
            it = parse_command(t, party_names=list(g.party))
            if it is not None:
                it.confidence = 1.0
            else:
                it = await self.digestor.digest(
                    t, list(g.party), g.world.here.name,
                    list(g.world.here.exits.values()),
                    known_targets=list(g.enemies) +
                    [n["name"] for n in g.world.here.npcs] + list(g.party))
                it.confidence = 1.0  # structured origin — trust it
        else:
            it = await self.digestor.digest(
                    t, list(g.party), g.world.here.name,
                    list(g.world.here.exits.values()),
                    known_targets=list(g.enemies) +
                    [n["name"] for n in g.world.here.npcs] + list(g.party))
        # digestor safety nets (live-game lessons):
        # 1. talk with no words — the digestor found the NPC but dropped
        #    WHAT was asked; keep the player's own words (the narrator
        #    answers them and the ledger fact stays meaningful)
        if it is not None and it.action == "talk" and not it.utterance:
            it.utterance = (it.raw or "")[:120]
        # 2. a move to nowhere（「下一步去哪？」misread as move）is a help
        #    question, not an illegal move — re-route to meta so the
        #    engine answers with exits + suggestions instead of denying
        if it is not None and it.action == "move" and not it.destination:
            it.action = "meta"
        # 3. rest kind: the LLM path doesn't emit args — sniff the kind
        #    from the player's words（長休/long rest）so a long rest is
        #    never silently downgraded to a short one
        if it is not None and it.action == "rest" \
                and not it.args.get("kind"):
            raw = f"{it.raw or ''} {it.utterance or ''}"
            it.args["kind"] = "long" if re.search(
                r"長休|长休|long\s*rest|全休|過夜|过夜", raw, re.I) \
                else "short"
        # 4. 「使用 醫藥／醫術」 is a SKILL attempt, not an item use —
        #    the 使用 verb pulls the digestor toward `use`; when the
        #    "item" names a skill, reroute deterministically
        if it is not None and it.action == "use":
            from engine.charlib import normalize_skill
            _ab, _sk = normalize_skill(it.item or "")
            if _sk:
                it.action = "skill"
                it.skill = _sk
                it.target = it.target or it.args.pop("to", "")
        # 5. UNPLANNED PLACES (v5 Director): walking somewhere that
        #    isn't an exit asks the Director LLM to propose the scene —
        #    the ENGINE gate canonizes it (typed, capped, exits back).
        #    This is why a non-narrative LLM layer exists.
        if it is not None and it.action == "move" and it.destination \
                and not g.world.find_exit(it.destination):
            tone = (self.rules or {}).get("tone", "奇幻")
            canon = ((self.rules or {}).get("world") or {}).get("canon", [])
            proposed = await self.director.gen_scene(
                tone, g.world.here.name, g.world.here.description,
                it.destination, gamerules_canon=canon)
            if proposed and canonize_scene(g, proposed):
                # point the move at the canonized name so find_exit hits
                it.destination = proposed["name"]
        # 6. UNPLANNED DISCOVERY: a search with nothing authored and no
        #    named target may surface an LLM-proposed inert prop instead
        #    of the static ambient table
        if it is not None and it.action == "search" \
                and not g.world.here.hidden_items:
            from .director import scan_prop
            text_blob = f"{it.raw or ''} {it.utterance or ''} {it.item or ''}"
            if not scan_prop(text_blob) and g._random.random() < 0.35:
                tone = (self.rules or {}).get("tone", "奇幻")
                proposal = await self.director.gen_item(
                    tone, g.world.here.name, g.world.here.description,
                    it.utterance or it.raw or "搜索")
                s = g.world.here
                dup = any(proposal and (proposal["name"] in n
                                        or n in proposal["name"])
                          for n, _ in s.ground_items + s.hidden_items)
                if proposal and not dup \
                        and classify(proposal["name"]) == "prop":
                    s.hidden_items.append((proposal["name"], 1))
                    g.ledger.add("director", "materialize",
                                 f"搜索途中世界浮現了 {proposal['name']}",
                                 item=proposal["name"],
                                 scene=g.world.current)
        # admin commands act as a chosen character: force the actor and
        # skip the ownership gate entirely (validity checked up top)
        if admin_actor and it is not None:
            it.actor = admin_actor
        # ownership: only the character's owner may act as that character
        if user_id and not admin_actor and it:
            # no actor specified → default to the CALLER's character
            if not it.actor:
                it.actor = next(
                    (n for n, v in g.party.items()
                     if str(v.get("owner_id", "")) == str(user_id)),
                    next(iter(g.party), None))
                if it.actor is None:
                    return (["❓ 還沒有角色——先用 `/pc name:名字 "
                             "occupation:職業` 建立角色。"], "")
            if it.actor and it.actor in g.party:
                oid = str(g.party[it.actor].get("owner_id", ""))
                if oid and oid != str(user_id):
                    g.ledger.add(it.actor, "deny",
                                 f"拒絕：{it.actor} 屬於其他玩家",
                                 reason="ownership")
                    return ([f"🚫 {it.actor} 屬於其他玩家——"
                             "你不能控制這個角色。"], "")
        idx0 = len(g.ledger.entries)  # this turn's slice of the ledger
        r = resolve(g, it)
        if r.confirm is not None:
            self.pending = r.confirm
        # v5 Pillar B: clocks tick, triggers fire — beats become part of
        # THIS turn's verdict and narration (the plot comes to them)
        if self.plot:
            beat_lines, _m = plot_tick(g, self.plot, self.plot_state)
            if beat_lines:
                r.lines += beat_lines
        # v3 lesson: every turn ends with a hook — append before posting
        if r.accepted and r.lines:
            from .templates import render_turn_context
            context = render_turn_context(g, it.actor or "")
            r.lines.append("")
            r.lines.append(context)
        # engine verdict first: hand it to the adapter the moment it exists
        # so the words-by-words narration can stream into its own message
        if on_resolved and r.lines:
            await on_resolved(r.lines)
        narration = ""
        if r.lines:
            self._unfinished = None  # new output supersedes the old;
            # _narrate below re-marks it if THIS turn's prose fails
        # v5 0b: denials get narrated too — the narrator's 吐槽 answers
        # the player instead of a dead-end 🚫. Rotation denials
        # （現在輪到 X）stay engine-only: turn order is meta, prose
        # there is noise.
        rotation_only = any(
            e.kind == "deny" and ("輪到" in str(e.data.get("reason", ""))
                             or "已行動" in str(e.data.get("reason", "")))
            for e in g.ledger.entries[idx0:])
        if r.lines and self.pending is None and not rotation_only:
            # v3 lesson: the narrator must answer the player's own words,
            # especially when the engine denied something
            player_input = it.raw or it.utterance or ""
            if not player_input:
                player_input = text[:120]
            narration = await self._narrate(
                idx0, player_input, on_delta,
                wants_help=bool(getattr(it, "wants_help", False)))
        self._save()
        return r.lines, narration

    # ---------- narration (shared by turns and roll settles) ----------

    async def _narrate(self, idx0: int, player_input: str = "",
                       on_delta=None, wants_help: bool = False) -> str:
        """Turn the ledger entries since idx0 into guarded prose.
        wants_help comes from the digestor's intent judgement; the
        keyword regex is only the no-LLM fallback."""
        g = self.game
        from .templates import render_hint
        from .guards import (make_placeholder_map, make_restore_map,
                             map_out, map_in, scrub_narration,
                             is_repetition, is_chinese)
        hints = [h for h in (render_hint(e, g)
                             for e in g.ledger.entries[idx0:]) if h]
        facts = [e.text for e in g.ledger.entries[idx0:]
                 if e.kind != "table"]
        brief = "；".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                         for n, e in g.party.items())
        # pass NPC knowledge constraints if this was a talk/insight turn;
        # a reveal entry carries its own npc + the exact fact
        npc_knows, npc_name = None, ""
        for e in g.ledger.entries[idx0:]:
            if e.kind in ("talk", "insight", "reveal") \
                    and e.data.get("npc"):
                npc_name = e.data["npc"]
                if e.kind == "reveal":
                    break
                if e.kind == "insight" and e.data.get("knows"):
                    # the engine already picked what insight reveals
                    npc_knows = e.data["knows"]
                    break
                for n in g.world.here.npcs:
                    if n["name"] == npc_name:
                        npc_knows = n.get("knows", [])
                        break
        # v3 guard: placeholder names — narrator never sees real names
        pmap = make_placeholder_map(list(g.party))
        rmap = make_restore_map(list(g.party))
        safe_hints = [map_out(h, pmap) for h in hints]
        safe_facts = [map_out(f, pmap) for f in facts]
        safe_brief = map_out(brief, pmap)
        safe_knows = ([map_out(k, pmap) for k in npc_knows]
                      if npc_knows else npc_knows)
        # v3 guard: repetition — if the last narrations were near-
        # identical, inject a hard break directive
        rep_hint = None
        if is_repetition(" ".join(safe_hints), self._recent_narrations):
            rep_hint = ("⚠️ 你最近的敘述幾乎相同——這次必須完全不同。"
                        "換一個場景細節、感官或節奏。")
        # help directives (live-game lesson: players asking NPCs or the
        # DM for help got atmosphere instead of information). Judged by
        # the DIGESTOR by meaning (wants_help); the regex only backstops
        # the deterministic no-LLM path
        help_bits = []
        reveals = [e for e in g.ledger.entries[idx0:]
                   if e.kind == "reveal"]
        if reveals and npc_name:
            # the engine already picked WHICH fact comes out this turn —
            # the narrator must voice exactly it, as dialogue
            fact = reveals[0].data.get("fact", "")
            help_bits.append(
                f"這段敘事必須讓 {npc_name} 以對話形式說出「{fact}」"
                "——這是引擎判定打聽到的新情報，逐字融入對白。")
        elif npc_knows and npc_name:
            for e in g.ledger.entries[idx0:]:
                if e.kind == "talk" and e.data.get("npc") == npc_name \
                        and e.data.get("ok"):
                    # a successful exchange must MOVE the story: the NPC
                    # answers with actual information, as dialogue
                    help_bits.append(
                        f"玩家正在向 {npc_name} 尋求資訊——這段敘事必須讓"
                        f"{npc_name}以對話形式明確說出至少一項上述已知"
                        "事實，不能只描寫氣氛或敷衍。")
                    break
        if wants_help or _HELP_RE.search(player_input):
            from .templates import suggested_actions
            opts = "；".join(suggested_actions(g, ""))[:220]
            help_bits.append(
                "玩家在向 DM 求助——先用一兩句總結現況，然後明確指出"
                f"可行的下一步（例如：{opts}）。不要只描寫氣氛。")
        # anti-parrot: the model copies its own recent openings (the
        # 千兩黃金 fixation) — name them so it cannot reuse them
        anti_open = None
        if self._recent_narrations:
            opens = "、".join(
                f"「{n.strip()[:8]}…」"
                for n in self._recent_narrations[-2:])
            anti_open = (f"你最近的敘述開頭是 {opens}——"
                         "這次禁止再用相同或相近的開頭，換全新的第一句。")
        directives = [d for d in (rep_hint, anti_open, *help_bits) if d]
        extra = "\n· ".join(directives) if directives else None
        # item inventory: what the narrator may mention
        s = g.world.here
        scene_items = ([n for n, _ in s.ground_items]
                       + [n for n, _ in s.hidden_items])
        party_inv = {ch: [n for n, _ in stacks]
                     for ch, stacks in g.inventory.items()}
        narration = await self.narrator.narrate(
            safe_facts, g.world.here.name, safe_brief, hints=safe_hints,
            npc_knows=safe_knows, npc_name=npc_name,
            extra_directive=extra, on_delta=on_delta,
            player_input=map_out(player_input, pmap),
            scene_items=scene_items, party_inventory=party_inv,
            world=world_block(self.rules))
        # review trail: log the pair so output quality is auditable
        log.info("narrate | in=%.60s | hints=%d | out=%.200s",
                 player_input.replace("\n", " "), len(hints),
                 narration.replace("\n", " "))
        # force Traditional Chinese (models skew Simplified)
        try:
            from opencc import OpenCC
            narration = OpenCC("s2t").convert(narration)
        except ImportError:
            pass
        # v3 guard: scrub fake dice/verdicts from narration
        narration, was_scrubbed_n = scrub_narration(narration)
        # item guard: bolded items must exist in the inventory
        from .guards import validate_narration_items
        all_known = scene_items + [i for items in party_inv.values()
                                   for i in items]
        narration, found_items = validate_narration_items(
            narration, all_known)
        for fi in found_items:
            if fi not in scene_items:
                scene_items.append(fi)
        # observability: narrator-invented props (the phantom 木雕
        # class of bug) — the DM sees what the guard let through
        log.info("item guard | scene=%s known=%d found=%s",
                 g.world.here.name, len(all_known),
                 found_items or "—")
        if was_scrubbed_n:
            g.ledger.add("engine", "guard",
                         "已從敘事中清除假骰/判定文字")
        # v3 guard: language check — if still not Chinese, degrade
        if narration and not is_chinese(narration):
            narration = ""  # reject non-Chinese output
        # /continue bookkeeping on the SAFE (placeholder) text the LLM
        # actually produced: empty = it never spoke; no terminal
        # punctuation = the prose was cut mid-sentence (max_tokens /
        # dropped stream) — both leave resumable state
        raw = narration.strip().rstrip("*").rstrip()
        if not raw or raw[-1] not in _END_PUNCT:
            self._unfinished = {"text": narration, "idx0": idx0,
                                "player_input": player_input}
        else:
            self._unfinished = None
        # v3 guard: restore real names before showing to players
        narration = map_in(narration, rmap)
        if not narration and hints:
            narration = map_in("\n".join(hints), rmap)  # degraded
        # track for repetition guard
        if narration:
            self._recent_narrations.append(narration)
            self._recent_narrations = self._recent_narrations[-5:]
            # v5 seed 5: persist the narration into the ledger — replays
            # are complete and #dnd-health reviews full prose
            g.ledger.add("narrator", "narration", narration[:400])
        return narration

    async def settle_roll(self, die: int,
                          on_delta=None,
                          on_resolved=None) -> tuple[list[str], str]:
        """Settle the pending check with the player's own die, then
        AUTO-CONTINUE: the narrator immediately picks the story back up
        from the settled outcome (v3 lesson: /roll must never leave the
        story hanging). on_resolved fires with the verdict lines before
        narration starts (instant verdict, streamed prose after).
        Returns (engine lines, narration) — empty lines when no check
        was pending (not a 'valid' settle)."""
        from .rules_core import resolve_pending_check
        g = self.game
        pend = getattr(g, "_pending_check", None)
        if not pend:
            return [], ""
        idx0 = len(g.ledger.entries)
        actor = pend.get("actor", "")
        skill = pend.get("skill", "")
        ability = pend.get("ability", "")
        from engine.charlib import SKILL_LABEL
        sk_zh = SKILL_LABEL.get(skill, "") or ability
        ok, line = resolve_pending_check(g, die)
        lines = [f"🎲 **{actor}** d20 → **{die}**"]
        if line:
            lines.append(line)
        # v3 lesson: every turn ends with a hook
        from .templates import render_turn_context
        lines.append("")
        lines.append(render_turn_context(g, actor))
        if on_resolved:
            await on_resolved(lines)
        self._unfinished = None  # this settle supersedes; _narrate re-marks
        # auto-continue: the story reacts to the settled verdict now.
        # The narrator answers the player's ORIGINAL /explore words (what
        # created the check), with the die outcome appended — not a bare
        # synthetic roll report (that produces flat re-quotes)
        origin = pend.get("origin", "")
        verdict = f"{actor} 擲骰 d20={die}：{'成功' if ok else '失敗'}"
        player_input = f"{origin}（{verdict}）" if origin else \
            f"{actor} 擲骰 d20={die}（{sk_zh}檢定{'成功' if ok else '失敗'}）"
        narration = await self._narrate(
            idx0, player_input, on_delta,
            wants_help=bool(pend.get("help")))
        self._save()
        return lines, narration

    # ---------- /continue: finish an unfinished narration first ----------

    def has_unfinished(self) -> bool:
        """True when the previous narration is resumable (and its ledger
        slice still exists — a state reset invalidates it)."""
        u = self._unfinished
        return bool(u) and 0 <= u.get("idx0", -1) < len(
            self.game.ledger.entries)

    async def continue_narration(self, on_delta=None) -> tuple[bool, str]:
        """Finish the previous unfinished narration, streaming word by
        word. Truncated prose (cut mid-sentence) resumes from the exact
        breakpoint via the narrator's continuation prompt; a silent
        narrator re-narrates the same ledger slice. Returns
        (continued, display_text) — (False, "") when there was nothing
        to finish or the narrator is still down (caller falls back)."""
        u = self._unfinished
        if not u:
            return False, ""
        if not (0 <= u["idx0"] < len(self.game.ledger.entries)):
            self._unfinished = None  # stale — the state was reset
            self._save()
            return False, ""
        from .guards import (make_restore_map, map_in, scrub_narration,
                             is_chinese)
        if u["text"]:
            # prose cut mid-sentence: continue from the breakpoint
            cont = await self.narrator.continue_text(u["text"], on_delta)
            try:
                from opencc import OpenCC
                cont = OpenCC("s2t").convert(cont)
            except ImportError:
                pass
            cont, _ = scrub_narration(cont)
            if cont and not is_chinese(cont):
                cont = ""
            if not cont:
                return False, ""  # narrator still down
            rmap = make_restore_map(list(self.game.party))
            # if the continuation itself got cut, stay resumable
            full = u["text"] + cont
            raw = full.strip().rstrip("*").rstrip()
            self._unfinished = (
                {"text": full, "idx0": u["idx0"],
                 "player_input": u["player_input"]}
                if not raw or raw[-1] not in _END_PUNCT else None)
            self._save()
            return True, map_in(cont, rmap)
        # the narrator never spoke: re-narrate the same turn from facts
        narration = await self._narrate(u["idx0"], u["player_input"],
                                        on_delta)
        self._save()
        return bool(narration), narration
