"""Per-channel orchestration: authorize → JEV → engine → commit → narrate.

Commands are serialized and staged. SQLite commits publish game state before
Discord callbacks or narration; narration failures leave a durable continuation.
"""

import asyncio
import json
from copy import copy, deepcopy
from functools import wraps
import logging
import os
import re
import inspect
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

from .cli import META, DEMO_PARTY, build_starter_game, seed_starter_knowledge
from .digestor import Digestor
from .director import (canonize_scene, DirectorProposal, DirectorDecision,
                       verify_proposal, scan_prop, MAX_DIRECTOR_SCENES)
from .director_llm import DirectorLLM
from .intent import Intent
from .narrator import Narrator
from .plot import load_plot, new_state, tick as plot_tick
from .rules_core import resolve, validate
from .turn import Game
from .state import encode_game, decode_game, clone_game
from .persistence import CampaignRepository, StorageError
from .memory import build_memory, recent_conversation
from .agent_config import legacy_config
from .model_gateway import ModelGateway, current_turn, turn_context
from .operators import MainAgent, ActionDecision
from .debug_capture import DebugCapture, capture_context, capture, replay_config
from .hints import progress_key, track_attempt

# a narration that ends WITHOUT one of these was cut mid-sentence
# (max_tokens cutoff / stream drop) -> /continue must finish it
_END_PUNCT = "。．.!！?？…」』）)\"'"


def serialized(result="turn"):
    """A command owns a private stage; only successful commits publish it."""
    def decorate(fn):
        @wraps(fn)
        async def call(self, *args, **kwargs):
            request_id = str(kwargs.pop("request_id", "") or "")
            arguments = inspect.signature(fn).bind(self, *args, **kwargs).arguments
            request_key = f"{fn.__name__}:{arguments.get('user_id', '')}:{request_id}" if request_id else ""
            async with self._lock:
                previous = self._operator_state.get("requests", {}).get(request_key) if request_key else None
                if previous is not None:
                    if previous.get("result") is not None:
                        return previous["result"] if result == "text" else tuple(previous["result"])
                    msg = "✅ 這個請求已儲存，不會重複執行；可用 `/continue` 查看敘事或下一步。"
                    return msg if result == "text" else (True, msg) if result == "continue" else ([msg], "")
                stage = copy(self)
                stage._owner = self
                stage.game = clone_game(self.game)
                stage.plot_state = deepcopy(self.plot_state)
                stage.pending = deepcopy(self.pending)
                stage._unfinished = deepcopy(self._unfinished)
                stage._recent_narrations = self._recent_narrations.copy()
                stage._decisions = []
                stage._operator_state = deepcopy(self._operator_state)
                if request_key:
                    requests = stage._operator_state.setdefault("requests", {})
                    # The receipt is committed with the mechanics. A process
                    # interruption before prose completes can never rerun them.
                    requests[request_key] = {"result": None}
                    while len(requests) > 128:
                        del requests[next(iter(requests))]
                stage._audit_cursor = 0
                stage._supervised = None
                stage._committed = False
                stage._attempt = None
                stage._hint_checked = False
                stage._latest_hint = ""
                try:
                    with turn_context(self.agent_config.max_calls, self._revision) as turn, \
                            capture_context(self.debug_capture, turn.trace_id):
                        bound = inspect.signature(fn).bind(stage, *args, **kwargs)
                        if self.debug_capture.enabled:
                            arguments = bound.arguments
                            capture("command_start", method=fn.__name__, channel_id=self.channel_id,
                                arguments={**{k: v for k, v in arguments.items() if k != "self" and not callable(v)},
                                           **({"request_id": request_id} if request_id else {})},
                                streaming=callable(arguments.get("on_delta")),
                                resolved_callback=callable(arguments.get("on_resolved")),
                                config=replay_config(self.agent_config), snapshot=stage._snapshot(),
                                entries=[asdict(e) for e in stage.game.ledger.entries],
                                revision=stage._revision, operator_state=self._operator_state,
                                unfinished=stage._unfinished, plot=stage.plot, rules=stage.rules)
                            original_delta = arguments.get("on_delta")
                            if callable(original_delta):
                                def observed_delta(text):
                                    original_delta(text)
                                    capture("delivery", phase="stream", status="sent", text=text)
                                bound.arguments["on_delta"] = observed_delta
                        log.info("command start | trace=%s channel=%s method=%s revision=%d supervision=%s budget=%d",
                                 turn.trace_id, self.channel_id, fn.__name__, self._revision,
                                 self.agent_config.enabled, turn.limit)
                        outcome = await fn(*bound.args, **bound.kwargs)
                        if request_key:
                            stage._operator_state["requests"][request_key]["result"] = outcome
                            stage._save(narration_only=True)
                        if stage._attempt and not stage._hint_checked:
                            stage._add_stall_hint(outcome[0])
                            stage._save(narration_only=True)
                        # Also retain failures/clarifications that produce no game turn.
                        if len(turn.records) > stage._audit_cursor:
                            stage._save(narration_only=True)
                        log.info("command end | trace=%s channel=%s method=%s revision=%d calls=%d/%d",
                                 turn.trace_id, self.channel_id, fn.__name__, stage._revision,
                                 turn.calls, turn.limit)
                        capture("command_end", result=outcome, calls=turn.calls, limit=turn.limit,
                                revision=stage._revision)
                        return outcome
                except StorageError:
                    log.exception("campaign save failed | channel=%s", self.channel_id)
                    msg = ("⚠️ 行動已儲存；敘事尚未儲存，請用 `/continue`。"
                           if stage._committed else "⚠️ 未能儲存，行動未執行；請稍後重試。")
                    if result == "text":
                        return msg
                    if result == "continue":
                        return True, msg
                    return [msg], ""
        return call
    return decorate


class V4Service:
    def __init__(self, data_dir: str, llm_url: str = "http://127.0.0.1:11434",
                 digest_model: str = "qwen2.5:7b-instruct",
                 narr_model: str = "qwen3.5:35b",
                 channel_id: str = "",
                 narr_think: bool | None = False,
                 digest_think: bool | None = None,
                 director_evaluate: bool = False, *, agent_config=None, gateway=None):
        fname = f"v4_state_{channel_id}.json" if channel_id else "v4_state.json"
        self.path = os.path.join(data_dir, fname)  # read-only legacy import source
        self.channel_id = channel_id
        self._lock = asyncio.Lock()
        self.repository = CampaignRepository(os.path.join(data_dir, "campaigns.db"))
        self._revision = self._saved_count = 0
        self._decisions = []
        self._audit_cursor = 0
        self._operator_state = {"accepted_turns": 0, "pending_triggers": [], "last_reviewed_event": 0}
        self._supervised = None
        self.agent_config = agent_config or (gateway.config if gateway else legacy_config(
            llm_url, digest_model, narr_model, digest_think, narr_think, director_evaluate))
        self.gateway = gateway or ModelGateway(self.agent_config)
        self.debug_capture = DebugCapture(data_dir, self.agent_config)
        self.digestor = Digestor(gateway=self.gateway)
        self.narrator = Narrator(gateway=self.gateway)
        self.director = DirectorLLM(gateway=self.gateway)
        self.director_evaluate = self.agent_config.director_evaluate
        self._unfinished = None
        self.pending = None
        self.pending_user_id = ""
        self.pending_admin = False
        self._recent_narrations = []
        self.rules = load_gamerules(channel_id, data_dir)
        self.plot = load_plot()
        self.plot_state = new_state(self.plot)
        self.game = self._load() or build_starter_game()
        repaired_knowledge = seed_starter_knowledge(self.game)
        from .campaign import sync_starter
        repaired_story = sync_starter(self.game)
        repaired_combat = self.game.check_end()
        check = getattr(self.game, "_pending_check", None)
        expired_check = bool(check and (check.get("actor") not in self.game.party
                             or check.get("scene", self.game.world.current) != self.game.world.current))
        if expired_check:
            self.game._pending_check = None
            self._unfinished = None
            self.game.ledger.add("engine", "check_expired", "待定檢定的角色或場景已不再有效；檢定已取消，沒有結算效果。")
        # Only recognizable, unowned seed characters are demo data. A player
        # choosing the same name must never lose their character on restart.
        demos = [name for name, template in DEMO_PARTY.items()
                 if (entry := self.game.party.get(name))
                 and not entry.get("owner_id") and not entry.get("owner")
                 and entry.get("occupation") == template["occupation"]
                 and entry.get("stats") == template["stats"]]
        if demos:
            self._drop_characters(demos)
            self.game.retired_demo_names = sorted(set(self.game.retired_demo_names) | set(demos))
            self._operator_state = {"accepted_turns": 0, "pending_triggers": [], "last_reviewed_event": 0}
            log.info("removed unowned demo characters | channel=%s count=%d", channel_id, len(demos))
        if self._revision == 0 or repaired_knowledge or repaired_story or repaired_combat or expired_check or demos:
            self._save()

    # ---------- persistence ----------

    def _snapshot(self) -> dict:
        blob = encode_game(self.game)
        blob.update(plot=self.plot_state,
                    pending=asdict(self.pending) if self.pending else None,
                    pending_user_id=self.pending_user_id,
                    pending_admin=self.pending_admin)
        return blob

    def _save(self, *, narration_only: bool = False, imported_from=None,
              restart: bool = False) -> None:
        # Historical entries are immutable; stamp only the newly staged events.
        for e in self.game.ledger.entries[self._saved_count:]:
            e.data.setdefault("scene", self.game.world.current)
            if e.kind in ("materialize", "director_scene"):
                e.data.setdefault("visibility", "private")
        # Prepare publication before committing, so a failed write never leaks
        # staged state through public .game, pending, or continuation properties.
        published_game = clone_game(self.game)
        published_plot = deepcopy(self.plot_state)
        published_pending = deepcopy(self.pending)
        published_unfinished = deepcopy(self._unfinished)
        published_operators = deepcopy(self._operator_state)
        turn = current_turn()
        audits = turn.records[self._audit_cursor:] if turn else []
        revision = self.repository.commit(
            self.channel_id, self._revision,
            None if narration_only else self._snapshot(),
            [asdict(e) for e in self.game.ledger.entries[self._saved_count:]],
            self._unfinished, self._decisions, imported_from=imported_from,
            operator_records=audits, operator_state=self._operator_state, restart=restart)
        self._revision = revision
        self._saved_count = len(self.game.ledger.entries)
        self._decisions = []
        if turn:
            self._audit_cursor = len(turn.records)
            turn.revision = revision
        self._committed = True
        if self.debug_capture.enabled:
            capture("commit", revision=revision, snapshot=self._snapshot(),
                    events=[asdict(e) for e in self.game.ledger.entries],
                    unfinished=self._unfinished, operator_state=self._operator_state)
        owner = getattr(self, "_owner", self)
        owner.game = published_game
        owner.plot_state = published_plot
        owner.pending = published_pending
        owner.pending_user_id = self.pending_user_id
        owner.pending_admin = self.pending_admin
        owner._unfinished = published_unfinished
        owner._operator_state = published_operators
        owner._revision = revision
        owner._saved_count = self._saved_count
        owner._recent_narrations = self._recent_narrations.copy()

    def _load(self):
        record = self.repository.load(self.channel_id)
        importing = record is None and os.path.exists(self.path)
        if record is None and not importing:
            return None
        try:
            if importing:
                with open(self.path, encoding="utf-8") as f:
                    blob = json.load(f)
                entries = blob.get("entries", [])
                unfinished = blob.get("unfinished")
            else:
                blob, entries = record["snapshot"], record["entries"]
                unfinished = record["unfinished"]
                self._operator_state.update(record.get("operator_state", {}))
            g = decode_game(blob, entries)
            self.plot_state = blob.get("plot") or new_state(self.plot)
            self.pending = Intent(**blob["pending"]) if blob.get("pending") else None
            self.pending_user_id = str(blob.get("pending_user_id", ""))
            self.pending_admin = bool(blob.get("pending_admin", False))
            if unfinished and "idx0" in unfinished:
                unfinished = dict(unfinished)
                idx = unfinished.pop("idx0")
                if not 0 <= idx < len(g.ledger.entries):
                    unfinished = None
                else:
                    unfinished["start_event_id"] = g.ledger.entries[idx].event_id
                    unfinished["end_event_id"] = g.ledger.entries[-1].event_id
            self._unfinished = unfinished
            self._recent_narrations = [e.text for e in g.ledger.entries
                                       if e.kind == "narration"][-5:]
            if importing:
                self.game = g
                self._save(imported_from=self.path)
                log.info("imported JSON campaign | channel=%s source=%s", self.channel_id, self.path)
            else:
                self._revision = record["revision"]
                self._saved_count = len(entries)
            return g
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise StorageError(f"Invalid campaign save for {self.channel_id!r}: {exc}") from exc

    # ---------- one turn ----------

    def table_talk(self, text: str, author: str = "") -> None:
        """Plain channel chatter is not campaign memory."""

    @serialized()
    async def handle(self, text: str, author: str = "", user_id: str = "",
                     structured: bool = False, on_delta=None, on_resolved=None,
                     admin_actor: str = "") -> tuple[list, str]:
        g = self.game
        t = (text or "").strip()
        if not t:
            return [], ""
        if t in META:
            return META[t](g), ""
        if admin_actor and admin_actor not in g.party:
            return [f"❓ 沒有角色「{admin_actor}」"], ""
        if t.lower().strip(" ！!。.") in ("確認", "确认", "confirm", "對", "对", "yes", "y", "好", "ok"):
            return await self._confirm(user_id, on_delta, on_resolved, bool(admin_actor))
        from .guards import scrub_input
        from .intent import parse_command, robbery_clarification
        t, was_scrubbed = scrub_input(t)
        if was_scrubbed:
            g.ledger.add(author or "?", "deny", "已過濾可疑指令文字", reason="injection")
        probe = parse_command(t, party_names=list(g.party)) or Intent("meta", actor=next(
            (n for n in g.party if t.startswith(n)), ""))
        from .objects import object_intent
        object_action = object_intent(g, t)
        if object_action:
            probe = object_action
        turn = current_turn()
        log.debug("action input | trace=%s text=%s", turn.trace_id if turn else "-", self.gateway.diagnostic(t))
        log.info("action route | trace=%s mode=%s parsed=%s",
                 turn.trace_id if turn else "-",
                 "typed" if structured else "supervised" if self.agent_config.enabled else "legacy",
                 probe.action if probe.raw else "none")
        reason = self._authorize(probe, user_id, admin_actor)
        if reason:
            return self._deny(probe.actor, reason)
        from .campaign import equipment_guidance
        from .intent import _action_text
        if guidance := equipment_guidance(g, _action_text(t, list(g.party))[1]):
            return [guidance], ""
        if self.agent_config.routing.enabled:
            self._attempt = {"before": progress_key(g, self.plot_state),
                             "eligible": not g.combat.active, "wants_help": bool(_HELP_RE.search(t))}
        if object_action:
            object_action.actor = probe.actor
            if structured or not self.agent_config.enabled or not validate(g, object_action):
                return await self._execute(object_action, user_id, admin_actor, text, on_delta, on_resolved)
        from .theft import parse_theft
        from .npc_actions import observation_plan, return_to_npc
        if exact := parse_theft(t, g.party):
            if "盗む" in t:
                exact.actor = probe.actor
                if structured or not self.agent_config.enabled or not validate(g, exact):
                    return await self._execute(exact, user_id, admin_actor, text, on_delta, on_resolved)
        if raw_plan := observation_plan(g, t):
            from .action_plans import parse_plan, PlanError
            try:
                plan = parse_plan(raw_plan, g, probe.actor, self._revision, t)
            except PlanError as exc:
                return ["❓ " + str(exc)], ""
            it = Intent("creative", actor=probe.actor, raw=t, args={"linked_plan": plan})
            self._supervised = ActionDecision(it, self._revision, linked_plan=plan)
            return await self._execute(it, user_id, admin_actor, text, on_delta, on_resolved)
        if returned := return_to_npc(g, t):
            returned.actor = probe.actor
            return await self._execute(returned, user_id, admin_actor, text, on_delta, on_resolved)
        if not structured:
            clarification = robbery_clarification(t, list(g.party),
                [n["name"] for n in g.scene_npcs()])
            if clarification:
                log.info("action stopped | trace=%s reason=robbery_method_required", turn.trace_id if turn else "-")
                return [clarification], ""
        if t.startswith(("((", "//")):
            it = Intent(action="chat", utterance=t.lstrip("(/ "), raw=t)
        else:
            it = parse_command(t, party_names=list(g.party)) if structured else None
            if self.agent_config.enabled and (not structured or (it and it.action in ("move", "search")
                                                                    and not self.agent_config.routing.enabled)):
                parsed = it if structured else parse_command(t, party_names=list(g.party))
                if self.pending and self.pending_user_id != str(user_id):
                    return self._deny(probe.actor, "請先等提出行動的玩家確認待決行動。")
                controller = self._main_agent()
                interpret = controller.interpret_routed if self.agent_config.routing.enabled and not structured else controller.interpret
                self._supervised = await interpret(
                    clone_game(g), self._revision, t, parsed, user_id=user_id,
                    admin_actor=admin_actor, rules=deepcopy(self.rules),
                    plot_state=deepcopy(self.plot_state),
                    **({"locked_intent": structured} if interpret == controller.interpret else {}))
                it = self._supervised.intent
            elif it is None and not structured:
                it = await self.digestor.digest(
                    t, list(g.party), g.world.here.name, list(g.world.here.exits.values()),
                    known_targets=g.target_names(),
                    conversation=recent_conversation(g, probe.actor))
            if structured and it is not None:
                it.confidence = 1.0
        if it is None:
            if self._supervised and self._supervised.director_decision:
                self._decisions.append(asdict(self._supervised.director_decision))
            if self._supervised and self._supervised.feedback:
                return [self._supervised.feedback], ""
            return [self._interpretation_help(probe)], ""
        if it.action == "talk" and not it.utterance:
            it.utterance = (it.raw or "")[:120]
        if it.action == "move" and not it.destination:
            it.action = "meta"
        if it.action == "rest" and not it.args.get("kind"):
            raw = f"{it.raw or ''} {it.utterance or ''}"
            it.args["kind"] = "long" if re.search(r"長休|长休|long\s*rest|全休|過夜|过夜", raw, re.I) else "short"
        if it.action == "use":
            from engine.charlib import normalize_skill
            _ab, skill = normalize_skill(it.item or "")
            if skill:
                it.action, it.skill = "skill", skill
                it.target = it.target or it.args.pop("to", "")
        return await self._execute(it, user_id, admin_actor, text, on_delta, on_resolved)

    def _interpretation_help(self, parsed=None):
        """Distinguish failed model requests from an ambiguous player action."""
        turn = current_turn()
        failures = [r for r in (turn.records if turn else [])
                    if r.get("kind") == "model_call" and r["role"] in ("main", "action")
                    and r["status"] in ("unavailable", "budget_exhausted")]
        if not failures:
            failures = [r for r in (turn.records if turn else [])
                        if r.get("kind") == "report" and r["role"] in ("main", "action")
                        and r["status"] in ("invalid", "fallback")]
        if failures:
            failure = failures[-1]
            log.warning("action stopped | trace=%s reason=model_failure role=%s status=%s error=%s http=%s",
                        turn.trace_id, failure["role"], failure["status"],
                        failure.get("error_code", "schema_validation"), failure.get("http_status", ""))
            if failure.get("http_status") in (401, 403):
                detail = "服務認證未能完成，請通知管理員檢查服務設定。"
            elif failure.get("http_status") == 404:
                detail = "暫時找不到所需服務，請通知管理員檢查服務設定。"
            elif failure.get("error_code") == "invalid_response" or failure.get("kind") == "report":
                detail = "回覆格式未通過驗證，請稍後重試。"
            elif failure.get("error_code") == "timeout":
                detail = "服務逾時，請稍後重試。"
            else:
                detail = "服務未能完成請求，請稍後重試或通知管理員。"
            return (f"⚠️ 暫時無法解析自由描述。{detail}"
                    "行動未執行。可先用明確指令，例如 `/explore 搜索`、`/defend` 或 `/status`。")
        log.info("action stopped | trace=%s reason=clarification_required candidate=%s",
                 turn.trace_id if turn else "-", parsed.action if parsed else "none")
        if parsed and parsed.action == "attack" and parsed.target:
            return (f"❓ 已識別你想與「{parsed.target}」戰鬥，仍需確認目標，行動尚未執行。"
                    "如確定要按一般戰鬥規則攻擊，可用 `/attack` 並填入目標名稱；"
                    "如果只是切磋，請說明規則。")
        return ("❓ 請說明要做甚麼、對誰，以及使用的方法；行動尚未執行。"
                "例如 `/explore 搜索`，或用 `/skill` 選擇技能與對象。")

    def _main_agent(self):
        return MainAgent(self.gateway, self.digestor, self.director, self.narrator)

    def _authorize(self, it, user_id, admin_actor=""):
        g = self.game
        if admin_actor:
            it.actor = admin_actor
        elif user_id and not it.actor:
            it.actor = next((n for n, e in g.party.items()
                             if str(e.get("owner_id", "")) == str(user_id)), "")
            if not it.actor:
                return "找不到你的角色——先用 `/pc` 建立。"
        if not it.actor:
            it.actor = next((n for n in g.party if g.alive(n)), next(iter(g.party), ""))
        if not it.actor or it.actor not in g.party:
            return "沒有可用的角色——先用 `/pc` 建立。"
        owner = str(g.party[it.actor].get("owner_id", ""))
        if user_id and not admin_actor and owner != str(user_id):
            return f"{it.actor} 屬於其他玩家——你不能控制這個角色。"
        return None

    def _deny(self, actor, reason):
        turn = current_turn()
        log.info("action denied | trace=%s reason=%s",
                 turn.trace_id if turn else "-", self.gateway.diagnostic(reason))
        self.game.ledger.add(actor or "?", "deny", f"拒絕：{reason}", reason=reason)
        lines = [f"🚫 {reason}"]
        self._add_stall_hint(lines)
        self._save()
        return lines, ""

    def _add_stall_hint(self, lines, it=None):
        if not getattr(self, "_attempt", None) or getattr(self, "_hint_checked", False):
            return
        self._hint_checked = True
        attempt = self._attempt
        eligible = attempt["eligible"] and (it is None or it.action not in ("rest", "meta", "chat", "observe", "inspect"))
        hint = track_attempt(self._operator_state, self.game, self.agent_config.hints,
                             before=attempt["before"], plot=self.plot_state, eligible=eligible,
                             wants_help=attempt["wants_help"] or bool(it and it.wants_help))
        if hint:
            lines.append(hint)
            self._latest_hint = hint
            if self._unfinished:
                self._unfinished["stall_hint"] = hint

    @serialized()
    async def confirm(self, user_id: str = "", on_delta=None, on_resolved=None,
                      admin: bool = False):
        return await self._confirm(user_id, on_delta, on_resolved, admin)

    async def _confirm(self, user_id, on_delta, on_resolved, admin=False):
        if self.pending is None:
            return await self._resume_plan(user_id, on_delta, on_resolved)
        if self.pending_user_id != str(user_id) or (self.pending_admin and not admin):
            return self._deny("?", "只有提出行動的玩家可以確認。")
        it = deepcopy(self.pending)
        admin_actor = it.actor if self.pending_admin else ""
        it.args["confirmed"] = True
        if self.agent_config.enabled:
            reason = self._authorize(it, user_id, admin_actor)
            if reason:
                return self._deny(it.actor, reason)
            self._supervised = await self._main_agent().interpret(
                clone_game(self.game), self._revision, it.raw, it, user_id=user_id,
                admin_actor=admin_actor, rules=deepcopy(self.rules),
                plot_state=deepcopy(self.plot_state), locked_intent=True)
            if self._supervised.intent is None:
                if self._supervised.director_decision:
                    self._decisions.append(asdict(self._supervised.director_decision))
                return [self._supervised.feedback or "❓ 請再描述你想確認的行動。"], ""
        return await self._execute(it, user_id, admin_actor, it.raw, on_delta, on_resolved)

    @serialized()
    async def action_plan(self, action="status", user_id="", on_delta=None, on_resolved=None, *, admin=False):
        """Explicit status/resume/cancel; narration continuation never runs steps."""
        from .action_plans import plans, plan_status
        probe = Intent("meta")
        pend = getattr(self.game, "_pending_check", None)
        reason = self._authorize(probe, user_id)
        if admin and action == "cancel" and pend:
            reason, probe.actor = None, pend["actor"]
        if reason:
            return self._deny(probe.actor, reason)
        if action == "status":
            if pend:
                return [f"🎲 待定檢定：{pend['actor']}（{pend.get('origin', '先前行動')}）。"
                        "由該玩家用 `/roll d20` 結算，或 `/plan cancel` 取消。"], ""
            return plan_status(self.game, probe.actor), ""
        if action == "resume":
            return await self._resume_plan(user_id, on_delta, on_resolved)
        if action != "cancel":
            return ["請選擇 status、resume 或 cancel。"], ""
        if pend and pend.get("actor") == probe.actor:
            plan_effect = (pend.get("effect") or {}).get("plan") or {}
            if plan_effect.get("version") == 2 and (pend.get("effect") or {}).get("kind") == "improvisation":
                self.game._creative_attempts[plan_effect["id"]] = "cancelled"
            self.game._pending_check = None
            self._unfinished = None
            self.game.ledger.add(probe.actor, "check_cancel", "玩家取消待定檢定；沒有結算效果，已付出的行動或材料不退還。")
            plan = plans(self.game).get(probe.actor)
            if plan and plan["status"] not in ("completed", "failed", "cancelled"):
                plan.update(status="cancelled", stop_reason="玩家取消待定檢定；剩餘步驟不會執行。")
            self._save()
            return ["📋 已取消待定檢定；沒有結算效果，已付出的行動或材料不退還。可重新提出行動。"], ""
        plan = plans(self.game).get(probe.actor)
        if not plan or plan["status"] in ("completed", "failed", "cancelled"):
            return ["（沒有可取消的行動計畫）"], ""
        plan["status"] = "cancelled"
        plan["stop_reason"] = "玩家取消；不會執行剩餘步驟。已消耗材料不退還，已製成物品保留。"
        pend = getattr(self.game, "_pending_check", None)
        if pend and (pend.get("effect") or {}).get("plan_id") == plan["id"]:
            self.game._pending_check = None
        self.game.ledger.add(probe.actor, "action_plan", plan["stop_reason"],
                             plan_id=plan["id"], status="cancelled")
        capture("action_plan", plan_id=plan["id"], outcome="cancelled")
        self._save()
        return ["📋 " + plan["stop_reason"]], ""

    async def _resume_plan(self, user_id, on_delta, on_resolved):
        from .action_plans import current_plan
        probe = Intent("creative")
        reason = self._authorize(probe, user_id)
        if reason:
            return self._deny(probe.actor, reason)
        plan = current_plan(self.game, probe.actor)
        if not plan:
            return ["（沒有待確認的行動或可繼續的計畫）"], ""
        probe.args = {"linked_plan_id": plan["id"]}
        probe.raw = plan["player_words"]
        probe.narrative = {"player_words": plan["player_words"], "attempted_action": plan["goal"]}
        return await self._execute(probe, user_id, "", probe.raw, on_delta, on_resolved)

    async def _execute(self, it, user_id, admin_actor, text, on_delta, on_resolved):
        g = self.game
        turn = current_turn()
        log.info("action selected | trace=%s action=%s confidence=%.2f",
                 turn.trace_id if turn else "-", it.action, it.confidence)
        log.debug("action candidate | trace=%s details=%s", turn.trace_id if turn else "-",
                  self.gateway.diagnostic({"actor": it.actor, "target": it.target, "skill": it.skill,
                                           "destination": it.destination}))
        reason = self._authorize(it, user_id, admin_actor)
        if reason:
            return self._deny(it.actor, reason)
        reading = it.action in ("inspect", "meta", "chat")
        if self.pending and self.pending_user_id != str(user_id) and not reading:
            return self._deny(it.actor, "請先等提出行動的玩家確認待決行動。")
        # A new action by the same player cancels their prior confirmation.
        if not reading:
            self.pending = None
            self.pending_user_id = ""
            self.pending_admin = False
        if getattr(g, "_pending_check", None) and not reading:
            p = g._pending_check
            return self._deny(it.actor, f"尚有待定檢定：{p['actor']}（{p.get('origin', '先前行動')}）。"
                              "請由該玩家用 `/roll d20` 結算，或 `/plan cancel` 取消；"
                              "你這次的行動未執行，結算後請重新提出。仍可查看 `/scene`、`/inventory` 或檢查物品。")
        if "linked_plan" in it.args:
            approved = (self._supervised and self._supervised.linked_plan == it.args["linked_plan"]
                        and self._supervised.source_revision == self._revision)
            if not approved or it.action != "creative" or it.args["linked_plan"].get("actor") != it.actor:
                return self._deny(it.actor, "行動計畫尚未通過當前回合的審核。")
        reason = validate(g, it, allow_unplanned_move=True)
        if reason and it.action in ("move", "search"):
            return self._deny(it.actor, reason)
        if it.action in ("attack", "move", "search") and it.confidence < 0.7 \
                and not it.args.get("confirmed"):
            self.pending = deepcopy(it)
            self.pending_user_id = str(user_id)
            self.pending_admin = bool(admin_actor)
            g.ledger.add(it.actor, "confirm", "等待玩家確認行動")
            self._save()
            return [f"❓ 我理解你要：{it.actor} {it.action} {it.target or it.destination or it.item}——用 `/confirm` 執行。"], ""
        baseline = clone_game(g)
        idx0 = len(g.ledger.entries)
        if "improvisation" in it.args:
            from .improvisation import verify_improvisation
            approved = self._supervised and self._supervised.improvisation == it.args["improvisation"]
            invalid = verify_improvisation(g, it.args["improvisation"], revision=self._revision)
            if not approved or invalid:
                return self._deny(it.actor, "花招尚未通過當前回合的審核。")
        if not reason:
            if self.agent_config.enabled:
                self._apply_supervised_proposal(it)
            else:
                await self._direct(it)
        r = resolve(g, it)
        if not r.accepted or r.confirm is not None:
            # No partially resolved mechanics or generated props may survive a denial.
            rejected = [e for e in g.ledger.entries[idx0:] if e.kind == "deny"]
            self.game = g = baseline
            for e in rejected:
                g.ledger.add(e.actor, e.kind, e.text, **e.data)
            for decision in self._decisions:
                if decision["accepted"]:
                    decision["accepted"] = False
                    decision["reasons"].append("action_rejected")
            if r.confirm is not None:
                self.pending = r.confirm
                self.pending_user_id = str(user_id)
                self.pending_admin = bool(admin_actor)
        if r.accepted and r.confirm is None and not reading:
            from .campaign import sync_starter
            r.lines += sync_starter(g)
        if r.accepted and r.confirm is None and g.ledger.turn > baseline.ledger.turn and self.plot:
            beat_lines, _ = plot_tick(g, self.plot, self.plot_state)
            r.lines += beat_lines
            if self.plot_state.get("ended") == "queen_alliance":
                g._starter_progress = dict(getattr(g, "_starter_progress", None) or {}, completed=True)
                sync_starter(g)
        pend = getattr(g, "_pending_check", None)
        if pend is not None and "user_id" not in pend and not reading:
            pend["user_id"] = str(g.party[pend["actor"]].get("owner_id", "")) or str(user_id)
            pend["scene"] = g.world.current
            pend.setdefault("event_id", g.ledger.entries[-1].event_id)
        # Director facts belong to the same turn as the action they support.
        for e in g.ledger.entries[idx0:]:
            e.turn = g.ledger.turn
        if r.accepted and r.lines and not reading and self._context_noteworthy(idx0):
            from .templates import render_turn_context
            r.lines += ["", render_turn_context(g, it.actor)]
        rotation_only = any(e.kind == "deny" and ("輪到" in str(e.data.get("reason", ""))
                            or "已行動" in str(e.data.get("reason", ""))) for e in g.ledger.entries[idx0:])
        player_input = it.raw or it.utterance or text[:120]
        # Speech without an addressed NPC has already been rendered by the
        # engine. It needs no second dialogue bubble or narration retry job.
        speech_only = all(e.kind == "talk" and not e.data.get("npc")
                          for e in g.ledger.entries[idx0:])
        narrate = bool(r.accepted and it.action != "inspect" and r.lines and self.pending is None and not rotation_only
                       and not speech_only
                       and len(g.ledger.entries) > idx0)
        if r.lines and it.action != "inspect" and not (it.action == "meta" and it.args.get("inventory")):
            self._unfinished = self._narration_job(idx0, player_input, it.wants_help) if narrate else None
            if self._unfinished and it.narrative:
                self._unfinished["narrative"] = it.narrative
        self._add_stall_hint(r.lines, it)
        self._queue_progress(idx0, baseline, accepted=r.accepted and r.confirm is None,
                             wants_help=it.wants_help or bool(_HELP_RE.search(text)),
                             gameplay=it.action not in ("meta", "chat"))
        self._save()
        return await self._deliver(r.lines, idx0, player_input, on_delta, on_resolved,
                                   narrate=narrate, wants_help=it.wants_help)

    def _apply_supervised_proposal(self, it):
        result = self._supervised
        if not result or not result.director_decision:
            return
        decision, proposal = result.director_decision, result.proposal
        if proposal:
            reason = ("stale_revision" if result.source_revision != self._revision else
                      "evaluation_required" if proposal.kind == "scene" and decision.evaluation != "approved" else
                      verify_proposal(self.game, proposal) or validate(self.game, it, allow_unplanned_move=True))
            if reason:
                decision.reasons.append(reason)
            elif proposal.kind == "scene":
                decision.accepted = canonize_scene(self.game, proposal.content)
                if decision.accepted:
                    it.destination = next(sid for sid, scene in self.game.world.scenes.items()
                                          if scene.name == proposal.content["name"].strip())
            else:
                name = proposal.content["name"].strip()
                self.game.world.here.hidden_items.append((name, 1))
                self.game.ledger.add("director", "materialize", f"搜索途中世界浮現了 {name}",
                                     item=name, scene=self.game.world.current, visibility="private")
                decision.accepted = True
        self._decisions.append(asdict(decision))

    def _queue_progress(self, idx0, baseline, *, accepted=True, wants_help=False, gameplay=True):
        if not self.agent_config.enabled or not accepted:
            return
        state = self._operator_state
        triggers = set(state["pending_triggers"])
        if gameplay and self.game.ledger.turn > baseline.ledger.turn:
            state["accepted_turns"] += 1
            if state["accepted_turns"] % self.agent_config.progress_interval == 0:
                triggers.add("interval")
        if wants_help:
            triggers.add("help")
        if self.game.world.current != baseline.world.current:
            triggers.add("scene")
        if any(e.kind == "beat" for e in self.game.ledger.entries[idx0:]):
            triggers.add("plot_beat")
        if baseline.combat.active and not self.game.combat.active:
            triggers.add("combat_complete")
        state["pending_triggers"] = sorted(triggers)

    async def _review_progress(self, player_input):
        triggers = self._operator_state["pending_triggers"]
        if not self.agent_config.enabled or not triggers or not self._unfinished:
            return
        guidance = await self._main_agent().review_progress(
            clone_game(self.game), self._revision, triggers.copy(), deepcopy(self.plot),
            player_input, self._unfinished.get("npc_name", ""))
        if guidance and guidance["source_revision"] == self._revision:
            self._unfinished["guidance"] = guidance
            self._operator_state["pending_triggers"] = []
            self._operator_state["last_reviewed_event"] = self._unfinished["end_event_id"]
        # Durable before streaming, including deferred trigger and audit status.
        self._save(narration_only=True)

    def _guidance(self):
        guidance = (self._unfinished or {}).get("guidance")
        if not guidance or not guidance.get("options"):
            return ""
        last = next((e.event_id for e in reversed(self.game.ledger.entries)
                     if e.kind not in ("narration", "guard")), 0)
        if guidance.get("source_event_id") != last:
            return ""
        return ("主代理已審核的可選下一步（只是建議，不是已發生事件；"
                "以 DM 身份提出，不能當作 NPC 的知識）：" + "；".join(guidance["options"]))

    def _narrator_interrupted(self):
        turn = current_turn()
        last = next((r for r in reversed(turn.records) if r["role"] == "narrator"
                     and r.get("kind") == "model_call"), None) if turn else None
        return bool(last and last["status"] != "ok")

    async def _direct(self, it):
        from .director import search_available
        g, s = self.game, self.game.world.here
        kind = ""
        if it.action == "move" and not g.world.find_exit(it.destination):
            if g.director_scene_count >= MAX_DIRECTOR_SCENES:
                self._decisions.append(asdict(DirectorDecision("scene", s.id, reasons=["scene_limit"])))
                return
            kind = "scene"
        elif it.action == "search" and not s.hidden_items and search_available(g):
            if not scan_prop(f"{it.raw} {it.utterance} {it.item}") and g._random.random() < 0.35:
                kind = "item"
        if not kind:
            return
        memory = build_memory(g, it.raw or it.utterance, audience="director",
                              plot_state=self.plot_state).render()
        tone = self.rules.get("tone", "奇幻")
        decision = DirectorDecision(kind, s.id)
        try:
            if kind == "scene":
                content = await self.director.gen_scene(tone, s.name, s.description, it.destination,
                    gamerules_canon=(self.rules.get("world") or {}).get("canon", []), memory=memory)
            else:
                content = await self.director.gen_item(tone, s.name, s.description,
                                                       it.utterance or it.raw or "搜索", memory=memory)
            decision.proposal = content
            proposal = DirectorProposal(kind, s.id, content)
            reason = verify_proposal(g, proposal)
            if reason:
                decision.reasons.append(reason)
            else:
                approved = True
                if self.director_evaluate:
                    approved, explanation = await self.director.evaluate(
                        asdict(proposal), world_block(self.rules) + "\n" + memory,
                        it.raw or it.destination or it.utterance)
                    decision.evaluation = "approved" if approved else "rejected"
                    decision.reasons.append(explanation)
                reason = verify_proposal(g, proposal) or validate(g, it, allow_unplanned_move=True)
                if reason:
                    decision.reasons.append(reason)
                elif approved:
                    if kind == "scene":
                        decision.accepted = canonize_scene(g, content)
                        if decision.accepted:
                            # Select the exact generated ID; a short generated
                            # name may also fuzzy-match an older exit's label.
                            it.destination = next(sid for sid, scene in g.world.scenes.items()
                                                  if scene.name == content["name"].strip())
                    else:
                        name = content["name"].strip()
                        s.hidden_items.append((name, 1))
                        g.ledger.add("director", "materialize", f"搜索途中世界浮現了 {name}",
                                     item=name, scene=s.id, visibility="private")
                        decision.accepted = True
        except Exception as exc:
            log.warning("director unavailable: %s", type(exc).__name__)
            decision.reasons.append("model_unavailable")
        self._decisions.append(asdict(decision))
        log.info("director decision | kind=%s accepted=%s evaluation=%s reasons=%s",
                 kind, decision.accepted, decision.evaluation, decision.reasons)

    def _narration_job(self, idx0, player_input, wants_help=False):
        entries = self.game.ledger.entries
        npc_name = next((e.data["npc"] for e in entries[idx0:]
                         if e.kind in ("talk", "insight", "reveal") and e.data.get("npc")), "")
        return {"text": "", "start_event_id": entries[idx0].event_id,
                "end_event_id": entries[-1].event_id, "player_input": player_input,
                "wants_help": wants_help, "npc_name": npc_name}

    async def _deliver(self, lines, idx0, player_input, on_delta, on_resolved,
                       *, narrate=True, wants_help=False):
        # This method is only called AFTER the engine transaction commits.
        try:
            if on_resolved and lines:
                await on_resolved(lines)
                capture("delivery", phase="verdict", status="sent", text=lines)
        except Exception:
            capture("delivery", phase="verdict", status="failed")
            log.exception("verdict delivery failed; campaign is committed")
            return lines, ""
        narration = ""
        if narrate:
            await self._review_progress(player_input)
            try:
                narration = await self._narrate(idx0, player_input, on_delta, wants_help=wants_help)
            except Exception:
                log.exception("narrator failed; committed narration remains resumable")
                return lines, ""
            self._save(narration_only=True)
        return lines, narration

    # ---------- non-turn mutations: same lock and transaction boundary ----------

    def _drop_characters(self, names):
        """Remove current references without rewriting the historical ledger."""
        g = self.game
        for name in names:
            g.party.pop(name, None)
            g.inventory.pop(name, None)
        g.combat.order = [c for c in g.combat.order if c["name"] not in names]
        g.combat.acted = [n for n in g.combat.acted if n not in names]
        g.combat.idx = g.combat.idx % len(g.combat.order) if g.combat.order else 0
        for attr in ("_aid", "_dodge", "_stealth", "_help_check", "_action_plans"):
            for name in names:
                getattr(g, attr, {}).pop(name, None)
        if (getattr(g, "_pending_check", None) or {}).get("actor") in names:
            g._pending_check = None
        if self.pending and self.pending.actor in names:
            self.pending, self.pending_user_id, self.pending_admin = None, "", False
        # Placeholder mappings change when the party changes.
        self._unfinished = None
        self._recent_narrations = []

    @serialized("text")
    async def new_game(self, user_id: str, *, admin: bool = False) -> str:
        if not admin:
            return "🚫 需要管理伺服器權限。"
        if self.game.party or self.game.ledger.entries or self.game.ledger.turn \
                or self.pending or self._unfinished or self.game.combat.active:
            return "⚠️ 此頻道已有遊戲，請用 `/scene` 查看；重新開始請用 `/reset_game`。"
        self.game.ledger.add("engine", "meta", "新遊戲已開始", user_id=str(user_id))
        self._save()
        return "🎲 新遊戲已開始！請每位玩家用 `/pc` 建立角色。\n" + self.scene()

    @serialized("text")
    async def reset_game(self, user_id: str, confirmation: str = "", *, admin: bool = False) -> str:
        if not admin:
            return "🚫 需要管理伺服器權限。"
        if confirmation.strip().lower() != "reset confirm":
            return ("⚠️ 將重新開始此頻道的遊戲，清除目前角色、物品、場景進度及待定行動。"
                    "舊遊戲會封存。確認請用 `/reset_game confirmation:reset confirm`。")
        self.game = build_starter_game()
        self.plot_state = new_state(self.plot)
        self.pending, self.pending_user_id, self.pending_admin = None, "", False
        self._unfinished = None
        self._recent_narrations = []
        self._operator_state = {"accepted_turns": 0, "pending_triggers": [], "last_reviewed_event": 0}
        self._saved_count = 0
        self.game.ledger.add("engine", "meta", "遊戲已重設", user_id=str(user_id))
        self._save(restart=True)
        return "🔄 舊遊戲已封存，新遊戲已開始！請用 `/pc` 建立角色。\n" + self.scene()

    def scene(self) -> str:
        """Public current-scene facts only; no action, model call, or write."""
        g = self.game
        s = g.world.here
        lines = [f"📍 **{s.name}**", s.description]
        if s.fire:
            lines.append("🔥 場景正在燃燒。")
        names = list(dict.fromkeys([n["name"] for n in g.scene_npcs()] +
                     [n for n, f in g.scene_enemies().items() if not f.dead and f.hp > 0]))
        lines.append("👥 人物：" + ("、".join(names) or "無"))
        lines.append("🚪 出口：" + ("、".join(s.exits.values()) or "無"))
        lines.append("📦 地上物品：" + ("、".join(f"{n} ×{q}" for n, q in s.ground_items) or "無"))
        if g.combat.active:
            lines.append(f"⚔️ 戰鬥 R{g.combat.round}")
        lines.extend(f"👹 {n}（敵對）{f.hp}/{f.hp_max}HP" for n, f in g.scene_enemies().items() if not f.dead and f.hp > 0)
        if not g.party:
            lines.append("👉 請先用 `/pc` 建立角色，再用 `/explore` 行動。")
        return "\n".join(lines)

    @serialized("text")
    async def create_character(self, name: str, occupation: str, user_id: str, owner: str) -> str:
        g = self.game
        uid, uname = str(user_id), owner
        if not name.strip() or len(name) > 80:
            return "❓ 請使用 1 至 80 字的角色名。"
        if name in g.party:
            return f"❓ 「{name}」已存在（擁有者：{g.party[name].get('owner', '?')}）"
        occ = occupation or "Fighter"
        # fair stats: server-side 4d6kh3 ×6, assigned by class priority
        from engine.charlib import default_ac, slots_for
        PRIORITIES = {
            "Barbarian": ["STR", "CON", "DEX", "WIS", "CHA", "INT"],
            "Bard": ["CHA", "DEX", "CON", "WIS", "INT", "STR"],
            "Cleric": ["WIS", "CON", "STR", "CHA", "DEX", "INT"],
            "Druid": ["WIS", "CON", "DEX", "INT", "CHA", "STR"],
            "Fighter": ["STR", "CON", "DEX", "WIS", "CHA", "INT"],
            "Monk": ["DEX", "WIS", "CON", "STR", "CHA", "INT"],
            "Paladin": ["STR", "CHA", "CON", "WIS", "DEX", "INT"],
            "Ranger": ["DEX", "WIS", "CON", "STR", "INT", "CHA"],
            "Rogue": ["DEX", "INT", "CON", "WIS", "CHA", "STR"],
            "Sorcerer": ["CHA", "CON", "DEX", "WIS", "INT", "STR"],
            "Warlock": ["CHA", "CON", "DEX", "WIS", "INT", "STR"],
            "Wizard": ["INT", "CON", "DEX", "WIS", "CHA", "STR"],
        }
        prio = PRIORITIES.get(occ, ["STR", "DEX", "CON", "INT", "WIS", "CHA"])
        rolls = sorted((g.roll("4d6kh3")[0] for _ in range(6)),
                       reverse=True)
        stats = dict(zip(prio, rolls))
        # HP = hit die + CON mod
        HIT_DICE = {"Barbarian": 12, "Fighter": 10, "Paladin": 10,
                    "Ranger": 10, "Bard": 8, "Cleric": 8, "Druid": 8,
                    "Monk": 8, "Rogue": 8, "Warlock": 8,
                    "Sorcerer": 6, "Wizard": 6}
        die = HIT_DICE.get(occ, 8)
        con_mod = (stats["CON"] - 10) // 2
        hp = die + con_mod
        ac = default_ac(occ, stats)
        slots = {str(k): v for k, v in slots_for(occ, 1).items()}

        g.party[name] = {
            "occupation": occ, "stats": stats,
            "hp_now": hp, "hp_max": hp, "level": 1, "xp": 0,
            "owner": uname, "owner_id": uid,
            "ac": ac, "slots": slots, "hd_used": 0,
        }
        g.inventory.setdefault(name, [])
        from engine.moves import STARTER_EQUIPMENT
        equipment = STARTER_EQUIPMENT.get(occ, ())
        for item in equipment:
            g.give_item(name, item)
        g.ledger.add(name, "meta",
                     f"{uname} 建立了角色 {name}（{occ}）")
        self._save()

        stat_txt = " ".join(f"{k} {stats[k]}"
                            for k in ["STR", "DEX", "CON", "INT", "WIS", "CHA"])
        slot_txt = (" · 法術格 " + " ".join(f"L{k}×{v}"
                    for k, v in slots.items())) if slots else ""
        return (
            f"🧙 **{name}** — {occ} Lv1 "
            f"(玩家：{uname})\n"
            f"🛡 AC {ac} · ❤️ {hp}/{hp}{slot_txt}\n"
            f"📊 {stat_txt}\n"
            f"🎲 屬性由系統擲骰（4d6 取高 3）\n"
            f"🎒 起始裝備：{'、'.join(equipment) or '無'}。用 `/inventory` 查看裝備與招式。")

    @serialized("text")
    async def remove_character(self, name: str, user_id: str, confirmation: str) -> str:
        g = self.game
        entry = g.party.get(name)
        if not entry:
            return f"❓ 沒有角色「{name}」"
        if str(entry.get("owner_id", "")) != str(user_id):
            return f"🚫 只有 {entry.get('owner', '?')} 可以刪除 {name}。"
        if confirmation.strip().lower() != "remove confirm":
            return f"⚠️ 確認刪除 {name}？填 `remove confirm`"
        self._drop_characters([name])
        g.ledger.add(name, "meta", f"刪除了角色 {name}")
        self._save()
        return f"🗑️ 已刪除 {name}。"

    @serialized("text")
    async def transfer_item(self, user_id: str, item: str, to: str = "", qty: int = 1) -> str:
        g = self.game
        giver = next((n for n, e in g.party.items() if str(e.get("owner_id", "")) == str(user_id)), "")
        if not giver:
            return "❓ 找不到你的角色。"
        lines, _ = await self._execute(Intent("give", actor=giver, item=item, qty=qty,
            args={"to": to, "drop": not to}), user_id, "", "", None, None)
        return "\n".join(lines)

    @serialized("text")
    async def admin_give(self, char: str, item: str, qty: int = 1) -> str:
        g = self.game
        if char not in g.party:
            return f"❓ 沒有角色「{char}」"
        if type(qty) is not int or qty <= 0:
            return "❓ 數量必須是正整數；尚未給予物品。"
        g.give_item(char, item, qty)
        g.ledger.add("admin", "item", f"管理員給了 {char} {item}×{qty}", item=item, target=char)
        self._save()
        inv = "、".join(f"{n}×{q}" for n, q in g.inventory[char])
        return f"🎒 {char} 現在攜帶：{inv}"

    @serialized("text")
    async def admin_inspire(self, character: str = "") -> str:
        if character and character not in self.game.party:
            return f"❓ 沒有角色「{character}」"
        names = [character] if character else list(self.game.party)
        for name in names:
            self.game.add_cond(name, "inspired", None)
        self.game.ledger.add("admin", "cond", f"管理員給了 {'、'.join(names)} 靈感")
        self._save()
        return f"✨ {'、'.join(names)} 獲得**靈感**——下一次攻擊或引擎擲骰有優勢（用後即消耗）"

    @serialized("text")
    async def admin_xp(self, amount: int, character: str = "") -> str:
        if character and character not in self.game.party:
            return f"❓ 沒有角色「{character}」"
        from engine.loot import add_xp
        lines = []
        add_xp(self.game, amount, lines, character)
        self._save()
        body = "\n".join(lines) or f"✅ XP {amount:+d} 已入帳（無升級）"
        return f"⭐ {character or '全隊'} XP {amount:+d}\n" + body

    def _context_noteworthy(self, idx0: int) -> bool:
        """0c: when does the machine block earn its place? Situation
        changes (scene, combat, loot/deaths, level-ups, beats), a pending
        check card, anyone downed — or every 3rd turn as a heartbeat."""
        g = self.game
        kinds = {e.kind for e in g.ledger.entries[idx0:]}
        if kinds & {"scene", "combat", "loot", "xp", "beat",
                    "director_scene", "reveal"}:
            return True
        if getattr(g, "_pending_check", None) is not None:
            return True
        if any(isinstance(e, dict) and int(e.get("hp_now", 1) or 0) <= 0
               for e in g.party.values()):
            return True
        return g.ledger.turn % 3 == 0

    # ---------- narration (shared by turns and roll settles) ----------

    async def _narrate(self, idx0: int, player_input: str = "",
                       on_delta=None, wants_help: bool = False) -> str:
        """Turn the ledger entries since idx0 into guarded prose.
        wants_help comes from the digestor's intent judgement; the
        keyword regex is only the no-LLM fallback."""
        g = self.game
        from .templates import render_hint
        from .guards import (make_placeholder_map, make_restore_map,
                             map_out, map_in, map_stream_in, scrub_narration,
                             is_repetition, is_chinese, is_dialogue_echo)
        end_id = (self._unfinished or {}).get("end_event_id", 2**63)
        events = [e for e in g.ledger.entries[idx0:] if e.event_id <= end_id
                  and e.kind not in ("table", "narration", "guard")
                  and e.data.get("visibility") != "private"]
        hints = [h for h in (render_hint(e, g) for e in events) if h]
        facts = [e.text for e in events]
        brief = "；".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                         for n, e in g.party.items())
        # pass NPC knowledge constraints if this was a talk/insight turn;
        # a reveal entry carries its own npc + the exact fact
        npc_knows, npc_name = None, ""
        for e in events:
            if e.kind in ("talk", "insight", "reveal") \
                    and e.data.get("npc"):
                npc_name = e.data["npc"]
                if e.kind == "reveal":
                    npc_knows = [e.data["fact"]]
                    break
                for n in g.scene_npcs():
                    if n["name"] == npc_name:
                        npc_knows = n.get("disclosed", [])
                        break
        no_information = next((e for e in events if e.kind == "talk"
            and e.data.get("npc") == npc_name
            and e.data.get("information") in ("exhausted", "unavailable")), None)
        if no_information:
            from .templates import conversation_next_step
            hints.append(conversation_next_step(g))
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
        if self._guidance():
            help_bits.append(self._guidance())
        if (self._unfinished or {}).get("stall_hint"):
            help_bits.append(self._unfinished["stall_hint"])
        if (self._unfinished or {}).get("narrative"):
            help_bits.append("玩家創意摘要只描述嘗試，不是成功事實。請呼應其中細節，"
                             "維持世界的黑暗基調，可帶機智與黑色幽默；後果必須符合引擎事實："
                             + json.dumps(self._unfinished["narrative"], ensure_ascii=False))
        reveals = [e for e in events
                   if e.kind == "reveal"]
        if reveals and npc_name:
            # the engine already picked WHICH fact comes out this turn —
            # the narrator must voice exactly it, as dialogue
            fact = reveals[0].data.get("fact", "")
            help_bits.append(
                f"這段敘事必須讓 {npc_name} 以對話形式說出「{fact}」"
                "——這是引擎判定打聽到的新情報，逐字融入對白。")
        elif no_information:
            help_bits.append(
                f"{npc_name} 這次交談沒有新情報。必須如實表明，不能聲稱發現新線索。"
                "如需回顧，只能重述上述已公開的事實；由 DM 提出骨架句中的可行下一步，"
                "不能把建議當成 NPC 的新知識。")
        elif npc_knows and npc_name:
            for e in events:
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
        extra = map_out("\n· ".join(directives), pmap) if directives else None
        # item inventory: what the narrator may mention
        s = g.world.here
        scene_items = [map_out(n, pmap) for n, _ in s.ground_items]
        party_inv = {map_out(ch, pmap): [map_out(n, pmap) for n, _ in stacks]
                     for ch, stacks in g.inventory.items()}
        narrator = self._main_agent() if self.agent_config.enabled else self.narrator
        # Deliver prose only after all guards. Streaming unguarded drafts
        # previously exposed rejected content before the authoritative edit.
        stream_delta = (lambda text: None) if on_delta else None
        if any(e.kind in ("theft", "theft_attempt", "creative_attempt", "item_transfer", "possession_established", "condition")
               or (e.kind in ("improvisation", "improvisation_cancelled") and e.data.get("plan_id")) for e in events):
            # Ownership and injuries must be exact. These bounded outcomes use
            # engine prose, including pending/failure, without a model draft.
            narration = "\n".join(safe_hints)
        else:
            narration = await narrator.narrate(
                safe_facts, map_out(g.world.here.name, pmap), safe_brief, hints=safe_hints,
                npc_knows=safe_knows, npc_name=map_out(npc_name, pmap),
                extra_directive=extra, on_delta=stream_delta,
                player_input=map_out(player_input, pmap),
                scene_items=scene_items, party_inventory=party_inv,
                world=map_out(world_block(self.rules), pmap),
                memory=map_out(build_memory(g, player_input, npc_name=npc_name).render(), pmap))
        # review trail: log the pair so output quality is auditable
        turn = current_turn()
        log.debug("narrate | trace=%s in=%s hints=%d out=%s",
                  turn.trace_id if turn else "-", self.gateway.diagnostic(player_input, 100),
                  len(hints), self.gateway.diagnostic(narration, 250))
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
        log.debug("item guard | scene=%s known=%d found=%s",
                  self.gateway.diagnostic(g.world.here.name), len(all_known),
                  self.gateway.diagnostic(found_items))
        if was_scrubbed_n:
            g.ledger.add("engine", "guard",
                         "已從敘事中清除假骰/判定文字")
        # v3 guard: language check — if still not Chinese, degrade
        if narration and not is_chinese(narration):
            narration = ""  # reject non-Chinese output
        statements = [map_out(text, pmap) for e in events if e.kind == "talk"
                      and (u := e.data.get("utterance"))
                      for text in (u, e.text, f"{e.actor}：{u}")]
        if narration and (is_dialogue_echo(narration, statements) or is_repetition(map_in(narration, rmap), self._recent_narrations)):
            log.info("narration rejected | trace=%s reason=dialogue_echo", turn.trace_id if turn else "-")
            narration = ""
        from .guards import narration_identity_valid
        if narration and not narration_identity_valid(map_in(narration, rmap), events, list(g.party),
                                                       [n["name"] for n in g.scene_npcs()]):
            log.info("narration rejected | trace=%s reason=actor_mismatch", turn.trace_id if turn else "-")
            narration = "\n".join(safe_hints)
        # /continue bookkeeping on the SAFE (placeholder) text the LLM
        # actually produced: empty = it never spoke; no terminal
        # punctuation = the prose was cut mid-sentence (max_tokens /
        # dropped stream) — both leave resumable state
        raw = narration.strip().rstrip("*").rstrip()
        if not raw or raw[-1] not in _END_PUNCT or self._narrator_interrupted():
            job = self._unfinished or self._narration_job(idx0, player_input, wants_help)
            self._unfinished = dict(job, text=narration)
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
            g.ledger.add("narrator", "narration", narration)
            if on_delta:
                on_delta(narration)
        return narration

    @serialized()
    async def settle_roll(self, die: int | None = None, on_delta=None,
                          on_resolved=None, *, user_id: str = "", admin: bool = False,
                          actor: str = "", expected_event_id: int | None = None):
        """Only the check owner (or an authenticated adapter admin) may settle."""
        from .rules_core import resolve_pending_check
        g = self.game
        pend = getattr(g, "_pending_check", None)
        if not pend:
            return ["（沒有待定檢定）"], ""
        if expected_event_id is not None and pend.get("event_id") != expected_event_id:
            return self._deny("?", "待定檢定已變更，請重新擲骰。")
        owner = str(g.party.get(pend["actor"], {}).get("owner_id", ""))
        if not admin and (str(user_id) != pend.get("user_id", owner)
                          or (owner and str(user_id) != owner)):
            return self._deny("?", "只有待定檢定的角色擁有者可以擲骰。")
        if actor and actor != pend["actor"]:
            return self._deny(actor, "待定檢定屬於另一個角色。")
        if pend.get("scene", g.world.current) != g.world.current:
            return self._deny(pend["actor"], "待定檢定的場景已變更。")
        if die is not None and (type(die) is not int or not 1 <= die <= 20):
            return self._deny(pend["actor"], "d20 必須是 1 至 20 的整數。")
        if die is None:
            die = g.d20()
        idx0 = len(g.ledger.entries)
        baseline = clone_game(g)
        if self.agent_config.routing.enabled:
            self._attempt = {"before": progress_key(g, self.plot_state), "eligible": False, "wants_help": False}
        ok, line = resolve_pending_check(g, die)
        lines = [f"🎲 **{pend['actor']}** d20 → **{die}**"]
        if line:
            lines.append(line)
        from .campaign import sync_starter
        lines += sync_starter(g)
        if self.plot:
            beat_lines, _ = plot_tick(g, self.plot, self.plot_state)
            lines += beat_lines
        from .templates import render_turn_context
        lines += ["", render_turn_context(g, pend["actor"])]
        verdict = f"{pend['actor']} 擲骰 d20={die}：{'成功' if ok else '失敗'}"
        player_input = f"{pend.get('origin', '')}（{verdict}）"
        self._unfinished = self._narration_job(idx0, player_input, bool(pend.get("help")))
        if pend.get("narrative"):
            self._unfinished["narrative"] = pend["narrative"]
        self._add_stall_hint(lines)
        self._queue_progress(idx0, baseline, wants_help=bool(pend.get("help")))
        self._save()
        return await self._deliver(lines, idx0, player_input, on_delta, on_resolved,
                                   wants_help=bool(pend.get("help")))

    def has_unfinished(self) -> bool:
        u = self._unfinished
        if not u:
            return False
        ids = {e.event_id for e in self.game.ledger.entries}
        return u.get("start_event_id") in ids and u.get("end_event_id") in ids

    @serialized("continue")
    async def continue_narration(self, on_delta=None) -> tuple[bool, str]:
        u = self._unfinished
        if not u:
            return False, ""
        if not self.has_unfinished():
            self._unfinished = None
            self._save(narration_only=True)
            return False, ""
        # Retry a deferred/failed review with this command's budget, never the action.
        if not u.get("guidance"):
            await self._review_progress(u.get("player_input", ""))
        from .guards import (make_restore_map, make_placeholder_map, map_out,
                             map_in, map_stream_in, scrub_narration, is_chinese, validate_narration_items)
        if u["text"]:
            pmap = make_placeholder_map(list(self.game.party))
            rmap = make_restore_map(list(self.game.party))
            stream_delta = (lambda text: None) if on_delta else None
            npc_name = u.get("npc_name", "")
            context = world_block(self.rules) + "\n" + build_memory(
                self.game, npc_name=npc_name).render()
            context += "\n" + self._guidance()
            if npc_name:
                context += f"\n{npc_name} 的對白只能引用當前狀態中該 NPC 的 facts，不能引用其他人的情報。"
            try:
                narrator = self._main_agent() if self.agent_config.enabled else self.narrator
                cont = await narrator.continue_text(
                    u["text"], stream_delta, context=map_out(context, pmap))
            except Exception:
                log.exception("continuation unavailable")
                return False, ""
            try:
                from opencc import OpenCC
                cont = OpenCC("s2t").convert(cont)
            except ImportError:
                pass
            cont, _ = scrub_narration(cont)
            known = [n for n, _ in self.game.world.here.ground_items] + [
                n for stacks in self.game.inventory.values() for n, _ in stacks]
            cont, _ = validate_narration_items(cont, known)
            if not cont or not is_chinese(cont):
                return False, ""
            from .guards import narration_identity_valid
            events = [e for e in self.game.ledger.entries
                      if u["start_event_id"] <= e.event_id <= u["end_event_id"]
                      and e.kind not in ("table", "narration", "guard")
                      and e.data.get("visibility") != "private"]
            if any(e.kind in ("theft", "theft_attempt", "creative_attempt", "item_transfer", "possession_established", "condition")
                   or (e.kind in ("improvisation", "improvisation_cancelled") and e.data.get("plan_id")) for e in events):
                # Theft prose is complete and engine-owned, including after
                # restoring an older unfinished narration job.
                return False, ""
            if not narration_identity_valid(map_in(u["text"] + cont, rmap), events, list(self.game.party),
                                             [n["name"] for n in self.game.scene_npcs()]):
                log.info("continuation rejected | reason=actor_mismatch")
                return False, ""
            full = u["text"] + cont
            raw = full.strip().rstrip("*").rstrip()
            self._unfinished = (dict(u, text=full)
                                if not raw or raw[-1] not in _END_PUNCT or self._narrator_interrupted() else None)
            text = map_in(cont, rmap)
            self.game.ledger.add("narrator", "narration", text)
            self._recent_narrations = (self._recent_narrations + [text])[-5:]
            self._save(narration_only=True)
            if on_delta:
                on_delta(text)
            return True, text
        idx0 = next(i for i, e in enumerate(self.game.ledger.entries)
                    if e.event_id == u["start_event_id"])
        try:
            narration = await self._narrate(idx0, u["player_input"], on_delta,
                                            wants_help=u.get("wants_help", False))
        except Exception:
            log.exception("renarration unavailable")
            return False, ""
        self._save(narration_only=True)
        return bool(narration), narration

    def nudge(self) -> str:
        """Read-only guidance: never send unfiltered ledger text to a model."""
        from .templates import render_turn_context
        if self.pending:
            p = self.pending
            return f"❓ 待確認：{p.actor} {p.action} {p.target or p.destination}——用 `/confirm`。"
        if getattr(self.game, "_pending_check", None):
            return f"🎲 等待 {self.game._pending_check['actor']} 的 `/roll d20`；原玩家或管理員可用 `/plan cancel` 取消。"
        from .action_plans import current_plan, describe
        active = [describe(plan) for name in self.game.party
                  if (plan := current_plan(self.game, name))]
        if active:
            return "\n".join(active) + "\n用 `/plan status` 查看、`/confirm` 繼續或 `/plan cancel` 取消。"
        from .campaign import objective_hint
        hint = objective_hint(self.game)
        return ((hint + "\n" if hint else "") + render_turn_context(self.game, "")) if self.game.party else "❓ 先用 `/pc` 建立角色。"
