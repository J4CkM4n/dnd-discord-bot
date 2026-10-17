"""Native operator reviews, exact fallback use, budgets and engine boundaries."""
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from core.decisions import DecisionGateway, MAX_REVIEW_CHARS, intent_questions
from core.debug_capture import DebugCapture
from core.model_gateway import current_turn, turn_context
from core.state import clone_game, encode_game
from core.trace_eval import evaluate, read_traces, replay
from test_campaign import run
from test_operators import Endpoint, EXECUTE_ACTION, PROGRESS, roles, supervised
from test_system_one import answer, proposal, router_answer


ATTACK = "我高喊海底的債主來了，握緊武器攻擊老船長"
ACTION_ROUTE = {"category": "attack", "target": "t0", "creative_detail": "yes"}
SCENE = {"name": "霧中小巷", "description": "潮濕石板通往碼頭旁的窄巷。", "npc": ""}
EVALUATED = {"relevant": True, "consistent": True, "reason": "符合原意和設定"}
FAILURES = ("uncertain", "low_probability", "low_confidence", "missing_confidence",
            "malformed", "timeout", "http_error")


def with_native(tmp_path, endpoint, *, selection=None, verdict="approved", native=True,
                backend="auto", enabled=True):
    s = supervised(tmp_path, endpoint)
    cfg = replace(s.agent_config, routing=replace(s.agent_config.routing, enabled=enabled,
                  api_key="native-private" if native else "", backend=backend))
    s.agent_config = s.gateway.config = cfg
    requests = []

    def handler(request):
        if not request.url.path.endswith("/systemone"):
            return endpoint(request)
        body = json.loads(request.content)
        requests.append(body)
        if "category" in body["questions"]:
            return httpx.Response(200, json=answer(body["questions"], **(selection or ACTION_ROUTE)))
        result = answer(body["questions"], **{k: "approved" for k in body["questions"]})
        if callable(verdict):
            verdict(body, result)
        else:
            first = result["answers"][next(iter(body["questions"]))]
            if verdict in ("rejected", "uncertain"):
                result = answer(body["questions"], **{k: verdict for k in body["questions"]})
            elif verdict == "low_probability":
                first["probabilities"] = {"approved": .84, "rejected": .16, "uncertain": 0}
            elif verdict == "low_confidence":
                first["confidence"] = .79
            elif verdict == "missing_confidence":
                first.pop("confidence")
            elif verdict == "malformed":
                result["answers"].pop(next(iter(body["questions"])))
            elif verdict == "timeout":
                raise httpx.ReadTimeout("private-provider-detail", request=request)
            elif verdict == "http_error":
                return httpx.Response(503)
        return httpx.Response(200, json=result)

    s.gateway.transport = httpx.MockTransport(handler)
    return s, requests


def interpret(s, text=ATTACK, *, limit=6):
    with turn_context(limit, s._revision) as turn:
        result = run(s._main_agent().interpret_routed(
            clone_game(s.game), s._revision, text, None, user_id="u1", rules=s.rules,
            plot_state=s.plot_state))
    return result, turn


def reviews(turn, operator):
    return [r for r in turn.records if r["role"] == operator and r["kind"] == "decision"]


def model_calls(turn):
    return [r for r in turn.records if r["kind"] == "model_call"]


def test_action_approval_uses_one_check_and_no_main_review(tmp_path):
    ep = Endpoint(action=[{"action": "attack", "target": "老船長"}])
    s, requests = with_native(tmp_path, ep)
    result, turn = interpret(s)
    assert result.intent.action == "attack" and result.intent.target == "老船長"
    assert roles(ep) == ["action"] and len(requests) == 2 and turn.calls == 3
    check = reviews(turn, "action")[0]
    assert check["status"] == "approved" and check["purpose"] == "intent_review"
    assert check["backend"] == "jev" and check["latency_ms"] >= 0 and not check["fallback_reason"]
    assert model_calls(turn)[-1]["role"] == "action"
    assert requests[-1]["state"]["player_input"] == ATTACK


@pytest.mark.parametrize("verdict", FAILURES)
def test_action_fallback_reviews_the_existing_candidate_exactly_once(tmp_path, verdict):
    ep = Endpoint(action=[{"action": "attack", "target": "老船長"}], main=[EXECUTE_ACTION])
    s, requests = with_native(tmp_path, ep, verdict=verdict)
    result, turn = interpret(s)
    assert result.intent.target == "老船長"
    assert roles(ep) == ["action", "main"] and len(requests) == 2 and turn.calls == 4
    assert ATTACK in ep.calls[-1][1] and '"action": "attack"' in ep.calls[-1][1]
    assert reviews(turn, "action")[0]["fallback_reason"]
    assert "private-provider-detail" not in repr(turn.records)


@pytest.mark.parametrize("text", [
    "不要攻擊老船長，只觀察他的手", "如果用匕首攻擊老船長會怎樣？",
    "只用帽子制服老船長，不要傷害他", "用繩索困住守衛，別攻擊老船長",
])
def test_action_rejection_preserves_negation_target_method_and_constraints(tmp_path, text):
    ep = Endpoint(action=[{"action": "attack", "target": "老船長"}])
    s, requests = with_native(tmp_path, ep, verdict="rejected")
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "❓" in "".join(lines) and encode_game(s.game) == before
    assert roles(ep) == ["action"] and len(requests) == 2
    assert requests[-1]["state"]["player_input"] == text
    instruction = requests[-1]["questions"]["intent_fidelity"]["instructions"]
    assert all(word in instruction for word in ("action", "target", "method", "negation", "constraint"))


@pytest.mark.parametrize("kind", ["scene", "item", "improvisation"])
def test_director_approval_replaces_evaluator_and_main_in_one_batch(tmp_path, kind):
    selection = {"category": "move", "destination": "unknown"} if kind == "scene" else (
        {"category": "search", "ambiguous": "yes"} if kind == "item" else
        {"category": "creative", "improvisation": "yes"})
    action = {"action": "move", "destination": "霧中小巷"} if kind == "scene" else (
        {"action": "search"} if kind == "item" else {"action": "creative", "utterance": "用帽子演戲"})
    candidate = SCENE if kind == "scene" else {"name": "褪色布條"} if kind == "item" else proposal()
    ep = Endpoint(action=[action], director=[candidate])
    s, requests = with_native(tmp_path, ep, selection=selection)
    s.game.world.here.hidden_items = []
    text = {"scene": "去霧中小巷", "item": "我翻找附近的角落", "improvisation": "我把帽子當舞台演戲"}[kind]
    result, turn = interpret(s, text)
    assert result.intent and (result.proposal or result.improvisation)
    assert roles(ep) == ["action", "director"] and turn.calls == 4 and len(requests) == 2
    assert set(requests[-1]["questions"]) == {"intent_fidelity", "consistency", "bounded_effects"}
    assert reviews(turn, "director")[0]["status"] == "approved"
    assert not reviews(turn, "action")
    if result.director_decision:
        assert result.director_decision.evaluation == "approved"


@pytest.mark.parametrize("kind", ["scene", "improvisation"])
@pytest.mark.parametrize("verdict", FAILURES)
def test_director_fallback_runs_both_reviews_once_after_generation(tmp_path, kind, verdict):
    scene = kind == "scene"
    ep = Endpoint(action=[{"action": "move", "destination": "霧中小巷"} if scene else
                          {"action": "creative", "utterance": "演戲"}],
                  director=[SCENE if scene else proposal()],
                  evaluator=[EVALUATED if scene else {"approved": True}], main=[{"approved": True}])
    s, requests = with_native(tmp_path, ep, verdict=verdict, selection=(
        {"category": "move", "destination": "unknown"} if scene else
        {"category": "creative", "improvisation": "yes"}))
    result, turn = interpret(s, "去霧中小巷" if scene else "用帽子演戲")
    assert result.intent and (result.proposal or result.improvisation)
    assert roles(ep) == ["action", "director", "evaluator", "main"]
    assert turn.calls == 6 and len(requests) == 2
    assert reviews(turn, "director")[0]["fallback_reason"]


def test_director_confident_rejection_stops_even_with_an_uncertain_answer(tmp_path):
    def reject(body, raw):
        raw["answers"] = answer(body["questions"], intent_fidelity="approved",
                                consistency="rejected", bounded_effects="uncertain")["answers"]
    ep = Endpoint(action=[{"action": "move", "destination": "霧中小巷"}], director=[SCENE])
    s, requests = with_native(tmp_path, ep, verdict=reject,
                              selection={"category": "move", "destination": "unknown"})
    result, turn = interpret(s, "去霧中小巷")
    assert result.intent is None and result.director_decision.evaluation == "rejected"
    assert roles(ep) == ["action", "director"] and len(requests) == 2
    assert reviews(turn, "director")[0]["status"] == "rejected"


def progress_review(s, *, limit=6):
    with turn_context(limit, s._revision) as turn:
        result = run(s._main_agent().review_progress(
            clone_game(s.game), s._revision, ["help"], s.plot, "有甚麼建議？"))
    return result, turn


def test_progress_filters_only_proposed_options_in_original_order(tmp_path):
    def choose(body, raw):
        raw["answers"] = answer(body["questions"], option_2="approved", option_0="rejected",
                                option_1="approved")["answers"]
    ep = Endpoint(progress=[{**PROGRESS, "options": [2, 0, 1]}])
    s, requests = with_native(tmp_path, ep, verdict=choose)
    result, turn = progress_review(s)
    from core.operators import public_options
    assert result["options"] == [public_options(s.game)[i] for i in (2, 1)]
    assert roles(ep) == ["progress"] and len(requests) == 1 and turn.calls == 2
    assert set(requests[0]["questions"]) == {"option_2", "option_0", "option_1"}
    assert reviews(turn, "progress")[0]["purpose"] == "guidance_review"


@pytest.mark.parametrize("verdict", (*FAILURES, "rejected"))
def test_progress_uncertainty_falls_back_for_the_complete_report(tmp_path, verdict):
    ep = Endpoint(progress=[{**PROGRESS, "options": [2, 0, 1]}], main=[{"options": [0]}])
    s, requests = with_native(tmp_path, ep, verdict=verdict)
    before = deepcopy(encode_game(s.game))
    result, turn = progress_review(s)
    assert result is not None and encode_game(s.game) == before
    if verdict == "rejected":
        assert not result["options"] and roles(ep) == ["progress"]
    else:
        assert len(result["options"]) == 1 and roles(ep) == ["progress", "main"]
        assert '"options": [2, 0, 1]' in ep.calls[-1][1]
    assert len(requests) == 1


@pytest.mark.parametrize("operator", ["action", "director", "progress"])
@pytest.mark.parametrize("mode", ["missing_credentials", "backend_llm", "disabled"])
def test_operator_checks_skip_native_without_an_available_backend(tmp_path, operator, mode):
    ep = Endpoint(action=[{"action": "attack", "target": "老船長"}], main=[EXECUTE_ACTION],
                  evaluator=[EVALUATED], progress=[PROGRESS],
                  router=[router_answer(**ACTION_ROUTE)])
    s, requests = with_native(tmp_path, ep, native=mode != "missing_credentials",
                              backend="llm" if mode == "backend_llm" else "auto", enabled=mode != "disabled")
    with turn_context(6, s._revision) as turn:
        check = run(DecisionGateway(s.gateway).review(
            s._revision, operator, "test_review", {"player_input": ATTACK},
            intent_questions(), fallback_calls=2 if operator == "director" else 1))
    assert check.outcome == "unavailable" and check.fallback_reason == mode
    assert not requests and not ep.calls and turn.calls == 0


@pytest.mark.parametrize("operator,fallback_calls", [("action", 1), ("director", 2), ("progress", 1)])
def test_review_budget_reserves_fallback_and_narration(tmp_path, operator, fallback_calls):
    s, requests = with_native(tmp_path, Endpoint())
    with turn_context(6, s._revision) as turn:
        turn.calls = 6 - fallback_calls - 1
        check = run(DecisionGateway(s.gateway).review(s._revision, operator, "test_review",
                    {}, intent_questions(), fallback_calls=fallback_calls))
    assert check.fallback_reason == "fallback_budget" and not requests


@pytest.mark.parametrize("during_request", [False, True])
def test_stale_revision_cannot_be_natively_approved(tmp_path, during_request):
    def advance(body, raw):
        current_turn().revision += 1
    s, requests = with_native(tmp_path, Endpoint(), verdict=advance)
    with turn_context(6, s._revision) as turn:
        check = run(DecisionGateway(s.gateway).review(s._revision - (not during_request),
            "action", "intent_review", {}, intent_questions(), fallback_calls=1))
    assert check.outcome == "unavailable" and check.fallback_reason == "stale_revision"
    assert len(requests) == int(during_request)


def test_director_review_selects_private_facts_and_redacts_credentials_and_account_ids(tmp_path):
    ep = Endpoint(action=[{"action": "move", "destination": "霧中小巷"}], director=[SCENE])
    s, requests = with_native(tmp_path, ep, selection={"category": "move", "destination": "unknown"})
    s.game.world.here.npcs[0]["knows"].append("船長私藏一張航海圖")
    s.plot_state = {"canon": "native-private", "nested": {"account_id": "account-secret", "fact": "私密設定"}}
    result, _ = interpret(s, "去霧中小巷")
    assert result.proposal
    payload = json.dumps(requests[-1], ensure_ascii=False)
    assert "船長私藏一張航海圖" in payload and "私密設定" in payload
    assert all(secret not in payload for secret in ("native-private", "account-secret", "owner_id", "u1"))
    assert "船長私藏一張航海圖" not in json.dumps(requests[0], ensure_ascii=False)


def test_oversized_essential_context_falls_back_without_truncating_request(tmp_path):
    ep = Endpoint(action=[{"action": "attack", "target": "老船長"}], main=[EXECUTE_ACTION])
    s, requests = with_native(tmp_path, ep)
    text = ATTACK + "x" * MAX_REVIEW_CHARS
    result, turn = interpret(s, text)
    assert result.intent and roles(ep) == ["action", "main"] and len(requests) == 1
    assert text in ep.calls[-1][1] and reviews(turn, "action")[0]["fallback_reason"] == "context_limit"


def test_native_approval_does_not_bypass_deterministic_proposal_validation(tmp_path):
    ep = Endpoint(action=[{"action": "creative", "utterance": "免費治療"}],
                  director=[proposal(success=[{"kind": "heal", "target": "依思"}])])
    s, requests = with_native(tmp_path, ep, selection={"category": "creative", "improvisation": "yes"})
    before = deepcopy(encode_game(s.game))
    result, turn = interpret(s, "我把帽子當法器，演一場治療儀式")
    assert result.intent is None and encode_game(s.game) == before
    assert len(requests) == 1 and not reviews(turn, "director")


def test_native_scene_approval_still_uses_engine_stale_revision_gate(tmp_path):
    ep = Endpoint(action=[{"action": "move", "destination": "霧中小巷"}], director=[SCENE])
    s, _ = with_native(tmp_path, ep, selection={"category": "move", "destination": "unknown"})
    result, _ = interpret(s, "去霧中小巷")
    s._supervised = result
    s._revision += 1
    before = deepcopy(encode_game(s.game))
    s._apply_supervised_proposal(result.intent)
    assert not result.director_decision.accepted and "stale_revision" in result.director_decision.reasons
    assert encode_game(s.game) == before


@pytest.mark.parametrize("operator,verdict", [
    ("action", "approved"), ("action", "malformed"),
    ("director", "approved"), ("director", "uncertain"),
    ("progress", "approved"), ("progress", "uncertain"),
])
def test_native_operator_review_trace_replays_and_exposes_measurements(tmp_path, operator, verdict):
    scene = operator == "director"
    ep = Endpoint(action=[{"action": "move", "destination": "霧中小巷"} if scene else
                          {"action": "attack", "target": "老船長"}],
                  director=[SCENE], evaluator=[EVALUATED], progress=[PROGRESS], main=[
                      {"approved": True} if scene else {"options": [0]} if operator == "progress" else EXECUTE_ACTION])
    s, _ = with_native(tmp_path, ep, verdict=verdict, selection=(
        {"category": "move", "destination": "unknown"} if scene else ACTION_ROUTE))
    if operator == "progress":
        s._operator_state["accepted_turns"] = 4
        s._save()
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.handle("依思 rest" if operator == "progress" else "去霧中小巷" if scene else ATTACK,
                 user_id="u1", structured=operator == "progress"))
    traces = read_traces(tmp_path / "debug")
    rows = next(iter(traces.values()))
    replayed = run(replay(rows))
    assert replayed["output_matches"] and replayed["snapshot_matches"] and replayed["all_calls_consumed"]
    assert not replayed["request_mismatches"]
    measured = evaluate(traces)["results"][0]["reviews"]
    assert measured[0]["operator"] == operator
    assert measured[0]["purpose"] == {"action": "intent_review", "director": "proposal_review",
                                      "progress": "guidance_review"}[operator]
    assert measured[0]["call_count"] == {"action": 3, "director": 4, "progress": 2}[operator]
    assert measured[0]["latency_ms"] >= 0


@pytest.mark.parametrize("native,backend", [(False, "auto"), (True, "llm")])
def test_non_native_standalone_actions_keep_the_existing_routed_path(tmp_path, native, backend):
    ep = Endpoint(router=[router_answer(**ACTION_ROUTE)],
                  action=[{"action": "attack", "target": "老船長"}])
    s, requests = with_native(tmp_path, ep, native=native, backend=backend)
    result, turn = interpret(s)
    assert result.intent and roles(ep) == ["router", "action"]
    assert not requests and not reviews(turn, "action")


@pytest.mark.parametrize("operator", ["director", "progress"])
def test_missing_native_credentials_go_straight_to_existing_reviewers(tmp_path, operator):
    selection = {"category": "move", "destination": "unknown"}
    ep = Endpoint(router=[router_answer(**selection)],
                  action=[{"action": "move", "destination": "霧中小巷"}], director=[SCENE],
                  evaluator=[EVALUATED], progress=[PROGRESS],
                  main=[{"approved": True} if operator == "director" else {"options": [0]}])
    s, requests = with_native(tmp_path, ep, native=False)
    if operator == "director":
        result, turn = interpret(s, "去霧中小巷")
        assert result.proposal and roles(ep) == ["router", "action", "director", "evaluator", "main"]
    else:
        result, turn = progress_review(s)
        assert result["options"] and roles(ep) == ["progress", "main"]
    assert not requests and reviews(turn, operator)[0]["fallback_reason"] == "missing_credentials"


@pytest.mark.parametrize("operator", ["action", "director", "progress"])
def test_budget_pressure_uses_existing_reviewer_without_native_attempt(tmp_path, operator):
    scene = operator == "director"
    ep = Endpoint(action=[{"action": "move", "destination": "霧中小巷"} if scene else
                          {"action": "attack", "target": "老船長"}], director=[SCENE],
                  evaluator=[EVALUATED], progress=[PROGRESS], main=[
                      {"approved": True} if scene else {"options": [0]} if operator == "progress" else EXECUTE_ACTION])
    s, requests = with_native(tmp_path, ep, selection=(
        {"category": "move", "destination": "unknown"} if scene else ACTION_ROUTE))
    s.gateway.config = s.agent_config = replace(s.agent_config, routing=replace(
        s.agent_config.routing, creative_max_calls=6))
    if operator == "progress":
        result, turn = progress_review(s, limit=3)
        assert result["options"] and not requests and roles(ep) == ["progress", "main"]
    else:
        result, turn = interpret(s, "去霧中小巷" if scene else ATTACK, limit=4)
        assert result.intent and len(requests) == 1
        assert roles(ep) == (["action", "director", "evaluator", "main"] if scene else ["action", "main"])
    assert reviews(turn, operator)[0]["fallback_reason"] == "fallback_budget"


@pytest.mark.parametrize("verdict", ["approved", "rejected", "uncertain", "malformed", "timeout"])
def test_linked_plan_reviews_all_candidate_data_in_a_single_batch(tmp_path, verdict):
    from test_action_plans import chain, template, TEXT
    ep = Endpoint(action=[chain()], director=[template()], evaluator=[{"approved": True}], main=[{"approved": True}])
    s, requests = with_native(tmp_path, ep, verdict=verdict, selection={
        "category": "attack", "compound": "yes", "linked": "yes", "improvisation": "yes"})
    s.game.party["BANANA"] = s.game.party.pop("依思")
    s.game.inventory.pop("依思")
    s.game.inventory["BANANA"] = [["毒藥", 1]]
    s.game.world.here.ground_items = [("治療藥水", 2)]
    s.game.world.here.npcs.append({"name": "哥布林", "disposition": "hostile", "knows": []})
    before = deepcopy(encode_game(s.game))
    result, turn = interpret(s, TEXT)
    assert encode_game(s.game) == before and len(requests) == 2
    state = requests[-1]["state"]
    assert state["player_input"] == TEXT and state["candidate"]["preparation"]["template"] == "improvised_throw"
    assert state["candidate"]["steps"] == chain()["steps"]
    assert bool(result.linked_plan) == (verdict != "rejected")
    assert roles(ep) == (["action", "director"] if verdict in ("approved", "rejected") else
                         ["action", "director", "evaluator", "main"])
    assert turn.calls == (4 if verdict in ("approved", "rejected") else 6)


@pytest.mark.parametrize("raw", [{**PROGRESS, "evidence": [99999]}, {**PROGRESS, "options": [99999]}])
def test_invalid_progress_evidence_or_options_never_reach_native_review(tmp_path, raw):
    ep = Endpoint(progress=[raw])
    s, requests = with_native(tmp_path, ep)
    result, _ = progress_review(s)
    assert result is None and not requests and roles(ep) == ["progress"]


def test_one_uncertain_progress_option_reviews_the_complete_report(tmp_path):
    def mixed(body, raw):
        raw["answers"] = answer(body["questions"], option_2="approved", option_0="rejected",
                                option_1="uncertain")["answers"]
    ep = Endpoint(progress=[{**PROGRESS, "options": [2, 0, 1]}], main=[{"options": [0, 1]}])
    s, requests = with_native(tmp_path, ep, verdict=mixed)
    result, _ = progress_review(s)
    from core.operators import public_options
    assert result["options"] == [public_options(s.game)[i] for i in (0, 1)]
    assert roles(ep) == ["progress", "main"] and len(requests) == 1


def test_native_progress_response_cannot_add_an_unproposed_option(tmp_path):
    def extra(body, raw):
        raw["answers"]["option_1"] = deepcopy(raw["answers"]["option_0"])
    ep = Endpoint(progress=[PROGRESS], main=[{"options": [1]}])
    s, requests = with_native(tmp_path, ep, verdict=extra)
    result, _ = progress_review(s)
    assert result is None and len(requests) == 1 and roles(ep) == ["progress", "main"]


def test_native_review_respects_exact_configured_thresholds(tmp_path):
    def boundary(body, raw):
        raw["answers"]["intent_fidelity"].update(confidence=.8,
            probabilities={"approved": .85, "rejected": .15, "uncertain": 0})
    ep = Endpoint(action=[{"action": "attack", "target": "老船長"}])
    s, _ = with_native(tmp_path, ep, verdict=boundary)
    result, turn = interpret(s)
    assert result.intent and reviews(turn, "action")[0]["status"] == "approved"
    assert roles(ep) == ["action"]


def test_native_scene_review_commits_before_delivery_and_still_checks_the_engine(tmp_path):
    ep = Endpoint(action=[{"action": "move", "destination": "霧中小巷"}], director=[SCENE], progress=[PROGRESS])
    s, requests = with_native(tmp_path, ep, selection={"category": "move", "destination": "unknown"})
    delivered = []
    async def on_resolved(lines):
        delivered.append(s.repository.load("t")["snapshot"]["world"]["current"])
    run(s.handle("去霧中小巷", user_id="u1", on_resolved=on_resolved))
    assert s.game.world.current == "dir1" and delivered == ["dir1"]
    assert "evaluator" not in roles(ep) and "main" not in roles(ep) and not ep.unexpected
    assert [r for r in requests if "consistency" in r["questions"]][0]["state"]["candidate"]["content"] == SCENE
    from test_campaign import decisions
    assert decisions(s)[-1]["accepted"] and decisions(s)[-1]["evaluation"] == "approved"
    from test_operators import audits
    assert max(r["call_count"] for r in audits(s)) <= 8


def test_native_approved_improvisation_pays_costs_and_keeps_roll_results_engine_owned(tmp_path):
    ep = Endpoint(action=[{"action": "creative", "utterance": "獻上帽子演戲"}],
                  director=[proposal(costs=["破帽子"])])
    s, requests = with_native(tmp_path, ep, selection={"category": "creative", "improvisation": "yes"})
    s.game.give_item("依思", "破帽子", 1)
    s._save()
    before = s.game.world.here.npcs[0]["disposition"]
    run(s.handle("我把破帽子獻給船長，演一場恐怖短劇", user_id="u1"))
    assert len(requests) == 2 and s.game._pending_check["effect"]["kind"] == "improvisation"
    assert not any(n == "破帽子" for n, _ in s.game.inventory["依思"])
    assert s.game.world.here.npcs[0]["disposition"] == before
    assert roles(ep) == ["action", "director", "narrator"]
    run(s.settle_roll(20, user_id="u1"))
    assert s.game.world.here.npcs[0]["disposition"] != before and len(requests) == 2


def test_native_approval_cannot_authorize_another_players_character(tmp_path):
    ep = Endpoint(action=[{"action": "attack", "actor": "大力蕉", "target": "老船長"}])
    s, requests = with_native(tmp_path, ep)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(ATTACK, user_id="u1"))
    assert "🚫" in "".join(lines) and encode_game(s.game) == before
    assert len(requests) == 1 and roles(ep) == ["action"]


def test_fallback_main_call_limit_prevents_native_attempt(tmp_path):
    s, requests = with_native(tmp_path, Endpoint())
    with turn_context(8, s._revision) as turn:
        turn.main_calls = 3
        checked = run(DecisionGateway(s.gateway).review(s._revision, "action", "intent_review",
                      {}, intent_questions(), fallback_calls=1))
    assert checked.fallback_reason == "fallback_budget" and not requests and turn.calls == 0


@pytest.mark.parametrize("native", [True, False])
def test_progress_without_guidance_options_preserves_existing_main_review(tmp_path, native):
    ep = Endpoint(progress=[{**PROGRESS, "options": []}], main=[{"options": []}])
    s, requests = with_native(tmp_path, ep, native=native)
    result, _ = progress_review(s)
    assert result["options"] == [] and roles(ep) == ["progress", "main"] and not requests
