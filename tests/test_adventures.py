"""Definition validation, pinned lifecycle transactions, and private model boundaries."""

import asyncio
import json
import logging
import sqlite3
from copy import deepcopy

import httpx
import pytest

from core.adventures import build_adventure, validate_definition
from core.agent_config import parse_config
from core.campaign import is_starter, sync_starter
from core.cli import seed_starter_knowledge
from core.debug_capture import DebugCapture, capture_context
from core.intent import Intent
from core.memory import build_memory
from core.model_gateway import ModelGateway
from core.persistence import CampaignRepository
from core.plot import tick
from core.script_store import ScriptError, ScriptStore
from core.scripts import ScriptService
from core.service import V4Service
from core.state import encode_game
from test_admins_scripts import definition, SECRET, NOTE, SOURCE, Gateway, LINKED
from test_campaign import run, Narrator
from test_game_commands import archives


def select(store, d=None, *, guild=42, channel=123, user=1):
    d = deepcopy(d or definition())
    draft_id = store.create(guild, channel, user, SOURCE, 2)
    store.update(draft_id, completed=2, status="ready", definition=d)
    version = store.approve(guild, channel, draft_id, user)
    return draft_id, version


@pytest.mark.parametrize("path,value", [
    (("schema_version",), 99), (("complete",), False), (("starting_scene",), "missing"),
    (("scenes", 0, "id"), "../bad"), (("scenes", 0, "exits"), {"missing": "路"}),
    (("scenes", 0, "ground_items"), [["金幣", -1]]), (("scenes", 0, "search_dc"), True),
    (("scenes", 0, "npcs", 0, "knows"), "bad"), (("public_setting", "premise"), SECRET),
    (("scenes", 0, "description"), SECRET), (("scenes", 0, "npcs", 0, "desc"), SECRET),
    (("scenes", 0, "npcs", 0, "knows"), [NOTE]),
    (("secrets", 0, "reveal"), {"npc": "missing"}),
    (("encounters",), {"arrival": [{"name": SECRET, "hp": 8, "ac": 12, "attack_bonus": 2, "dmg": "1d6", "loot": []}]}),
    (("conversion_issues",), ["unconverted trap"]), (("encounters",), {"arrival": [{"name": "敵人", "hp": 0, "ac": 12, "attack_bonus": 2, "dmg": "1d6", "loot": []}]}),
    (("plot", "beats"), {"bad": {"text": "trap", "effects": [{"teleport": "unknown"}]}}),
    (("plot", "clocks"), [{"id": "clock", "start": 0, "max": 3, "on_expire": "missing"}]),
    (("plot", "triggers"), [{"beat": "missing", "when": {"scene": "missing"}}]),
])
def test_malformed_or_private_public_definitions_cannot_be_approved(tmp_path, path, value):
    d = definition()
    obj = d
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    assert validate_definition(d)
    store = ScriptStore(CampaignRepository(str(tmp_path / "campaigns.db")))
    draft_id = store.create(42, 123, 1, SOURCE, 2)
    store.update(draft_id, status="ready", definition=d)
    with pytest.raises(ScriptError):
        store.approve(42, 123, draft_id, 1)
    assert store.selected(42, 123) is None


@pytest.mark.parametrize("value", [None, [], "text", 3, True])
def test_wrong_schema_types_fail_closed(value):
    assert validate_definition(value)
    for field in ("public_setting", "scenes", "encounters", "plot"):
        d = definition()
        d[field] = value
        assert validate_definition(d)


def test_partial_validation_allows_later_chunk_content_and_references():
    from core.adventures import empty_definition

    assert not validate_definition(empty_definition(), partial=True)
    d = definition()
    d.update(title="", starting_scene="later_scene", complete=False, conversion_issues=["pending content"])
    d["scenes"][0]["exits"] = {"later_scene": "下一段的地點"}
    d["plot"]["beats"] = {"later_beat": {"text": "稍後發生。", "effects": []}}
    d["secrets"][0]["reveal"] = {"npc": "later_npc"}
    assert not validate_definition(d, partial=True)
    assert validate_definition(d)


@pytest.mark.parametrize("npcs", [None, ["keeper"], {"keeper": {}}, [None], [{"id": []}]])
def test_partial_validation_rejects_malformed_nested_npcs(npcs):
    d = definition()
    d["scenes"][0]["npcs"] = npcs
    issues = validate_definition(d, partial=True)
    assert issues and any("scenes[0].npcs" in issue for issue in issues)
    assert all(marker not in str(issues) for marker in (SECRET, NOTE, SOURCE))


@pytest.mark.parametrize("private_text", ['quoted "secret"', "two\nsecret lines", r"back\slash"])
def test_public_private_separation_compares_decoded_text(private_text):
    d = definition()
    d["secrets"][0]["text"] = private_text
    d["public_setting"]["premise"] = "公開：" + private_text
    assert any("private secret" in issue for issue in validate_definition(d))
    d = definition()
    d["private_author_notes"] = [private_text]
    d["scenes"][0]["npcs"][0]["knows"] = [private_text]
    assert any("private note" in issue for issue in validate_definition(d))


def test_approved_versions_are_immutable_drafts_are_owned_and_replacement_invalidates_ids(tmp_path):
    repo = CampaignRepository(str(tmp_path / "campaigns.db"))
    store = ScriptStore(repo)
    draft, version = select(store)
    assert store.approve(42, 123, draft, 1) == version
    store.clear(42, 123)
    assert store.selected(42, 123) is None
    assert store.approve(42, 123, draft, 1) == version
    assert store.selected(42, 123)["version_id"] == version
    with pytest.raises(sqlite3.IntegrityError):
        with repo.connect() as conn:
            conn.execute("UPDATE adventure_versions SET definition='{}' WHERE version_id=?", (version,))
    with pytest.raises(sqlite3.IntegrityError):
        with repo.connect() as conn:
            conn.execute("DELETE FROM adventure_versions WHERE version_id=?", (version,))
    replacement = store.create(42, 123, 1, "replacement", 2)
    for old in (draft, "unrelated"):
        with pytest.raises(ScriptError):
            store.approve(42, 123, old, 1)
        with pytest.raises(ScriptError):
            store.discard(42, 123, old, 1)
    with pytest.raises(ScriptError):
        store.discard(42, 123, replacement, 2)
    # A new draft doesn't change the selected, immutable approved version.
    assert ScriptStore(CampaignRepository(repo.path)).selected(42, 123)["version_id"] == version
    store.discard(42, 123, replacement, 1)
    assert store.draft(42, 123) is None
    assert store.selected(42, 123)["version_id"] == version


def test_approval_and_selection_rollback_together(tmp_path):
    repo = CampaignRepository(str(tmp_path / "campaigns.db"))
    store = ScriptStore(repo)
    draft = store.create(42, 123, 1, SOURCE, 2)
    store.update(draft, status="ready", definition=definition())
    with repo.connect() as conn:
        conn.execute("CREATE TRIGGER fail_selection BEFORE INSERT ON script_selections "
                     "BEGIN SELECT RAISE(ABORT, 'storage failure'); END")
    from core.persistence import StorageError
    with pytest.raises(StorageError):
        store.approve(42, 123, draft, 1)
    assert store.draft(42, 123)["status"] == "ready"
    assert store.selected(42, 123) is None
    with repo.connect() as conn:
        assert conn.execute("SELECT count(*) FROM adventure_versions").fetchone()[0] == 0


def test_new_game_activation_pin_restart_channel_isolation_and_clear(tmp_path):
    s = V4Service(str(tmp_path), channel_id="123", guild_id="42")
    other = V4Service(str(tmp_path), channel_id="456", guild_id="42")
    other_before = other.repository.load("456")
    draft, version = select(s.script_store)
    result = run(s.new_game("1", admin=True))
    assert "石灘" in result and SOURCE not in result and NOTE not in result and SECRET not in result
    assert s.game.campaign_version == version and not s.game.party and not s.game.inventory
    assert set(s.game.world.scenes) == {"arrival"} and SECRET in s.game.world.here.npcs[0]["knows"]
    assert not is_starter(s.game) and sync_starter(s.game) == [] and not seed_starter_knowledge(s.game)
    assert not s.plot["clocks"] and s.rules["tone"] == "海洋奇幻"
    pinned = s.repository.load("123")["snapshot"]["active_campaign"]
    changed = definition()
    changed["scenes"][0]["name"] = "新版本石灘"
    _, next_version = select(s.script_store, changed)
    restored = V4Service(str(tmp_path), channel_id="123", guild_id="42")
    assert restored.active_campaign == pinned and restored.game.campaign_version == version
    assert restored.game.world.here.name == "石灘"
    assert "已有遊戲" in run(restored.new_game("1", admin=True))
    run(restored.create_character("英雄", "Fighter", "1", "玩家"))
    result = run(restored.reset_game("1", "reset confirm", admin=True))
    assert "新版本石灘" in result
    assert not restored.game.party and restored.game.campaign_version == next_version
    archive = archives(restored)[0]
    assert json.loads(archive["channels"][0]["snapshot"])["active_campaign"] == pinned
    assert json.loads(archive["channels"][0]["snapshot"])["party"]
    assert other.repository.load("456") == other_before
    restored.script_store.clear(42, 123)
    assert restored.game.campaign_version == next_version  # selection doesn't affect live state
    run(restored.reset_game("1", "reset confirm", admin=True))
    assert restored.active_campaign is None and restored.game.world.current == "dock"
    assert is_starter(restored.game)
    assert not restored.game.party and len(archives(restored)) == 2


def test_failed_reset_keeps_old_world_rules_plot_and_definition(tmp_path):
    s = V4Service(str(tmp_path), channel_id="123", guild_id="42")
    select(s.script_store)
    run(s.new_game("1", admin=True))
    before, old_game = s.repository.load("123"), encode_game(s.game)
    old_rules, old_plot, old_definition = deepcopy(s.rules), deepcopy(s.plot), deepcopy(s.active_campaign)
    s.script_store.clear(42, 123)
    with s.repository.connect() as conn:
        conn.execute("CREATE TRIGGER fail_reset BEFORE UPDATE OF snapshot ON channels "
                     "BEGIN SELECT RAISE(ABORT, 'storage failure'); END")
    assert "未能儲存" in run(s.reset_game("1", "reset confirm", admin=True))
    assert s.repository.load("123") == before and encode_game(s.game) == old_game
    assert (s.rules, s.plot, s.active_campaign) == (old_rules, old_plot, old_definition)
    assert not archives(s)


def test_custom_world_with_starter_identifiers_never_receives_starter_repairs(tmp_path):
    s = V4Service(str(tmp_path), channel_id="123", guild_id="42")
    d = definition()
    d["starting_scene"] = d["scenes"][0]["id"] = "dock"
    d["scenes"][0]["name"] = "霧錨鎮碼頭"
    d["scenes"][0]["npcs"][0].update(name="老船長", desc="獨眼，正在補網")
    d["secrets"][0]["reveal"] = {"npc": "老船長"}
    d["scenes"].append({"id": "tavern", "name": "破舊酒館", "description": "作者的酒館。",
                         "exits": {}, "npcs": [], "ground_items": [], "hidden_items": [], "search_dc": 12})
    select(s.script_store, d)
    run(s.new_game("1", admin=True))
    s.game._starter_progress = {"tavern": True}
    s._save()
    before = encode_game(s.game)
    assert sync_starter(s.game) == [] and not seed_starter_knowledge(s.game)
    restored = V4Service(str(tmp_path), channel_id="123", guild_id="42")
    assert encode_game(restored.game) == before
    assert restored.game.world.scenes["tavern"].description == "作者的酒館。"


def test_supported_plot_effects_and_engine_authorized_secret_disclosure():
    d = definition()
    d["secrets"] = [{"id": "truth", "text": SECRET, "reveal": {"beat": "storm"}}]
    d["plot"].update(
        clocks=[{"id": "tide", "start": 0, "max": 2, "on_expire": "storm"}],
        beats={"storm": {"text": "風暴來臨。", "effects": [
            {"clock": {"id": "tide", "set": 2}}, {"inspire": {"char": "party"}}]}},
        objectives=[{"id": "investigate", "text": "調查燈塔。", "public": True}])
    assert not validate_definition(d)
    g, rules, plot = build_adventure({"version_id": "v1", "definition": d})
    assert SECRET not in build_memory(g).render() and SECRET not in json.dumps(rules)
    from core.plot import new_state
    state = new_state(plot)
    g.ledger.turn = 1
    assert not tick(g, plot, state)[0]
    g.ledger.turn = 2
    lines, changed = tick(g, plot, state)
    assert changed and SECRET in "\n".join(lines)
    assert SECRET in build_memory(g).render()
    assert not tick(g, plot, state)[0]


def test_queued_admin_operation_rechecks_authorization_inside_channel_lock(tmp_path):
    async def scenario():
        s = V4Service(str(tmp_path), channel_id="123")
        current = [True]
        await s._lock.acquire()
        task = asyncio.create_task(s.reset_game("1", "reset confirm", admin=True,
                                               permission_check=lambda: current[0]))
        await asyncio.sleep(0)
        current[0] = False
        s._lock.release()
        assert "🚫" in await task and not archives(s)
    run(scenario())


def test_demoted_admin_cannot_confirm_queued_override_and_other_admin_cannot_take_ownership(tmp_path):
    async def scenario():
        s = V4Service(str(tmp_path), channel_id="123")
        await s.create_character("英雄", "Fighter", "2", "另一玩家")
        s.pending = Intent("move", actor="英雄", destination="tavern")
        s.pending_user_id, s.pending_admin = "1", True
        s._save()
        current = [True]
        await s._lock.acquire()
        task = asyncio.create_task(s.confirm("1", admin=True, admin_check=lambda: current[0]))
        await asyncio.sleep(0)
        current[0] = False
        s._lock.release()
        assert "🚫" in (await task)[0][0] and s.pending is not None
        lines, _ = await s.confirm("2", admin=True)
        assert "🚫" in lines[0] and s.pending_user_id == "1"
        s.narrator = Narrator()
        await s.confirm("1", admin=True)
        assert s.pending is None
        assert s.game.world.current == "tavern"
    run(scenario())


def test_promotions_and_audit_survive_campaign_reset(tmp_path):
    from core.admins import AdminPolicy
    s = V4Service(str(tmp_path), channel_id="123", guild_id="42")
    admins = AdminPolicy(s.repository, "99")
    admins.change(42, 1, 99, promote=True)
    select(s.script_store)
    run(s.new_game("1", admin=True))
    run(s.reset_game("1", "reset confirm", admin=True))
    restored = AdminPolicy(CampaignRepository(s.repository.path), "99")
    assert restored.authorized(42, 1)
    with s.repository.connect() as conn:
        assert conn.execute("SELECT count(*) FROM admin_audit").fetchone()[0] == 1


def test_custom_routing_requests_receive_public_context_without_source_notes_or_secrets(tmp_path):
    from test_operators import Endpoint
    from test_system_one import routed, router_answer
    endpoint = Endpoint(router=[router_answer()], action=[{"action": "search"}])
    s, native_requests = routed(tmp_path, endpoint)
    s.guild_id = "42"
    select(s.script_store, channel=s.channel_id)
    run(s.reset_game("1", "reset confirm", admin=True))
    run(s.create_character("英雄", "Fighter", "1", "玩家"))
    lines, prose = run(s.handle("搜索", user_id="1"))
    assert lines and not native_requests
    routing = [prompt for role, prompt in endpoint.calls if role == "router"]
    assert routing
    assert all(marker not in prompt for prompt in routing for marker in (SOURCE, NOTE, SECRET))


@pytest.mark.parametrize("repair", [False, True])
def test_private_preparation_and_custom_narration_never_capture_or_log_source_notes_or_secrets(tmp_path, caplog, repair):
    requests = []
    cfg = parse_config({"roles": {r: {"model": r} for r in ("director", "narrator")},
                        "supervision": {"enabled": False}}, {})
    responses = [definition(), LINKED]
    if repair:
        malformed = definition()
        malformed["scenes"][0]["npcs"] = [SECRET]
        responses.insert(0, malformed)

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        text = json.dumps(responses.pop(0), ensure_ascii=False) if body["model"] == "director" else "海浪拍打腳邊的石頭。"
        return httpx.Response(200, json={"message": {"content": text}, "done_reason": "stop"})

    gateway = ModelGateway(cfg, httpx.MockTransport(transport))
    store = ScriptStore(CampaignRepository(str(tmp_path / "campaigns.db")))
    sink = DebugCapture(tmp_path, cfg, enabled=True)
    caplog.set_level(logging.DEBUG, logger="dnd-bot")

    async def scenario():
        service = ScriptService(store, gateway)
        with capture_context(sink, "outer-must-not-capture-private-upload"):
            draft = service.submit(42, 123, 1, "private.md", SOURCE.encode())
            await asyncio.gather(*service.tasks.values())
        store.approve(42, 123, draft, 1)
        s = V4Service(str(tmp_path), channel_id="123", guild_id="42", gateway=gateway)
        s.debug_capture = sink
        assert SECRET not in await s.new_game("1", admin=True, request_id="new")
        await s.create_character("英雄", "Fighter", "1", "玩家", request_id="pc")
        lines, _ = await s.handle("英雄 rest", user_id="1", structured=True, request_id="rest")
        assert SECRET not in "\n".join(lines)
        narration_requests = [r for r in requests if r["model"] == "narrator"]
        assert narration_requests
        for request in narration_requests:
            assert all(marker not in json.dumps(request) for marker in (SOURCE, NOTE, SECRET))
        assert SECRET not in s.scene() and SOURCE not in str(s._operator_state) and NOTE not in str(s._operator_state)
        # The engine, rather than the narrator, authorizes a fact disclosure.
        from core.rules_core import _reveal_fact
        _reveal_fact(s.game, "英雄", s.game.world.here.npcs[0])
        s._save()
        await s.handle("英雄 rest", user_id="1", structured=True)
        assert any(SECRET in json.dumps(r) for r in requests if r["model"] == "narrator")
        assert all(SOURCE not in json.dumps(r) and NOTE not in json.dumps(r)
                   for r in requests if r["model"] != "director")
        with s.repository.connect() as conn:
            receipts = conn.execute("SELECT payload FROM operator_state WHERE channel_id='123'").fetchone()[0]
            assert SOURCE not in receipts and NOTE not in receipts
    run(scenario())
    assert all(marker not in caplog.text for marker in (SOURCE, NOTE, SECRET))
    debug = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "debug").glob("*.jsonl"))
    assert all(marker not in debug for marker in (SOURCE, NOTE, SECRET))


def test_partial_but_valid_model_json_is_not_approved(tmp_path):
    config = parse_config({}, {})
    response = {"message": {"content": json.dumps(definition())}, "done_reason": "length"}
    gateway = ModelGateway(config, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))

    async def scenario():
        store = ScriptStore(CampaignRepository(str(tmp_path / "campaigns.db")))
        service = ScriptService(store, gateway)
        draft = service.submit(42, 123, 1, "a.txt", SOURCE.encode())
        await asyncio.gather(*service.tasks.values())
        assert store.draft(42, 123)["status"] == "failed"
        with pytest.raises(ScriptError):
            store.approve(42, 123, draft, 1)
    run(scenario())
