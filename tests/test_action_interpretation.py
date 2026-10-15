"""Offline regressions for free-form actions and honest interpretation errors."""
from copy import deepcopy

import httpx
import pytest

from core.intent import parse_command
from core.state import encode_game
from test_campaign import run
from test_operators import Endpoint, EXECUTE_PARSED, audits, roles, supervised


@pytest.mark.parametrize("text", ["洗劫船長", "我想洗劫船長", "打劫老船長", "依思 搶劫船長"])
def test_bare_robbery_asks_for_method_without_models_or_game_changes(tmp_path, text):
    endpoint = Endpoint()
    s = supervised(tmp_path, endpoint)
    before, plot = encode_game(s.game), deepcopy(s.plot_state)
    lines, prose = run(s.handle(text, user_id="u1"))
    reply = "\n".join(lines)
    assert "偷偷扒竊" in reply and "威嚇" in reply and "老船長" in reply
    assert "未能理解" not in reply and not prose and not endpoint.calls
    assert encode_game(s.game) == before and s.plot_state == plot
    assert not s.pending and not getattr(s.game, "_pending_check", None)


def test_robbery_cannot_be_misparsed_as_attack():
    assert parse_command("依思 打劫船長", ["依思"]) is None


@pytest.mark.parametrize("text,skill", [
    ("我想偷偷扒竊老船長", "sleight of hand"),
    ("依思 扒竊老船長", "sleight of hand"),
    ("我想威嚇老船長", "intimidation"),
    ("我要說服老船長", "persuasion"),
])
def test_explicit_method_survives_main_outage_without_free_loot(tmp_path, text, skill):
    endpoint = Endpoint(main=[httpx.Response(503)])
    s = supervised(tmp_path, endpoint)
    # Ensure the check cannot auto-succeed through a passive score.
    s.game.party["依思"]["stats"].update(DEX=1, CHA=1)
    before = deepcopy(s.game.inventory)
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "未能理解" not in "\n".join(lines)
    pending = s.game._pending_check
    assert pending["actor"] == "依思" and pending["skill"] == skill
    assert s.game.inventory == before
    assert roles(endpoint) == ["main", "narrator"]


def test_explicit_method_still_goes_through_main_review(tmp_path):
    endpoint = Endpoint(main=[{"consult": []}, EXECUTE_PARSED])
    s = supervised(tmp_path, endpoint)
    s.game.party["依思"]["stats"]["DEX"] = 1
    lines, _ = run(s.handle("偷偷扒竊老船長", user_id="u1"))
    assert "檢定" in "\n".join(lines)
    assert s.game._pending_check["skill"] == "sleight of hand"
    assert roles(endpoint) == ["main", "main", "narrator"]


@pytest.mark.parametrize("response,expected", [
    (httpx.Response(401, text="DO-NOT-PRINT"), "認證"),
    (httpx.Response(404, text="DO-NOT-PRINT"), "模型或 API 路徑"),
    (httpx.Response(503, text="DO-NOT-PRINT"), "模型服務"),
    ("not-json", "格式未通過驗證"),
    ({"wrong": "shape"}, "格式未通過驗證"),
])
def test_model_failure_is_not_blame_on_player_and_does_not_mutate(tmp_path, response, expected):
    endpoint = Endpoint(main=[response])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, prose = run(s.handle("把那位的東西據為己有", user_id="u1"))
    reply = "\n".join(lines)
    assert expected in reply and "主代理" in reply and "行動未執行" in reply
    assert "DO-NOT-PRINT" not in reply and "未能理解" not in reply
    assert not prose and encode_game(s.game) == before
    assert any(r["status"] == "fallback" for r in audits(s))
    assert "DO-NOT-PRINT" not in repr(audits(s))


def test_action_endpoint_failure_names_action_role(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]},
        {"decision": "clarify", "intent": "action", "director": False}],
        action=[httpx.Response(503)])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, _ = run(s.handle("嘗試弄走他身上的東西", user_id="u1"))
    assert "行動解析器" in lines[0] and "模型服務" in lines[0]
    assert encode_game(s.game) == before


@pytest.mark.parametrize("exception,expected", [
    (httpx.ReadTimeout("DO-NOT-PRINT"), "逾時"),
    (httpx.ConnectError("DO-NOT-PRINT"), "模型服務"),
])
def test_transport_failures_have_safe_actionable_messages(tmp_path, exception, expected):
    def fail(_):
        raise exception
    endpoint = Endpoint(main=[fail])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, _ = run(s.handle("把那位的東西據為己有", user_id="u1"))
    assert expected in lines[0] and "DO-NOT-PRINT" not in lines[0]
    assert "DO-NOT-PRINT" not in repr(audits(s))
    assert encode_game(s.game) == before


def test_main_clarification_is_distinct_from_provider_failure(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]},
        {"decision": "clarify", "intent": "action", "director": False}],
        action=[{"action": "skill", "skill": "sleight of hand", "target": "老船長"}])
    s = supervised(tmp_path, endpoint)
    before = encode_game(s.game)
    lines, _ = run(s.handle("嘗試弄走他身上的東西", user_id="u1"))
    assert "使用的方法" in lines[0] and "模型服務" not in lines[0]
    assert encode_game(s.game) == before


def test_robbery_clarification_preserves_ownership_checks(tmp_path):
    endpoint = Endpoint()
    s = supervised(tmp_path, endpoint)
    lines, _ = run(s.handle("大力蕉 洗劫船長", user_id="u1"))
    assert "屬於其他玩家" in lines[0] and not endpoint.calls
