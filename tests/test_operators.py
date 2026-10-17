"""Controller integration: fake model endpoints, real engine and SQLite commits."""
import json
from collections import defaultdict, deque
from copy import deepcopy

import httpx
import pytest

from core.agent_config import ROLES, parse_config
from core.digestor import Digestor
from core.director_llm import DirectorLLM
from core.model_gateway import ModelError, ModelGateway
from core.narrator import Narrator
from core.operators import OperatorReport, progress_input
from core.persistence import CampaignRepository, StorageError
from core.service import V4Service
from core.state import encode_game
from core.world import Enemy
from test_campaign import run, service, decisions

EXECUTE_PARSED = {"decision": "execute", "intent": "parsed", "director": False}
EXECUTE_ACTION = {"decision": "execute", "intent": "action", "director": False}
PROGRESS = {"objectives": [], "stalled": False, "repetition": False, "evidence": [], "options": [0]}


class Endpoint:
    def __init__(self, **responses):
        self.responses = defaultdict(deque, {r: deque(values) for r, values in responses.items()})
        self.calls = []
        self.unexpected = []

    def __call__(self, request):
        body = json.loads(request.content)
        role = body["model"]
        self.calls.append((role, body["messages"][0]["content"]))
        if self.responses[role]:
            value = self.responses[role].popleft()
            if callable(value):
                value = value(body)
        elif role == "narrator":
            value = "海風輕拂，眾人望向前方。"
        else:
            self.unexpected.append(role)
            return httpx.Response(503)
        if isinstance(value, httpx.Response):
            return value
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return httpx.Response(200, json={"message": {"content": content}})


def supervised(tmp_path, endpoint, *, evaluate=False, max_calls=6):
    s = service(tmp_path)
    cfg = parse_config({"roles": {r: {"model": r} for r in ROLES},
        "supervision": {"max_calls": max_calls, "director_evaluate": evaluate}}, {})
    s.agent_config = cfg
    s.gateway = ModelGateway(cfg, httpx.MockTransport(endpoint))
    s.digestor, s.director = Digestor(gateway=s.gateway), DirectorLLM(gateway=s.gateway)
    s.narrator = Narrator(gateway=s.gateway)
    return s


def audits(s):
    with s.repository.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM operator_reports WHERE channel_id=? ORDER BY id", (s.channel_id,))]


def roles(endpoint):
    return [r for r, _ in endpoint.calls]


def test_freeform_help_is_six_calls_and_guidance_is_separate_from_facts(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action", "memory"]}, EXECUTE_ACTION, {"options": [0]}],
                        action=[{"action": "meta", "wants_help": True}], progress=[PROGRESS])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, prose = run(s.handle("any ideas? stuck 咗", user_id="u1"))
    assert lines and prose and not endpoint.unexpected
    assert roles(endpoint) == ["main", "action", "main", "progress", "main", "narrator"]
    assert s.game.inventory == before["inventory"]
    assert s._operator_state["pending_triggers"] == []
    assert s._operator_state["accepted_turns"] == 0  # help is not a gameplay action
    narrator_prompt = endpoint.calls[-1][1]
    assert "主代理已審核的可選下一步" in narrator_prompt
    records = audits(s)
    assert any(r["role"] == "memory" and r["provider"] == "deterministic" for r in records)
    model_calls = [r for r in records if r["kind"] == "model_call"]
    assert len(model_calls) == 6 and max(r["call_count"] for r in model_calls) == 6
    assert all(r["provider"] == "local" and "latency_ms" in r for r in model_calls)
    assert model_calls[3]["source_revision"] > model_calls[0]["source_revision"]
    assert all(e.kind not in ("progress", "operator_report", "guidance") for e in s.game.ledger.entries)


@pytest.mark.parametrize("plan", [{"consult": ["action", "action"]}, {"consult": ["shell"]},
                                 {"consult": []}, {"consult": ["action"], "write_hp": 999}, "not-json"])
def test_invalid_delegation_clarifies_without_mutation(tmp_path, plan):
    endpoint = Endpoint(main=[plan])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, _ = run(s.handle("請幫我處理一下那個", user_id="u1"))
    assert "格式未通過驗證" in lines[0] and roles(endpoint) == ["main"]
    assert encode_game(s.game) == before
    assert any(r["status"] == "fallback" for r in audits(s))


def test_main_disagreement_does_not_execute_action_or_advance_plot(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]},
        {"decision": "clarify", "intent": "action", "director": False}],
        action=[{"action": "attack", "target": "船長"}])
    s = supervised(tmp_path, endpoint)
    before, plot = encode_game(s.game), deepcopy(s.plot_state)
    lines, _ = run(s.handle("想和那位較量一下", user_id="u1"))
    assert "❓" in lines[0] and encode_game(s.game) == before and s.plot_state == plot
    assert roles(endpoint) == ["main", "action", "main"]


def test_main_failure_uses_only_deterministic_candidate(tmp_path):
    endpoint = Endpoint(main=[httpx.Response(503)])
    s = supervised(tmp_path, endpoint)
    lines, _ = run(s.handle("依思 rest", user_id="u1"))
    assert lines and s.game.ledger.turn == 1
    assert roles(endpoint) == ["main", "narrator"]


def test_no_model_calls_for_unowned_character_or_status(tmp_path):
    endpoint = Endpoint()
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, _ = run(s.handle("大力蕉 go 小巷", user_id="u1"))
    assert "🚫" in lines[0] and not endpoint.calls
    assert encode_game(s.game) == before
    from core.cli import META
    run(s.handle(next(iter(META)), user_id="u1"))
    assert not endpoint.calls


def test_interpreted_other_player_is_denied_before_generation(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action", "director"]}, EXECUTE_ACTION],
        action=[{"action": "move", "actor": "大力蕉", "destination": "新地方"}])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, _ = run(s.handle("偷偷帶另一位走", user_id="u1"))
    assert "🚫" in lines[0] and "director" not in roles(endpoint)
    assert encode_game(s.game) == before


def test_uncertain_action_requires_owner_confirmation(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action", "director"]}, EXECUTE_ACTION],
        action=[{"action": "move", "destination": "新地方", "confidence": .2}])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, _ = run(s.handle("往那邊試試", user_id="u1"))
    assert "/confirm" in lines[0] and s.pending_user_id == "u1"
    assert s.game.world.current == before["world"]["current"]
    assert roles(endpoint) == ["main", "action", "main"]
    lines, _ = run(s.confirm("u2"))
    assert "🚫" in lines[0] and s.pending


def test_director_proposal_requires_jev_main_approval_and_commit_before_delivery(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["director"]}, {**EXECUTE_PARSED, "director": True}, {"options": [0]}],
        director=[{"name": "霧中小巷", "description": "石板上覆著青苔。", "npc": ""}],
        evaluator=[{"relevant": True, "consistent": True, "reason": "符合"}], progress=[PROGRESS])
    s = supervised(tmp_path, endpoint)
    old_scene = s.game.world.current
    seen = []
    async def delivered(lines):
        record = s.repository.load(s.channel_id)
        seen.append(record["snapshot"]["world"]["current"])
        assert "progress" not in roles(endpoint)
    lines, _ = run(s.handle("依思 go 小巷", user_id="u1", on_resolved=delivered))
    assert lines and s.game.world.current != old_scene and seen == [s.game.world.current]
    assert roles(endpoint) == ["main", "director", "evaluator", "main", "narrator"]
    assert s._operator_state["pending_triggers"] == ["scene"]
    assert decisions(s)[-1]["accepted"] is True


def test_main_rejects_director_proposal(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["director"]}, EXECUTE_PARSED],
        director=[{"name": "霧中小巷", "description": "石板上覆著青苔。", "npc": ""}],
        evaluator=[{"relevant": True, "consistent": True, "reason": "符合"}])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    run(s.handle("依思 go 小巷", user_id="u1"))
    assert encode_game(s.game) == before
    assert not decisions(s)[-1]["accepted"]


def test_jev_uses_evaluator_role_and_defers_progress_within_budget(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["director"]}, {**EXECUTE_PARSED, "director": True}, {"options": [0]}],
        director=[{"name": "霧中小巷", "description": "石板上覆著青苔。", "npc": ""}],
        evaluator=[{"relevant": True, "consistent": True, "reason": "符合意圖"}], progress=[PROGRESS])
    s = supervised(tmp_path, endpoint, evaluate=True)
    run(s.handle("依思 go 小巷", user_id="u1"))
    assert roles(endpoint) == ["main", "director", "evaluator", "main", "narrator"]
    assert s._operator_state["pending_triggers"] == ["scene"]
    assert any(r["role"] == "progress" and r["status"] == "deferred" for r in audits(s))
    assert decisions(s)[-1]["evaluation"] == "approved"
    run(s.handle("依思 rest", user_id="u1", structured=True))
    assert roles(endpoint)[-3:] == ["progress", "main", "narrator"]
    assert s._operator_state["pending_triggers"] == []


def test_typed_combat_only_invokes_narrator_until_milestone(tmp_path):
    endpoint = Endpoint()
    s = supervised(tmp_path, endpoint)
    enemy = Enemy.make("守衛", 100, 15)
    s.game.enemies = {"守衛": enemy}
    s.game.combat.order = [{"name": "依思", "npc": False}, {"name": "大力蕉", "npc": False},
                           {"name": "守衛", "npc": True}]
    s._save()
    run(s.handle("依思 defend", user_id="u1", structured=True))
    assert roles(endpoint) == ["narrator"]


def test_interval_trigger_and_restart_counter(tmp_path):
    endpoint = Endpoint(progress=[PROGRESS], main=[{"options": [0]}])
    s = supervised(tmp_path, endpoint)
    for _ in range(4):
        run(s.handle("依思 rest", user_id="u1", structured=True))
    assert roles(endpoint) == ["narrator"] * 4
    fresh = V4Service(str(tmp_path), channel_id="t", gateway=s.gateway)
    fresh.plot, fresh.rules = {}, {}
    assert fresh._operator_state["accepted_turns"] == 4
    run(fresh.handle("依思 rest", user_id="u1", structured=True))
    assert fresh._operator_state["accepted_turns"] == 5
    assert roles(endpoint)[-3:] == ["progress", "main", "narrator"]
    assert '"interval"' in endpoint.calls[-3][1]


def test_help_and_plot_milestones_coalesce_and_progress_never_ticks_plot(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION, {"options": [0]}],
        action=[{"action": "rest", "wants_help": True}], progress=[PROGRESS])
    s = supervised(tmp_path, endpoint)
    s._operator_state["accepted_turns"] = 4
    s.plot = {"clocks": [{"id": "clock", "max": 20, "start": 10}],
              "triggers": [{"id": "first", "when": {"turn_gte": 1}, "beat": "first"}],
              "beats": {"first": {"text": "鐘聲響起。", "effects": []}}}
    s.plot_state = {"clocks": {"clock": 10}, "last_turn": 0, "fired": [], "ended": None}
    s._save()
    run(s.handle("休息之前給我們一點建議", user_id="u1"))
    assert s.plot_state["clocks"]["clock"] == 11 and s.plot_state["fired"] == ["first"]
    prompts = [p for r, p in endpoint.calls if r == "progress"]
    assert len(prompts) == 1 and all(f'"{t}"' in prompts[0] for t in ("help", "interval", "plot_beat"))


def test_invalid_progress_evidence_is_audit_only_and_can_retry(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION],
        action=[{"action": "meta", "wants_help": True}],
        progress=[{**PROGRESS, "stalled": True, "evidence": [99999]}])
    s = supervised(tmp_path, endpoint)
    run(s.handle("any ideas", user_id="u1"))
    assert roles(endpoint) == ["main", "action", "main", "progress", "narrator"]
    assert s._operator_state["pending_triggers"] == ["help"]
    assert "主代理已審核的可選下一步" not in endpoint.calls[-1][1]
    assert any(r["status"] == "invalid" and r["role"] == "progress" for r in audits(s))


def test_progress_and_guidance_survive_restart_without_replaying_action(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION, {"options": [0]}],
        action=[{"action": "meta", "wants_help": True}], progress=[PROGRESS], narrator=[""])
    s = supervised(tmp_path, endpoint)
    run(s.handle("any ideas", user_id="u1"))
    assert s.has_unfinished() and s._unfinished["guidance"]["options"]
    count, turn = len(s.game.ledger.entries), s.game.ledger.turn
    continuation = Endpoint()
    gateway = ModelGateway(s.agent_config, httpx.MockTransport(continuation))
    fresh = V4Service(str(tmp_path), channel_id="t", gateway=gateway)
    fresh.rules, fresh.plot = {}, {}
    ok, text = run(fresh.continue_narration())
    assert ok and text and roles(continuation) == ["narrator"]
    assert fresh.game.ledger.turn == turn and len(fresh.game.ledger.entries) == count + 1
    assert "主代理已審核的可選下一步" in continuation.calls[0][1]
    assert not fresh.has_unfinished()


def test_failed_progress_commit_preserves_action_and_recovery(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION, {"options": [0]}],
        action=[{"action": "rest", "wants_help": True}], progress=[PROGRESS])
    s = supervised(tmp_path, endpoint)
    commit = s.repository.commit
    def fail_narration(channel, revision, snapshot, *args, **kwargs):
        if snapshot is None:
            raise StorageError("disk failure")
        return commit(channel, revision, snapshot, *args, **kwargs)
    s.repository.commit = fail_narration
    lines, _ = run(s.handle("想休息並求建議", user_id="u1"))
    assert "行動已儲存" in lines[0] and s.game.ledger.turn == 1
    record = s.repository.load(s.channel_id)
    assert record["unfinished"] and record["operator_state"]["pending_triggers"] == ["help"]
    assert not record["unfinished"].get("guidance")


def test_reports_reject_stale_revision_or_missing_event():
    report = OperatorReport("progress", 3, (9,), "{}")
    assert report.read(3, {9}) == {}
    with pytest.raises(ModelError):
        report.read(4, {9})
    with pytest.raises(ModelError):
        report.read(3, {8})


def test_progress_sees_only_public_objectives_and_npc_knowledge(tmp_path):
    s = service(tmp_path)
    s.game.world.here.hidden_items.append(("秘密寶藏", 1))
    s.game.world.here.npcs = [{"name": "甲", "knows": ["未公開密碼"], "disclosed": ["公開碼頭"]},
                             {"name": "乙", "knows": ["另一秘密"], "disclosed": ["另一人情報"]}]
    s.game.ledger.add("engine", "reveal", "另一人情報", npc="乙")
    s.game.ledger.add("engine", "reveal", "公開碼頭", npc="甲")
    s.game.ledger.add("narrator", "narration", "敘事捏造寶藏")
    plot = {"objectives": [{"id": "public", "text": "尋找路線", "public": True},
                            {"id": "secret", "text": "秘密終局"}], "beats": {"future": "未來劇情"}}
    data, evidence = progress_input(s.game, plot, "", "甲")
    text = json.dumps(data, ensure_ascii=False)
    for secret in ("秘密寶藏", "未公開密碼", "另一秘密", "另一人情報", "敘事捏造寶藏", "秘密終局", "未來劇情"):
        assert secret not in text
    assert "公開碼頭" in text and data["authored_objectives"] == {"public": "尋找路線"}
    assert 1 not in evidence and 2 in evidence


def test_v1_migration_preserves_events_and_separates_operator_state(tmp_path):
    s = service(tmp_path)
    run(s.handle("依思 rest", user_id="u1", structured=True))
    before = s.repository.load("t")
    with s.repository.connect() as conn:
        conn.execute("DROP TABLE operator_reports")
        conn.execute("DROP TABLE operator_state")
        conn.execute("PRAGMA user_version=1")
    migrated = CampaignRepository(s.repository.path)
    after = migrated.load("t")
    assert after["snapshot"] == before["snapshot"] and after["entries"] == before["entries"]
    assert after["operator_state"] == {}
    migrated = CampaignRepository(s.repository.path)  # migration is idempotent
    with migrated.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM operator_reports").fetchone()[0] == 0


def test_typed_combat_completion_triggers_progress_after_committing(tmp_path):
    endpoint = Endpoint(progress=[PROGRESS], main=[{"options": [0]}])
    s = supervised(tmp_path, endpoint)
    enemy = Enemy.make("守衛", 1, 1)
    s.game.enemies = {"守衛": enemy}
    s.game.combat.order = [{"name": "依思", "npc": False}, {"name": "大力蕉", "npc": False},
                           {"name": "守衛", "npc": True}]
    s._save()
    run(s.handle("依思 attack 守衛", user_id="u1", structured=True))
    assert not s.game.combat.active
    assert roles(endpoint) == ["progress", "main", "narrator"]
    assert '"combat_complete"' in endpoint.calls[0][1]


def test_judged_proposal_cannot_bypass_negative_evaluation(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["director"]}, {**EXECUTE_PARSED, "director": True}],
        director=[{"name": "霧中小巷", "description": "石板上覆著青苔。", "npc": ""}],
        evaluator=[{"relevant": False, "consistent": True, "reason": "不相關"}])
    s = supervised(tmp_path, endpoint, evaluate=True)
    before = encode_game(s.game)
    run(s.handle("依思 go 小巷", user_id="u1"))
    assert encode_game(s.game) == before
    assert decisions(s)[-1]["accepted"] is False
    assert decisions(s)[-1]["evaluation"] == "rejected"


def test_all_six_roles_share_one_command_budget(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action", "director"]}, {**EXECUTE_ACTION, "director": True}],
        action=[{"action": "move", "destination": "小巷", "confidence": .95}],
        director=[{"name": "霧中小巷", "description": "石板上覆著青苔。", "npc": ""}],
        evaluator=[{"relevant": True, "consistent": True, "reason": "符合"}])
    s = supervised(tmp_path, endpoint, evaluate=True)
    run(s.handle("走過去那邊的小巷看看", user_id="u1"))
    assert roles(endpoint) == ["main", "action", "director", "evaluator", "main", "narrator"]
    assert s.game.world.current != "dock" and s._operator_state["pending_triggers"] == ["scene"]
    assert max(r["call_count"] for r in audits(s)) == 6


def test_audit_and_game_commit_roll_back_together(tmp_path):
    endpoint = Endpoint(main=[{"consult": []}, EXECUTE_PARSED])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    with s.repository.connect() as conn:
        conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON operator_reports "
                     "BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    lines, _ = run(s.handle("依思 rest", user_id="u1"))
    assert "行動未執行" in lines[0]
    assert encode_game(s.game) == before
    assert s.repository.load("t")["snapshot"]["turn"] == 0
    assert not audits(s)


def test_truncated_stream_with_punctuation_remains_resumable(tmp_path):
    endpoint = Endpoint(narrator=[
        httpx.Response(200, text=json.dumps({"message": {"content": "海風拂過。"}})),
        httpx.Response(200, text=json.dumps({"message": {"content": "眾人繼續前行。"}, "done": True}))])
    s = supervised(tmp_path, endpoint)
    updates = []
    run(s.handle("依思 rest", user_id="u1", structured=True, on_delta=updates.append))
    assert updates and s.has_unfinished()
    assert any(r["role"] == "narrator" and r["status"] == "partial" for r in audits(s))
    run(s.continue_narration())
    assert not s.has_unfinished() and s.game.ledger.turn == 1


def test_model_guidance_must_be_subset_of_validated_progress_options(tmp_path):
    endpoint = Endpoint(main=[{"options": [999]}], progress=[PROGRESS])
    s = supervised(tmp_path, endpoint)
    s._operator_state["accepted_turns"] = 4
    run(s.handle("依思 rest", user_id="u1", structured=True))
    assert s.game.ledger.turn == 1 and s._operator_state["pending_triggers"] == ["interval"]
    assert "主代理已審核的可選下一步" not in endpoint.calls[-1][1]


def test_authored_and_inferred_objectives_are_distinguished():
    from core.operators import MainAgent
    data = {"authored_objectives": {"route": "尋找航路"}, "options": ["搜索。"]}
    good = {**PROGRESS, "objectives": [
        {"origin": "authored", "objective_id": "route", "title": "尋找航路", "evidence": []},
        {"origin": "inferred", "objective_id": "", "title": "可能想尋找線索", "evidence": [1]}]}
    MainAgent._validate_progress(good, {1}, data)
    invented = deepcopy(good)
    invented["objectives"][0]["title"] = "得到寶藏"
    with pytest.raises(ModelError):
        MainAgent._validate_progress(invented, {1}, data)
    completed = deepcopy(good)
    completed["objectives"][1]["completed"] = True
    with pytest.raises(ModelError):
        MainAgent._validate_progress(completed, {1}, data)
