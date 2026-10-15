"""Grounded conversation references and single rendering of player speech."""

import json
import logging

import pytest

from core.guards import is_dialogue_echo
from core.memory import recent_conversation
from core.templates import render_hint
from test_campaign import run, service, restart
from test_operators import Endpoint, EXECUTE_ACTION, roles, supervised

REMARK = "死老嘢 講啲唔講啲"


def previous_exchange(s):
    npc = s.game.world.here.npcs[0]
    npc.update(knows=["已公開的消息"], disclosed=["已公開的消息"])
    s.game.ledger.add("依思", "talk", "依思 向 老船長：還有消息嗎？",
                      npc="老船長", utterance="還有消息嗎？", information="exhausted")
    s._save()


def test_untargeted_speech_is_shown_once_without_narrator_or_retry_job(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION],
                        action=[{"action": "talk", "utterance": REMARK}])
    s = supervised(tmp_path, endpoint)
    # Discord account and owned character may intentionally have different names.
    lines, prose = run(s.handle(REMARK, author="BANANA🍌", user_id="u1"))
    assert lines == [f"💬 依思：「{REMARK}」"] and prose == ""
    assert roles(endpoint) == ["main", "action", "main"]
    assert not s.has_unfinished() and not restart(tmp_path).has_unfinished()
    assert render_hint(s.game.ledger.entries[-1], s.game) == ""
    assert s.game.ledger.entries[-1].actor == "依思"


def test_followup_gets_same_actor_context_and_captain_response(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action", "memory"]}, EXECUTE_ACTION],
                        action=[{"action": "talk", "target": "老船長", "utterance": REMARK}])
    s = supervised(tmp_path, endpoint)
    previous_exchange(s)
    lines, prose = run(s.handle(REMARK, user_id="u1"))
    assert "向 老船長" in lines[0] and "沒有新情報" in "\n".join(lines)
    assert prose and roles(endpoint) == ["main", "action", "main", "narrator"]
    for role, prompt in endpoint.calls[:3]:
        assert "還有消息嗎？" in prompt
        assert "exhausted" in prompt and "老船長" in prompt
    assert not endpoint.unexpected


def test_recent_conversation_is_public_actor_scoped_and_survives_restart(tmp_path):
    s = service(tmp_path)
    previous_exchange(s)
    s.game.world.here.npcs[0]["knows"].append("不可透露的秘密")
    s._save()
    context = recent_conversation(restart(tmp_path).game, "依思")
    assert context["npc"] == "老船長" and context["information"] == "exhausted"
    assert "不可透露的秘密" not in json.dumps(context, ensure_ascii=False)
    assert recent_conversation(s.game, "大力蕉") is None
    assert recent_conversation(s.game, "unknown") is None


@pytest.mark.parametrize("change", ["age", "scene", "move_back", "npc_removed"])
def test_conversation_reference_expires_without_guessing_a_new_target(tmp_path, change):
    s = service(tmp_path)
    previous_exchange(s)
    if change == "age":
        s.game.ledger.turn += 4
    elif change == "scene":
        s.game.world.current = "tavern"
    elif change == "move_back":
        s.game.ledger.add("大力蕉", "scene", "全隊回到碼頭", scene="dock")
    else:
        s.game.world.here.npcs = [{"name": "新 NPC"}]
    assert recent_conversation(s.game, "依思") is None


def test_model_echo_is_replaced_by_grounded_fallback_and_logged(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="dnd-bot")
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION],
                        action=[{"action": "talk", "target": "老船長", "utterance": REMARK}],
                        narrator=[f"**[PC1]**：「{REMARK}」"])
    s = supervised(tmp_path, endpoint)
    previous_exchange(s)
    lines, prose = run(s.handle(REMARK, user_id="u1"))
    assert "老船長" in prose and "沒有新情報" in prose
    assert REMARK not in prose and "reason=dialogue_echo" in caplog.text
    assert s.has_unfinished()  # a rejected model reply can be retried with /continue
    assert s._unfinished["text"] == ""
    assert len([r for r in roles(endpoint) if r == "narrator"]) == 1


def test_useful_npc_response_is_kept_even_when_it_quotes_the_player(tmp_path):
    prose = f'老船長皺眉：「{REMARK}？我已把知道的消息全告訴你了。」'
    endpoint = Endpoint(main=[{"consult": ["action"]}, EXECUTE_ACTION],
                        action=[{"action": "talk", "target": "老船長", "utterance": REMARK}],
                        narrator=[prose])
    s = supervised(tmp_path, endpoint)
    previous_exchange(s)
    _, output = run(s.handle(REMARK, user_id="u1"))
    assert output == prose and not s.has_unfinished()


def test_main_clarification_cannot_be_bypassed_by_prior_conversation(tmp_path):
    endpoint = Endpoint(main=[{"consult": ["action"]},
        {"decision": "clarify", "intent": "action", "director": False}],
        action=[{"action": "talk", "target": "老船長", "utterance": REMARK}])
    s = supervised(tmp_path, endpoint)
    previous_exchange(s)
    turn, count = s.game.ledger.turn, len(s.game.ledger.entries)
    lines, prose = run(s.handle(REMARK, user_id="u1"))
    assert "尚未執行" in lines[0] and prose == ""
    assert s.game.ledger.turn == turn and len(s.game.ledger.entries) == count
    assert "narrator" not in roles(endpoint)


def test_explicit_player_chat_is_not_redirected_to_recent_npc(tmp_path):
    s = service(tmp_path)
    previous_exchange(s)
    turn = s.game.ledger.turn
    lines, prose = run(s.handle("// 大家幾點休息？", user_id="u1"))
    assert lines == [] and prose == "" and s.game.ledger.turn == turn
    assert s.game.ledger.entries[-1].kind == "table"


@pytest.mark.parametrize("formatted", [f"「{REMARK}」", f"**依思**：『{REMARK}！』"])
def test_echo_comparison_ignores_dialogue_formatting(formatted):
    assert is_dialogue_echo(formatted, [REMARK, f"依思：{REMARK}"])
    assert not is_dialogue_echo(formatted + "老船長轉身離開。", [REMARK, f"依思：{REMARK}"])
