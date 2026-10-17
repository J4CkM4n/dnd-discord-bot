"""Jev contracts, fallback routing, specialist delegation and turn budgets."""
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from core.agent_config import ConfigError, load_agent_config, parse_config
from core.decisions import RouteDecision, routing_questions, validate_answers
from core.debug_capture import DebugCapture
from core.model_gateway import ModelError
from core.service import V4Service, load_gamerules
from core.state import encode_game
from test_campaign import run, decisions
from test_operators import EXECUTE_ACTION, Endpoint, supervised, roles, audits


def answer(questions, **selections):
    defaults = {"category": "search", "target": "none", "destination": "none"}
    result = {}
    for key, question in questions.items():
        selected = selections.get(key, defaults.get(key, "approved" if "approved" in question["criteria"] else "no"))
        result[key] = {"type": "choice", "choice": selected,
                       "probabilities": {option: float(option == selected) for option in question["criteria"]},
                       "confidence": 1.0}
    return {"answers": result}


def router_answer(**selections):
    def response(body):
        data = json.loads(body["messages"][0]["content"].split("\n", 1)[1])
        return answer(data["questions"], **selections)
    return response


def routed(tmp_path, endpoint, *, native=False, selection=None, native_error=None, native_transform=None):
    s = supervised(tmp_path, endpoint)
    cfg = replace(s.agent_config, routing=replace(s.agent_config.routing, enabled=True,
                  api_key="native-private" if native else "", backend="auto"))
    s.agent_config = s.gateway.config = cfg
    native_requests = []
    def handler(request):
        if request.url.path.endswith("/systemone"):
            body = json.loads(request.content)
            native_requests.append(body)
            if native_error:
                return httpx.Response(native_error)
            result = answer(body["questions"], **(selection or {}))
            if native_transform:
                native_transform(result)
            return httpx.Response(200, json=result)
        return endpoint(request)
    s.gateway.transport = httpx.MockTransport(handler)
    s.debug_capture = DebugCapture(tmp_path, cfg, enabled=False)
    return s, native_requests


@pytest.mark.parametrize("routing_enabled,switch,expected", [
    (True, None, True), (True, "true", True), (True, "false", False),
    (True, "OFF", False), (True, " 0 ", False), (False, "true", False),
])
def test_system_one_env_gate_at_startup(tmp_path, routing_enabled, switch, expected):
    path = tmp_path / "agents.toml"
    path.write_text(f"[routing]\nenabled = {str(routing_enabled).lower()}\n")
    env = {} if switch is None else {"V4_SYSTEM_ONE_ENABLED": switch}
    cfg = load_agent_config(str(path), env)
    assert cfg.routing.enabled is expected
    assert cfg.enabled  # Other model roles remain available.


@pytest.mark.parametrize("routing_enabled", [True, False])
def test_invalid_system_one_env_switch_is_rejected(routing_enabled):
    with pytest.raises(ConfigError, match="V4_SYSTEM_ONE_ENABLED"):
        parse_config({"routing": {"enabled": routing_enabled}},
                     {"V4_SYSTEM_ONE_ENABLED": "invalid"})


@pytest.mark.parametrize("native", [True, False])
def test_system_one_disabled_skips_all_routing_and_executes_action(tmp_path, native):
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION],
                        action=[{"action": "search"}])
    s, requests = routed(tmp_path, endpoint, native=native)
    cfg = parse_config({"roles": {r: {"model": r} for r in s.agent_config.roles},
                        "routing": {"enabled": True}},
                       {"V4_SYSTEM_ONE_ENABLED": "false",
                        "TYPESAFE_API_KEY": "native-private" if native else ""})
    s.agent_config = s.gateway.config = cfg
    s.game.world.here.hidden_items = [("破紙", 1)]
    lines, prose = run(s.handle("搜索", user_id="u1"))
    assert lines and prose and s.game.ledger.entries
    assert not requests and not endpoint.unexpected
    assert roles(endpoint) == ["main", "action", "main", "narrator"]
    assert not any(r["role"] == "router" for r in audits(s))


def test_native_fast_path_excludes_secrets_and_uses_one_decision_call(tmp_path):
    endpoint = Endpoint()
    s, requests = routed(tmp_path, endpoint, native=True)
    s.game.world.here.hidden_items = [("私密道具", 1)]
    s.game.world.here.npcs[0]["knows"].append("未公開機密")
    s.game.world.here.search_dc = 10
    lines, _ = run(s.handle("搜索", user_id="u1"))
    assert lines and len(requests) == 1
    payload = json.dumps(requests[0], ensure_ascii=False)
    assert "未公開機密" not in payload and "私密道具" not in payload
    assert "owner_id" not in payload and "u1" not in payload
    assert "action" not in roles(endpoint) and "main" not in roles(endpoint)
    assert roles(endpoint) == ["narrator"]
    assert [r for r in audits(s) if r.get("provider") == "typesafe"][0]["call_count"] == 1


def test_jev_failure_falls_back_once_and_counts_all_calls(tmp_path):
    endpoint = Endpoint(router=[router_answer()], action=[{"action": "search"}])
    s, requests = routed(tmp_path, endpoint, native=True, native_error=503)
    s.game.world.here.hidden_items = [("破舊木雕", 1)]
    run(s.handle("搜索", user_id="u1"))
    assert len(requests) == 1 and roles(endpoint).count("router") == 1
    calls = [r for r in audits(s) if r.get("kind") == "model_call"]
    assert len(calls) <= 6 and any(r.get("http_status") == 503 for r in calls)


def test_uncalibrated_llm_decision_requires_specialist_without_complete_parse(tmp_path):
    endpoint = Endpoint(router=[router_answer(category="talk", target="t0")],
                        action=[{"action": "talk", "target": "船長", "utterance": "你好"}])
    s, _ = routed(tmp_path, endpoint)
    run(s.handle("嗰位獨眼佬，我想同你傾下", user_id="u1"))
    assert roles(endpoint)[:2] == ["router", "action"]


@pytest.mark.parametrize("selection", [{"compound": "yes"}, {"ambiguous": "yes"}])
def test_compound_and_hypothetical_never_execute_partial_attacks(tmp_path, selection):
    endpoint = Endpoint(action=[{"action": "unknown", "confidence": 0}])
    s, _ = routed(tmp_path, endpoint, native=True, selection={"category": "attack", **selection})
    before = deepcopy(s.game.party)
    turn = s.game.ledger.turn
    lines, _ = run(s.handle("如果我打船長，然後去酒館呢？", user_id="u1"))
    assert "❓" in "".join(lines)
    assert s.game.party == before and s.game.ledger.turn == turn
    assert not s.game.combat.active


def test_typed_commands_bypass_both_router_and_supervisor(tmp_path):
    endpoint = Endpoint()
    s, requests = routed(tmp_path, endpoint, native=True)
    s.game.world.here.hidden_items = [("破紙", 1)]
    run(s.handle("搜索", user_id="u1", structured=True))
    assert not requests and not endpoint.unexpected
    assert roles(endpoint) == ["narrator"]


def proposal(target="老船長", **overrides):
    return {"method": "把破帽子當作戲台演一齣恐怖短劇", "skill": "performance",
            "difficulty": "hard", "costs": [], "success": [{"kind": "npc_reaction", "target": target}],
            "failure": [], **overrides}


def creative_endpoint(*, approve=True, raw=None):
    return Endpoint(router=[router_answer(category="creative", improvisation="yes", creative_detail="yes")],
        action=[{"action": "creative", "utterance": "演一齣恐怖短劇"}],
        director=[raw or proposal()], evaluator=[{"approved": approve}], main=[{"approved": True}])


def test_improvisation_survives_restart_and_settles_exactly_once(tmp_path):
    endpoint = creative_endpoint()
    s, _ = routed(tmp_path, endpoint)
    original = "我把破帽子當戲台，演船長被幽靈追債"
    lines, _ = run(s.handle(original, user_id="u1"))
    pending = s.game._pending_check
    assert pending["effect"]["kind"] == "improvisation"
    assert pending["narrative"]["player_words"] == original
    assert pending["dc"] == 18 and "成功" not in "".join(lines)
    assert len(roles(endpoint)) == 6
    # No rerouting or regeneration when the stored check settles.
    restored = V4Service(str(tmp_path), channel_id="t", gateway=s.gateway)
    run(restored.settle_roll(20, user_id="u1"))
    count = sum(e.kind == "improvisation" for e in restored.game.ledger.entries)
    assert count == 1
    run(restored.settle_roll(20, user_id="u1"))
    assert sum(e.kind == "improvisation" for e in restored.game.ledger.entries) == count
    assert roles(endpoint).count("director") == 1


@pytest.mark.parametrize("approved", [False, True])
def test_rejected_or_invalid_proposal_leaves_world_unchanged(tmp_path, approved):
    raw = proposal(success=[{"kind": "damage", "target": "船長", "amount": 999}]) if approved else proposal()
    endpoint = creative_endpoint(approve=approved, raw=raw)
    s, _ = routed(tmp_path, endpoint)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle("用帽子演恐怖短劇", user_id="u1"))
    assert "❓" in "".join(lines)
    assert encode_game(s.game) == before


def test_creative_budget_allows_native_failure_plus_all_required_reviews(tmp_path):
    endpoint = creative_endpoint()
    s, requests = routed(tmp_path, endpoint, native=True, native_error=503)
    run(s.handle("用帽子演恐怖短劇", user_id="u1"))
    calls = [r for r in audits(s) if r.get("kind") == "model_call"]
    assert len(requests) == 1 and len(calls) == 7
    assert s.game._pending_check and roles(endpoint).count("evaluator") == 1


def test_distribution_validation_rejects_unknown_choices_nonfinite_and_incomplete(tmp_path):
    s, _ = routed(tmp_path, Endpoint())
    questions, _, _ = routing_questions(s.game)
    for mutation in (lambda r: r["answers"].pop("category"),
                     lambda r: r["answers"]["category"].update(choice="teleport"),
                     lambda r: r["answers"]["category"]["probabilities"].update(search=float("nan"))):
        raw = answer(questions)
        mutation(raw)
        with pytest.raises(ModelError):
            validate_answers(raw, questions)
    route = RouteDecision(1, "jev", {"category": {"choice": "search", "probabilities": {"search": 1}, "confidence": None}})
    assert not route.certain("category", s.agent_config.routing)


@pytest.mark.parametrize("data", [
    {"routing": {"probability": 2}}, {"routing": {"timeout": 0}},
    {"routing": {"base_url": "https://name:SECRET@host"}},
    {"routing": {"creative_max_calls": 9}}, {"hints": {"cooldown": 0}},
])
def test_new_configuration_is_validated_without_exposing_credentials(data):
    with pytest.raises(ConfigError) as exc:
        parse_config(data, {})
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize("confidence", [None, 0.1])
def test_native_uncertain_decision_delegates_to_action_specialist(tmp_path, confidence):
    endpoint = Endpoint(action=[{"action": "search"}])
    def uncertain(raw):
        if "category" in raw["answers"]:
            raw["answers"]["category"]["confidence"] = confidence
    s, requests = routed(tmp_path, endpoint, native=True, native_transform=uncertain)
    s.game.world.here.hidden_items = [("破紙", 1)]
    run(s.handle("搜索", user_id="u1"))
    assert roles(endpoint)[:1] == ["action"]
    assert len(requests) == 2 and "intent_fidelity" in requests[-1]["questions"]
    assert s.game.ledger.turn > 0 and "main" not in roles(endpoint)


@pytest.mark.parametrize("text, executes", [("搜索", True), ("搜索，然後攻擊船長", False)])
def test_model_outage_executes_only_complete_deterministic_requests(tmp_path, text, executes):
    endpoint = Endpoint(router=[httpx.Response(503)], action=[httpx.Response(503)])
    s, _ = routed(tmp_path, endpoint)
    s.game.world.here.hidden_items = [("破紙", 1)]
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(text, user_id="u1"))
    assert (s.game.ledger.turn > before["turn"]) == executes
    if not executes:
        assert "❓" in "".join(lines) and encode_game(s.game) == before


def test_flavored_attack_preserves_engine_mechanics_and_dark_narrative_brief(tmp_path):
    original = "我高喊『海底的債主來了』，握緊武器攻擊老船長"
    endpoint = Endpoint(router=[router_answer(category="attack", target="t0", creative_detail="yes")],
                        action=[{"action": "attack", "target": "老船長"}])
    s, _ = routed(tmp_path / "freeform", endpoint)
    s.rules = load_gamerules()
    typed, _ = routed(tmp_path / "typed", Endpoint())
    run(s.handle(original, user_id="u1"))
    run(typed.handle("攻擊老船長", user_id="u1", structured=True))
    assert encode_game(s.game) == encode_game(typed.game)
    prompt = next(p for r, p in endpoint.calls if r == "narrator")
    assert original in prompt and "黑暗海洋奇幻" in prompt
    assert "黑色幽默" in prompt and "不是成功事實" in prompt
    assert roles(endpoint) == ["router", "action", "narrator"]


@pytest.mark.parametrize("reject", ["none", "evaluator", "main"])
def test_new_scene_needs_all_reviews_before_commit_and_delivery(tmp_path, reject):
    endpoint = Endpoint(router=[router_answer(category="move", destination="unknown")],
        action=[{"action": "move", "destination": "霧中小巷"}],
        director=[{"name": "霧中小巷", "description": "濕石板上滿是鹽痕。", "npc": ""}],
        evaluator=[{"relevant": True, "consistent": reject != "evaluator", "reason": "場景審核"}],
        main=[{"approved": reject != "main"}])
    s, _ = routed(tmp_path, endpoint)
    before = deepcopy(encode_game(s.game))
    delivered = []
    async def on_resolved(lines):
        delivered.append(s.repository.load("t")["snapshot"]["world"]["current"])
    lines, _ = run(s.handle("前往霧中小巷", user_id="u1", on_resolved=on_resolved))
    if reject == "none":
        assert s.game.world.current == "dir1" and delivered == ["dir1"]
        assert decisions(s)[-1]["accepted"]
    else:
        assert "❓" in "".join(lines) and encode_game(s.game) == before
        assert not delivered


def test_new_route_rechecks_model_actor_ownership(tmp_path):
    endpoint = Endpoint(router=[router_answer(category="attack", target="t0")],
                        action=[{"action": "attack", "actor": "大力蕉", "target": "老船長"}])
    s, _ = routed(tmp_path, endpoint)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle("請幫我挑釁眼前的船長", user_id="u1"))
    assert "🚫" in "".join(lines) and encode_game(s.game) == before
    assert "director" not in roles(endpoint)


def test_routed_creative_costs_and_effects_roll_back_on_failed_commit(tmp_path):
    endpoint = creative_endpoint(raw=proposal(costs=["破帽子"]))
    s, _ = routed(tmp_path, endpoint)
    s.game.give_item("依思", "破帽子", 1)
    s._save()
    before = s.repository.load("t")
    before_game = deepcopy(encode_game(s.game))
    with s.repository.connect() as conn:
        conn.execute("CREATE TRIGGER fail_save BEFORE INSERT ON events "
                     "BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END")
    lines, _ = run(s.handle("把帽子獻給船長，演一齣恐怖短劇", user_id="u1"))
    assert "未能儲存" in lines[0]
    assert s.repository.load("t") == before and encode_game(s.game) == before_game
    assert ["破帽子", 1] in s.game.inventory["依思"] and not getattr(s.game, "_pending_check", None)
