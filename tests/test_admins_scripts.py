"""Guild authorization and private submission, without a live Discord connection."""

import asyncio
import inspect
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.discord_bot import DiscordBot
from core.admins import AdminPolicy, initial_admin_id
from core.adventures import MAX_BYTES, MAX_CHARS, chunks, decode_upload, empty_definition, validate_definition
from core.agent_config import ConfigError, legacy_config
from core.persistence import CampaignRepository
from core.script_store import ScriptError, ScriptStore
from core.scripts import ScriptService, private_preview
from test_campaign import run
from test_campaign_integration import interaction

SECRET = "HIDDEN_TRUTH_729"
NOTE = "AUTHOR_ONLY_338"
SOURCE = "SOURCE_ONLY_921"
LINKED = {"complete": True, "scenes": {}, "npcs": {}, "conversion_issues": []}


def definition():
    d = empty_definition()
    d.update(title="燈塔迷霧", starting_scene="arrival",
             public_setting={"tone": "海洋奇幻", "premise": "調查失去光芒的燈塔。", "facts": ["此島四面環海。"]},
             private_author_notes=[NOTE],
             secrets=[{"id": "truth", "text": SECRET, "reveal": {"npc": "燈塔守衛"}}])
    d["scenes"] = [{"id": "arrival", "name": "石灘", "description": "海浪拍打卵石。",
                    "exits": {}, "npcs": [{"id": "keeper", "name": "燈塔守衛", "desc": "披著舊雨衣。",
                                           "disposition": "friendly", "knows": [], "loot": []}],
                    "ground_items": [["繩索", 1]], "hidden_items": [["銅鑰匙", 1]], "search_dc": 10}]
    return d


class Gateway:
    def __init__(self, responses=None):
        self.config = legacy_config("http://unused")
        self.responses = list(responses or [definition(), LINKED])
        self.calls = []

    def log_configuration(self):
        pass

    async def object(self, role, prompt):
        self.calls.append((role, prompt))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def admin_interaction(user_id=1, guild_id=42):
    it = interaction()
    it.guild.id = it.guild_id = guild_id
    it.user.id = user_id
    done = [False]

    async def acknowledge(*args, **kwargs):
        done[0] = True

    it.response.is_done = lambda: done[0]
    it.response.send_message.side_effect = acknowledge
    it.response.defer.side_effect = acknowledge
    return it


def messages(it):
    return it.response.send_message.call_args_list + it.followup.send.call_args_list


@pytest.mark.parametrize("value", ["<@123>", "1,2", "-1", "abc", "0", str(2**64), "１２３", "9" * 100])
def test_invalid_initial_admin_fails_startup_without_echoing_value(value, monkeypatch):
    monkeypatch.setenv("ADMIN_ID", value)
    with pytest.raises(ConfigError, match="ADMIN_ID") as error:
        DiscordBot(gateway=Gateway())
    assert value not in str(error.value)


def test_protected_initial_multiple_admins_server_isolation_and_restart(tmp_path):
    path = str(tmp_path / "campaigns.db")
    policy = AdminPolicy(CampaignRepository(path), "99")
    assert initial_admin_id(" 99 ") == "99" and initial_admin_id("") == ""
    for guild_id in ("g1", "g2"):
        assert policy.authorized(guild_id, "99")
    assert not policy.authorized("", "99") and not policy.authorized("g1", "99", member=False)
    for user_id in ("1", "2"):
        assert policy.change("g1", user_id, "99", promote=True)
        assert not policy.change("g1", user_id, "99", promote=True)
    again = AdminPolicy(CampaignRepository(path), "99")
    assert again.authorized("g1", "1") and again.authorized("g1", "2")
    assert not again.authorized("g2", "1")
    assert not again.change("g1", "99", "1", promote=False)
    assert again.authorized("g1", "99")
    assert again.change("g1", "1", "2", promote=False)
    assert not again.authorized("g1", "1")
    assert again.authorized("g1", "1", manage_server=True)
    assert not again.authorized("g1", "1", manage_server=True, bot=True)
    assert not again.change("g1", "1", "2", promote=False)
    with again.repository.connect() as conn:
        assert conn.execute("SELECT count(*) FROM admin_audit").fetchone()[0] == 6
        assert conn.execute("SELECT promoted_by,promoted_at FROM bot_admins").fetchone()[0] == "99"


def test_management_commands_require_current_admin_and_same_guild_human(monkeypatch):
    monkeypatch.setenv("ADMIN_ID", "99")
    bot = DiscordBot(gateway=Gateway())
    it = admin_interaction(99)
    member = SimpleNamespace(id=1, guild=it.guild, bot=False,
                             guild_permissions=SimpleNamespace(manage_guild=False))
    run(bot.tree.get_command("promote").callback(it, member))
    assert bot.admins.authorized(42, 1)
    assert not bot._v4_games
    # Promoted users can manage peers without Discord management permissions.
    peer = admin_interaction(1)
    member.id = 2
    run(bot.tree.get_command("promote").callback(peer, member))
    assert bot.admins.authorized(42, 2)
    run(bot.tree.get_command("admins").callback(peer))
    assert "<@99>" in "\n".join(c.args[0] for c in messages(peer))
    member.id = 99
    run(bot.tree.get_command("demote").callback(peer, member))
    assert bot.admins.authorized(42, 99)
    member.id, member.bot = 7, True
    run(bot.tree.get_command("promote").callback(peer, member))
    member.bot, member.guild = False, SimpleNamespace(id=777)
    run(bot.tree.get_command("promote").callback(peer, member))
    assert not bot.admins.authorized(42, 7)
    member.id, member.guild = 2, it.guild
    member.guild_permissions.manage_guild = True
    run(bot.tree.get_command("demote").callback(peer, member))
    assert not bot.admins.authorized(42, 2)
    assert "Discord" in messages(peer)[-1].args[0]
    assert all(c.kwargs["ephemeral"] for c in messages(it) + messages(peer))
    bot.admins.change(42, 1, 99, promote=False)
    run(bot.tree.get_command("promote").callback(peer, member))
    assert not bot.admins.authorized(42, 2)


@pytest.mark.parametrize("dm", [False, True])
def test_unauthorized_and_dm_commands_and_callbacks_do_not_load_games(dm):
    bot = DiscordBot(gateway=Gateway())
    it = admin_interaction()
    if dm:
        it.guild = it.guild_id = None
        it.user.guild_permissions.manage_guild = True
    for command in bot.tree.get_commands():
        if command.name.endswith("-admin") or command.name in ("new_game", "reset_game", "promote", "demote", "admins"):
            kwargs = {}
            for name, p in inspect.signature(command.callback).parameters.items():
                if name != "interaction" and p.default is inspect.Parameter.empty:
                    kwargs[name] = SimpleNamespace(id=7) if name == "member" else "test"
            run(command.callback(it, **kwargs))
            assert command.default_permissions is None and command.guild_only
    for command in bot.tree.get_command("script").commands:
        args = ("old-id",) if command.name in ("approve", "discard") else \
            (SimpleNamespace(size=0),) if command.name == "submit" else ()
        run(command.callback(it, *args))
    run(bot._v4_admin_run(it, "hero", "search"))
    run(bot._v4_settle(it, "d20", admin=True))
    assert not bot._v4_games
    assert not bot.scripts.store.draft(42, 123)
    assert all(c.kwargs["ephemeral"] for c in messages(it))


def test_promoted_admin_can_invoke_every_admin_command_and_autocomplete():
    bot = DiscordBot(gateway=Gateway())
    bot.admins.change(42, 1, 99, promote=True)
    fake = SimpleNamespace(
        game=SimpleNamespace(party={}, combat=SimpleNamespace(active=False)),
        handle=AsyncMock(return_value=([], "")), new_game=AsyncMock(return_value="started"),
        reset_game=AsyncMock(return_value="reset"), admin_give=AsyncMock(return_value="given"),
        admin_inspire=AsyncMock(return_value="inspired"), admin_xp=AsyncMock(return_value="xp"))
    bot._v4_games["123"] = fake
    for command in bot.tree.get_commands():
        if not (command.name.endswith("-admin") or command.name in ("new_game", "reset_game")):
            continue
        it = admin_interaction()
        kwargs = {name: "test" for name, p in inspect.signature(command.callback).parameters.items()
                  if name != "interaction" and p.default is inspect.Parameter.empty}
        if command.name == "xp-admin":
            kwargs["amount"] = 10
        run(command.callback(it, **kwargs))
        assert not it.response.send_message.called or "🚫" not in it.response.send_message.call_args.args[0]
        assert command.default_permissions is None and command.guild_only
    assert fake.handle.await_count == 7
    assert all(getattr(fake, name).await_count == 1 for name in ("new_game", "reset_game", "admin_give", "admin_xp", "admin_inspire"))
    ac = bot.tree.get_command("give-admin")._params["character"].autocomplete
    fake.game.party = {"hero": {"occupation": "Fighter"}}
    assert run(ac(admin_interaction(), ""))[0].value == "hero"
    bot.admins.change(42, 1, 99, promote=False)
    assert run(ac(admin_interaction(), "")) == []
    assert run(ac(admin_interaction(guild_id=777), "")) == []


@pytest.mark.parametrize("filename,data,match", [
    ("a.pdf", b"x", "UTF-8"), ("a.txt", b"\xff", "UTF-8"),
    ("a.md", b"x" * (MAX_BYTES + 1), "1 MiB"),
    ("a.md", b"x" * (MAX_CHARS + 1), "100,000"),
    ("a.txt", b" ", "UTF-8"), ("a.txt", b"x\x00y", "UTF-8")],
    ids=["extension", "encoding", "byte-limit", "character-limit", "empty", "binary"])
def test_upload_rejections_are_explicit(filename, data, match):
    with pytest.raises(ScriptError, match=match):
        decode_upload(filename, data)


def test_upload_limits_utf8_bom_and_chunk_boundaries():
    assert decode_upload("A.MD", b"\xef\xbb\xbfhello") == "hello"
    assert len(decode_upload("a.txt", b"x" * MAX_CHARS)) == MAX_CHARS
    source = ("x" * 1800 + "\n" + "中" * 1800) * 4
    assert "".join(chunks(source)) == source
    assert all(len(part) <= 3000 for part in chunks(source))


def test_private_submit_review_owner_approval_and_demoted_callbacks():
    async def scenario():
        gateway = Gateway()
        bot = DiscordBot(gateway=gateway)
        bot.admins.change(42, 1, 99, promote=True)
        bot.admins.change(42, 2, 99, promote=True)
        it = admin_interaction()

        class Attachment:
            filename, size = "adventure.md", len(SOURCE)
            read = AsyncMock(return_value=SOURCE.encode())

            @property
            def url(self):
                raise AssertionError("attachment URL must never be read or retained")

        await bot.tree.get_command("script").get_command("submit").callback(it, Attachment())
        draft = bot.scripts.store.draft(42, 123)
        assert draft["source_text"] == SOURCE and draft["status"] == "preparing"
        await asyncio.gather(*bot.scripts.tasks.values())
        draft = bot.scripts.store.draft(42, 123)
        assert draft["status"] == "ready" and draft["completed"] == draft["total"] == 2
        status = admin_interaction()
        await bot.tree.get_command("script").get_command("status").callback(status)
        review = "\n".join(c.args[0] for c in messages(status))
        assert SECRET in review and NOTE in review and SOURCE not in review
        assert all(c.kwargs["ephemeral"] for c in messages(it) + messages(status))
        assert not it.channel.send.called and not status.channel.send.called
        assert status.response.send_message.call_args.kwargs["file"].filename == "adventure-review.json"
        other = admin_interaction(2)
        approve = bot.tree.get_command("script").get_command("approve").callback
        await approve(other, draft["draft_id"])
        assert not bot.scripts.store.selected(42, 123)
        bot.admins.change(42, 1, 99, promote=False)
        await approve(admin_interaction(), draft["draft_id"])
        assert not bot.scripts.store.selected(42, 123)
        bot.admins.change(42, 1, 99, promote=True)
        await approve(admin_interaction(), draft["draft_id"])
        selected = bot.scripts.store.selected(42, 123)
        assert selected and not bot.scripts.store.selected(42, 456) and not bot.scripts.store.selected(777, 123)
        assert {role for role, _ in gateway.calls} == {"director"}
        assert not bot._v4_games
        await bot.scripts.close()
    run(scenario())


def test_upload_rechecks_demotion_after_attachment_read():
    async def scenario():
        bot = DiscordBot(gateway=Gateway())
        bot.admins.change(42, 1, 99, promote=True)

        async def read():
            bot.admins.change(42, 1, 99, promote=False)
            return b"adventure"

        file = SimpleNamespace(filename="a.md", size=9, read=read)
        it = admin_interaction()
        await bot._submit_script(it, file)
        assert not bot.scripts.store.draft(42, 123) and not bot.scripts.tasks
        assert all(c.kwargs["ephemeral"] for c in messages(it))
    run(scenario())


def test_cross_chunk_references_restart_resume_and_progress(tmp_path):
    async def scenario():
        first = definition()
        first["scenes"][0]["exits"] = {"高塔": "高塔"}
        second = empty_definition()
        second["scenes"] = [{"id": "tower", "name": "高塔", "description": "樓梯通往高處。",
                             "exits": {"石灘": "石灘"}, "npcs": [], "ground_items": [], "hidden_items": [], "search_dc": 12}]
        store = ScriptStore(CampaignRepository(str(tmp_path / "campaigns.db")))
        source = "a" * 3000 + "b"
        draft_id = store.create(42, 123, 1, source, 3)
        store.update(draft_id, completed=1)
        service = ScriptService(store, Gateway([first, second, LINKED]))
        service.resume()
        await asyncio.gather(*service.tasks.values())
        draft = store.draft(42, 123)
        assert draft["status"] == "ready" and draft["completed"] == draft["total"] == 3
        assert draft["definition"]["scenes"][0]["exits"] == {"tower": "高塔"}
        assert draft["definition"]["scenes"][1]["exits"] == {"arrival": "石灘"}
        assert "a" * 3000 in service.gateway.calls[0][1] and '"text": "b"' in service.gateway.calls[1][1]
        assert "起始場景" in private_preview(draft, None)
    run(scenario())


@pytest.mark.parametrize("response", [RuntimeError(SOURCE), {"wrong": SECRET}])
def test_model_failures_are_persisted_privately_and_never_approvable(tmp_path, caplog, response):
    async def scenario():
        store = ScriptStore(CampaignRepository(str(tmp_path / "campaigns.db")))
        service = ScriptService(store, Gateway([response]))
        draft_id = service.submit(42, 123, 1, "a.txt", SOURCE.encode())
        await asyncio.gather(*service.tasks.values())
        draft = store.draft(42, 123)
        assert draft["status"] == "failed"
        assert SOURCE not in str(draft["issues"]) and SECRET not in str(draft["issues"])
        with pytest.raises(ScriptError):
            store.approve(42, 123, draft_id, 1)
    run(scenario())
    assert SOURCE not in caplog.text and SECRET not in caplog.text
