#!/usr/bin/env python3
"""Party Health Board — standalone Discord bot that live-renders party HP
status from the dm-bot campaign SQLite DB into one channel message.

Update strategy:
  * every EVENT_POLL seconds a cheap MAX(id) heartbeat on the messages table
    detects new DM replies and pushes a board edit immediately (bypasses the
    edit throttle — one extra edit per reply is well within rate limits)
  * a FULL_REFRESH rebuild every 30s acts as a fallback in case a poll was
    missed; the fallback path still honours MIN_EDIT_INTERVAL

Configuration arrives via environment (systemd EnvironmentFile):
    DISCORD_TOKEN   bot token
    CHANNEL_ID      channel that hosts the board message
    DB_PATH         read-only path to campaign.db
    HEALTH_KEY      reserved for future use
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dnd-health")

# the main bot's engine provides the shared (pure) move tables — /moves is
# fully deterministic: no LLM, one read-only DB query
DM_BOT_HOME = os.environ.get("DM_BOT_HOME", "/home/<USER>/dnd-dm-bot")

MARKER = "🩸 **隊伍健康看板 / Party Health Board**"

# One-time play guide (posted into this channel on first boot; marker-scanned
# + state.json flag so restarts never re-post it). Chunks stay under
# Discord's 2000-char message limit. Bump GUIDE_VERSION to republish an
# updated guide (the old pin is unpinned first).
GUIDE_MARKER = "📘 **玩家指南 / Game Guide**"
GUIDE_VERSION = 3
GUIDE_CHUNKS = [
    GUIDE_MARKER + """ v3（本訊息由看板機器人發布；指南更新時會自動重發新版）

**這是什麼遊戲？**
AI 地下城主（DM）主持的《龍與地下城》5e 冒險：你們扮演角色說話、行動、擲骰；DM 扮演全世界。
核心原則：**引擎判定、模型敘事** — 骰值、HP、物品、情報全部由系統計算，DM 只把結果寫成故事。

**🚀 快速開始**
1️⃣ 管理員用 `/new_game` 開始遊戲
2️⃣ 每位玩家用 `/pc <名字> <職業>` 建立角色（屬性由系統公正擲骰）
3️⃣ `/scene` 查看場景，再用 `/explore <描述>` 自由行動——粵語英文都得

**💬 怎麼行動（/explore 自由描述）**
例子：「我匿埋一邊」（潛行）、「用醫藥救隊友」（醫藥）、「向船長打聽線索」（社交）、
「執起地下把劍」（拾取）。引擎自動挑技能＋熟練加值；唔確定就出檢定卡等你骰。""",

    """**🎯 檢定怎麼骰**
引擎覺得結果不確定時會出檢定卡（🎯 … vs DC）。
你執行 `/roll d20` → 引擎套用你的修正值判定 → **劇情自動續寫**（逐字輸出）。

**⚔️ 戰鬥（每人一個專屬指令，自動完成帶細節）**
`/attack [目標] [招式]` — 目標清單帶敵人 HP
`/use [物品] [隊友]` — 用物品；**餵倒地隊友藥水＝救佢**
`/skill [技能] [對象]` — 18 項技能（★＝熟練）
`/defend` 防禦（敵人對你劣勢）· `/flee` 撤退 · `/observe` 找破綻（下擊優勢）
輪到你時引擎會提示；敵人回合自動結算。

**🧰 玩家指令**
`/inventory` 物品＋法術格＋技能＋招式 · `/status` HP＋場景＋📜已知情報
`/give` 給隊友（不填對象＝放地上） · `/continue` 卡住時推進 · `/help` 說明

**🎛 管理員**
`/new_game` 開始新遊戲 · `/reset_game` 封存並重新開始（填 `reset confirm` 確認）
explore/attack/use/skill/defend/flee/observe-roll/give 的 `-admin` 版本""",

    """**📜 情報系統**
每個 NPC 知道若干事實；成功的交談/洞察會問出**新**情報（一次一項），
問過的自動記在 `/status` 的「已知情報」——不用重問。
建議行動會顯示進度（已問出 2/4）。

**🔄 一回合的流程**
你說話（行動）→ 引擎即時判定（🎲）→ 不確定就出檢定卡 → 你 `/roll d20` →
系統判定 → DM 逐字敘述結果 → HP／物品／情報自動更新 → 看板同步。

**其他**
· 直接打字＝桌邊聊天（引擎記錄但不回應）
· 只能控制自己擁有的角色（引擎強制）

祝冒險愉快！有問題隨時 `/help` 🎲""",
]
DIFFICULTY_ZH = {"easy": "新手", "normal": "標準", "hard": "困難"}
# The tracked v3 game chat. The placeholder is REPLACED at startup by
# resolve_game_chat(): GAME_CHANNEL_ID env override, else the newest
# discord session row — no real channel id is ever hardcoded in source.
CHAT_WHERE = "platform = 'discord' AND chat_id = '<CHANNEL_ID>'"
SESSION_SQL = ""
HEARTBEAT_SQL = ""
LAST_REPLY_SQL = ""
EVENTS_SQL = ""


def resolve_game_chat(db_path: str) -> None:
    """Pin the v3 game chat and (re)build the SQL filters from it."""
    global CHAT_WHERE, SESSION_SQL, HEARTBEAT_SQL, LAST_REPLY_SQL, EVENTS_SQL
    cid = os.environ.get("GAME_CHANNEL_ID", "").strip()
    if not cid:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                   timeout=2.0)
            try:
                row = conn.execute(
                    "SELECT chat_id FROM sessions WHERE platform = 'discord' "
                    "ORDER BY rowid DESC LIMIT 1").fetchone()
            finally:
                conn.close()
            cid = row[0] if row else ""
        except sqlite3.Error as exc:
            log.warning("could not read sessions for chat detection: %s", exc)
    if not cid:
        log.error("no GAME_CHANNEL_ID env and no discord session row — "
                  "v3 board section stays disabled")
        return
    CHAT_WHERE = f"platform = 'discord' AND chat_id = '{cid}'"
    SESSION_SQL = ("SELECT party, scene, difficulty, combat, pending_check "
                   "FROM sessions WHERE " + CHAT_WHERE)
    HEARTBEAT_SQL = "SELECT MAX(id), MAX(ts) FROM messages WHERE " + CHAT_WHERE
    LAST_REPLY_SQL = ("SELECT ts FROM messages WHERE " + CHAT_WHERE +
                      " AND role = 'assistant' ORDER BY id DESC LIMIT 1")
    EVENTS_SQL = ("SELECT line FROM events WHERE " + CHAT_WHERE +
                  " ORDER BY id DESC LIMIT 3")
    log.info("tracking game chat %s", cid)


STATE_FILE = Path(__file__).resolve().parent / "state.json"
EVENT_POLL = 2            # seconds; cheap new-reply heartbeat
FULL_REFRESH = 30.0       # seconds; fallback rebuild if a poll was missed
MIN_EDIT_INTERVAL = 10.0  # fallback-path edit throttle (reply pushes bypass)


# ------------------------------------------------------------------ rendering

def hp_bar(now: int, mx: int, segments: int = 10) -> str:
    filled = 0 if mx <= 0 else round(now / mx * segments)
    filled = max(0, min(segments, filled))
    return "▓" * filled + "░" * (segments - filled)


def status_word(now: int, mx: int) -> str:
    if now <= 0:
        return "昏迷"
    if now <= 5 or (mx > 0 and now / mx <= 0.25):
        return "危急"
    if mx > 0 and now / mx <= 0.50:
        return "受傷"
    return "健康"


def character_line(name: str, data: dict) -> str:
    hp = int(data.get("hp_now") or 0)
    mx = int(data.get("hp_max") or 0)
    cls = data.get("occupation") or "?"
    lvl = data.get("level") or 1  # missing/null level (pre-tracking chars) → Lv1
    return (f"• **{name}** {cls} Lv{lvl} `{hp_bar(hp, mx)}` "
            f"**{hp}/{mx}** — {status_word(hp, mx)}")


def fmt_uptime(secs: float) -> str:
    secs = int(secs)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, _ = divmod(secs, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{secs}s"


def rel_zh(delta: float) -> str:
    s = max(0, int(delta))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分鐘"
    if s < 86400:
        return f"{s // 3600} 小時"
    return f"{s // 86400} 天"


def pending_line(p) -> str | None:
    if not p:
        return None
    return (f"🎯 {p.get('char') or '?'} {p.get('ability') or '?'} "
            f"vs DC {p.get('dc') or '?'} — 等待玩家回覆「骰」")


def combat_lines(combat: dict) -> list:
    order = combat.get("order") or []
    idx = combat.get("idx", -1)
    turn = "—"
    if 0 <= idx < len(order):
        turn = order[idx].get("name", "—")
        if order[idx].get("npc"):
            turn += "（敵）"
    lines = ["", f"⚔️ **戰鬥進行中**｜第 {combat.get('round', '?')} 回合｜"
             f"目前行動：**{turn}**"]
    for e in order:
        if not e.get("npc") or e.get("hp") is None:
            continue  # only enemies that carry hp data
        hp = int(e.get("hp") or 0)
        mx = int(e.get("hp_max") or 0)
        lines.append(f"　👹 {e.get('name', '?')} `{hp_bar(hp, mx)}` {hp}/{mx}")
    return lines


def render(party, scene, difficulty, combat, pending, last_ts, events,
           uptime_secs: float) -> str:
    stamp = datetime.now().strftime("%H:%M:%S")
    diff = DIFFICULTY_ZH.get(difficulty, difficulty or "—")
    lines = [
        MARKER,
        f"🟢 自動更新中｜運行 {fmt_uptime(uptime_secs)}｜更新：{stamp}",
        f"🗺️ 場景：{scene or '—'}｜難度：{diff}",
    ]
    if last_ts:
        lines.append(f"⏱️ 最後回合：{rel_zh(time.time() - last_ts)}前")
    lines.append("")
    if party:
        lines += [character_line(n, d) for n, d in party.items()]
    else:
        lines.append("_尚無隊伍資料 / no party data_")
    pl = pending_line(pending)
    if pl:
        lines += ["", pl]
    if combat:
        lines += combat_lines(combat)
    if events:
        lines += ["", "📜 最近事件"]
        lines += [f"　▪️ {e}" for e in events]  # newest first
    lines += ["", "_自動更新，資料來源 campaign.db (updated automatically)_"]
    return "\n".join(lines)


# ---------------------------------------------------------------- data source

def _json(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def read_last_reply_id(db_path: str):
    """Cheap heartbeat: MAX(id) of tracked messages, or None when unreadable."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(HEARTBEAT_SQL).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None  # briefly locked/missing — skip this cycle
    return row[0] if row and row[0] is not None else None


def read_board(db_path: str):
    """Full snapshot for one render, or None when the DB is unreadable."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(SESSION_SQL).fetchone()
            if row is None:
                return None
            reply = conn.execute(LAST_REPLY_SQL).fetchone()
            try:
                events = [r[0] for r in conn.execute(EVENTS_SQL)]
            except sqlite3.OperationalError:
                events = []  # events table absent on older DBs
            ev_head = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE " + CHAT_WHERE
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return (_json(row[0]), row[1], row[2], _json(row[3]), _json(row[4]),
            reply[0] if reply else None, events, ev_head)


def read_new_events(db_path: str, last_id: int) -> tuple[int, list]:
    """(new_cursor, new event lines) — v3 engine facts since last push."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT id, line FROM events WHERE " + CHAT_WHERE +
                " AND id > ? ORDER BY id LIMIT 15", (last_id,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return last_id, []
    return (rows[-1][0] if rows else last_id), [r[1] for r in rows]


def _campaign_id(path: str) -> str:
    stem = Path(path).stem
    return stem[len("v4_state_"):] if stem.startswith("v4_state_") else ""


def _campaign_conn(data_dir: str):
    uri = (Path(data_dir) / "campaigns.db").resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=1.0)


def read_v4_state(path: str, cursor: int | None = None) -> dict:
    """SQLite takes precedence over preserved JSON. Read only a page of events."""
    db = Path(path).parent / "campaigns.db"
    if db.exists():
        with closing(_campaign_conn(str(db.parent))) as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT snapshot,last_event_id FROM channels WHERE channel_id=?",
                               (_campaign_id(path),)).fetchone()
            if row:
                blob = json.loads(row[0])
                blob["last_event_id"] = row[1]
                if cursor is not None:
                    cursor = cursor if cursor <= row[1] else 0
                    blob["entries"] = [json.loads(r[0]) for r in conn.execute(
                        "SELECT payload FROM events WHERE channel_id=? AND event_id>? "
                        "ORDER BY event_id LIMIT 15", (_campaign_id(path), cursor))]
                return blob
    with open(path, encoding="utf-8") as f:
        blob = json.load(f)
    entries = blob.get("entries", [])
    blob["last_event_id"] = len(entries)
    if cursor is not None:
        cursor = cursor if cursor <= len(entries) else 0
        blob["entries"] = [dict(e, event_id=i + 1)
                           for i, e in enumerate(entries) if cursor <= i < cursor + 15]
    return blob


def read_new_v4_entries(path: str, cursor: int) -> tuple[int, list]:
    """(new_cursor, new lines) — v4 ledger entries since last push."""
    try:
        blob = read_v4_state(path, cursor)
        new = blob.get("entries", [])
    except (OSError, ValueError, sqlite3.Error):
        return cursor, []
    return (new[-1]["event_id"] if new else blob["last_event_id"]), [
        f"[t{e.get('turn', '?')}][{e.get('kind', '?')}] "
        f"{e.get('actor', '?')}: {e.get('text', '')}" for e in new
        if e.get("kind") not in ("table",)]  # table talk = not game log


def v4_summary(path: str, label: str = "") -> str:
    """One compact debug block per game for the board message."""
    try:
        blob = read_v4_state(path)
    except (OSError, ValueError, sqlite3.Error):
        return ""
    world = blob.get("world", {})
    cur = world.get("scenes", {}).get(world.get("current", ""), {})
    combat = blob.get("combat") or {}
    lines = [f"🧪 v4 {label or '遊戲'}：{cur.get('name', '?')} · turn "
             f"{blob.get('turn', 0)} · "
             + (f"戰鬥 R{combat.get('round', '?')}" if combat.get("order")
                else "探索")]
    for n, e in blob.get("party", {}).items():
        lines.append(f"　{n} {e.get('hp_now')}/{e.get('hp_max')}HP "
                     f"slots {e.get('slots') or {}}")
    for n, f in (blob.get("enemies") or {}).items():
        if not f.get("dead"):
            lines.append(f"　👹 {n} {f.get('hp')}/{f.get('hp_max')} "
                         f"AC{f.get('ac')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- state file

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_state(**fields) -> None:
    data = load_state()
    data.update(fields)
    try:
        STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        log.warning("could not persist state.json")


# ------------------------------------------------------------------ moves

def moves_text(db_path: str, char_filter: str = "") -> str:
    """Every character's attack moves, computed live from campaign.db with
    the engine's own pure tables (no LLM). Locked spell moves are shown
    with the slot level that unlocks them."""
    if DM_BOT_HOME not in sys.path:
        sys.path.insert(0, DM_BOT_HOME)
    from engine.moves import compute_attack_moves, move_spell_level
    from engine.charlib import slots_for
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT party FROM sessions WHERE " + CHAT_WHERE).fetchone()
        inv_rows = conn.execute("SELECT char_name, kind, name, qty FROM items "
                                "WHERE " + CHAT_WHERE).fetchall()
    finally:
        conn.close()
    if not row or not row[0]:
        return "（尚無隊伍資料 / no party data）"
    party = json.loads(row[0])
    inv: dict[str, list] = {}
    for cn, kind, name, qty in inv_rows:
        inv.setdefault(cn, []).append((kind, name, qty))
    q = (char_filter or "").strip()
    lines = ["⚔️ **角色招式一覽 / Character Moves**（即時計算，無 LLM）", ""]
    for name, v in party.items():
        if not isinstance(v, dict) or (q and q not in name):
            continue
        occ = v.get("occupation", "?")
        lvl = int(v.get("level", 1) or 1)
        mx = {int(k): n for k, n in slots_for(occ, lvl).items()}
        max_slot = max(mx, default=0)
        cur = v.get("slots") or {}
        slot_txt = ""
        if mx:
            slot_txt = "｜法術格 " + " · ".join(
                f"L{k} {cur.get(str(k), n)}/{n}" for k, n in sorted(mx.items()))
        lines.append(f"**{name}**（{occ} Lv{lvl}）{slot_txt}")
        for m in compute_attack_moves(occ, lvl, inv.get(name, []), 99):
            req = move_spell_level.get(m["name"], 0)
            aoe = "（範圍）" if m["aoe"] else ""
            if req > max_slot:
                lines.append(f"　🔒 {m['name']} {m['ability']} {m['dmg']}{aoe}"
                             f" — 需 {req} 環法術格（尚未解鎖）")
                continue
            cost = f"（耗 {req} 環法術格）" if req else ""
            src = "（裝備）" if m.get("improvised") else ""
            lines.append(f"　· {m['name']} {m['ability']} {m['dmg']}{aoe}{cost}{src}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- bot

class HealthBoardBot(discord.Client):
    def __init__(self, channel_id: int, db_path: str):
        super().__init__(intents=discord.Intents.default())  # default intents only
        self.channel_id = channel_id
        self.db_path = db_path
        self.channel = None
        self.board = None
        self.last_content = None
        self.last_edit = 0.0
        self.started = time.monotonic()
        self._last_max_id = None
        self._next_full = 0.0
        self._board_located = False
        self._guide_done = False
        self._perm_warned = False
        self._task = None
        self.tree = app_commands.CommandTree(self)
        self._register_commands()
        # Campaigns share campaigns.db; preserved JSON remains an import source.
        self.data_dir = os.path.dirname(os.path.abspath(db_path))
        self.log_state = load_state()

    def _v4_state_files(self) -> list[str]:
        """Logical per-channel paths (SQLite channels first), plus unimported JSON."""
        try:
            files = [os.path.join(self.data_dir, f)
                     for f in os.listdir(self.data_dir)
                     if f.startswith("v4_state") and f.endswith(".json")]
            files.sort(key=os.path.getmtime, reverse=True)
            if (Path(self.data_dir) / "campaigns.db").exists():
                with closing(_campaign_conn(self.data_dir)) as conn:
                    channels = [r[0] for r in conn.execute(
                        "SELECT channel_id FROM channels ORDER BY revision DESC,channel_id")]
                active = [os.path.join(self.data_dir, f"v4_state_{c}.json" if c else "v4_state.json")
                          for c in channels]
                files = active + [p for p in files if p not in active]
            return files
        except (OSError, sqlite3.Error):
            return []

    def _v4_label(self, path: str) -> str:
        """v4_state_<channel_id>.json -> '#channel' (resolved) or a mention."""
        cid = _campaign_id(path)
        if cid.isdigit():
            ch = self.get_channel(int(cid))
            return f"#{ch.name}" if getattr(ch, "name", None) \
                else f"<#{cid}>"
        return "playground"

    def _register_commands(self):
        @self.tree.command(name="moves",
                           description="角色攻擊招式一覽（即時計算，無 LLM）/ attack moves reference")
        @app_commands.describe(character="Optional: only one character / 可只看一名角色")
        async def moves_cmd(interaction: discord.Interaction,
                            character: str = ""):
            try:
                text = moves_text(self.db_path, character)
            except Exception as e:
                log.warning("moves failed: %s", e)
                await interaction.response.send_message(f"⚠️ {e}")
                return
            chunks, t = [], text
            while len(t) > 1900:
                cut = t.rfind("\n", 0, 1900)
                cut = cut if cut > 950 else 1900
                chunks.append(t[:cut])
                t = t[cut:].lstrip("\n")
            chunks.append(t)
            await interaction.response.send_message(chunks[0])
            for c in chunks[1:]:
                await interaction.followup.send(c)

    async def setup_hook(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def on_ready(self):
        log.info("connected as %s (id %s)", self.user, self.user.id)
        if not self._board_located:
            try:
                await self._locate_board()
            except discord.HTTPException as exc:
                log.warning("board lookup failed: %s", exc)
        await self._ensure_guide()
        for g in self.guilds:  # guild-scoped sync (needs 'applications.commands')
            try:
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                log.info("slash commands synced to guild %s", g.id)
            except Exception as exc:
                log.warning("command sync failed for %s: %s (is "
                            "'applications.commands' in the invite?)", g.id, exc)

    async def _ensure_guide(self):
        """Post the play guide once per channel; a GUIDE_VERSION bump
        republishes (unpins the old guide first) so updates reach the
        channel, not just fresh installs."""
        if self._guide_done:
            return
        self._guide_done = True
        st = load_state()
        if st.get("guide_posted") \
                and st.get("guide_version") == GUIDE_VERSION:
            return  # current version already posted
        try:
            if self.channel is None:
                self.channel = self.get_channel(self.channel_id) \
                    or await self.fetch_channel(self.channel_id)
            # retire any older pinned guide first
            try:
                for m in await self.channel.pins():
                    if m.author.id == self.user.id \
                            and m.content.startswith(GUIDE_MARKER):
                        await m.unpin()
            except discord.HTTPException:
                pass
            first = None
            for chunk in GUIDE_CHUNKS:
                sent = await self.channel.send(chunk)
                first = first or sent
            try:
                await first.pin()  # nice-to-have; needs manage-messages
            except discord.HTTPException:
                pass
            save_state(guide_posted=True, guide_version=GUIDE_VERSION)
            log.info("game guide v%s posted (%d chunks, first pinned)",
                     GUIDE_VERSION, len(GUIDE_CHUNKS))
        except discord.HTTPException as exc:
            log.warning("guide post failed: %s", exc)

    async def on_resumed(self):
        self.last_content = None  # force full re-render after reconnect

    async def _locate_board(self):
        """Find a reusable board message via state.json, else channel history."""
        self.channel = self.get_channel(self.channel_id) \
            or await self.fetch_channel(self.channel_id)
        self._board_located = True
        msg_id = load_state().get("message_id")
        if msg_id:
            try:
                self.board = await self.channel.fetch_message(msg_id)
                log.info("reusing board message %s (state.json)", msg_id)
                return
            except discord.NotFound:
                pass
        async for msg in self.channel.history(limit=50):  # newest first
            if msg.author.id == self.user.id and msg.content.startswith(MARKER):
                self.board = msg
                save_state(message_id=msg.id)
                log.info("reusing board message %s (channel history)", msg.id)
                return
        log.info("no existing board message; will post a new one")

    async def _poll_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._tick()
            except Exception:
                log.exception("poll tick failed")  # never crash the loop
            await asyncio.sleep(EVENT_POLL)

    async def _tick(self):
        if not self._board_located:
            return  # never post a fresh board before on_ready locates/reuses
        max_id = read_last_reply_id(self.db_path)
        reply_push = False
        if max_id is not None:
            if self._last_max_id is not None and max_id > self._last_max_id:
                reply_push = True  # a new DM reply landed
            self._last_max_id = max_id
        if not reply_push and time.monotonic() < self._next_full:
            await self._push_logs()  # logs flow even between board refreshes
            return
        data = read_board(self.db_path)
        self._next_full = time.monotonic() + FULL_REFRESH
        content = render(*data[:7], time.monotonic() - self.started) if data else MARKER
        blocks = [v4_summary(p, self._v4_label(p))
                  for p in self._v4_state_files()[:3]]  # newest games
        blocks = [b for b in blocks if b]
        if blocks:
            content += "\n" + "\n".join(blocks)
        await self._publish(content, bypass_throttle=reply_push)
        await self._push_logs()

    async def _push_logs(self):
        """Debug portal: stream new engine facts (v3 events + every v4
        channel ledger) into the channel as batched log messages."""
        if self.channel is None:
            return
        # first run: park the cursors at the current head (no backlog flood)
        if "last_event_id" not in self.log_state:
            try:
                conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                       timeout=1.0)
                try:
                    head = conn.execute("SELECT COALESCE(MAX(id),0) FROM events "
                                        "WHERE " + CHAT_WHERE).fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.Error:
                head = 0
            self.log_state["last_event_id"] = head
        # v4 cursors are per state file; the legacy single-file cursor
        # migrates to its per-file key once
        v4_updates = {}
        lines = []
        for path in self._v4_state_files():
            key = f"last_v4_len::{os.path.basename(path)}"
            if key not in self.log_state:
                if os.path.basename(path) == "v4_state.json" \
                        and "last_v4_len" in self.log_state:
                    self.log_state[key] = self.log_state["last_v4_len"]
                else:
                    try:
                        self.log_state[key] = read_v4_state(path)["last_event_id"]
                    except (OSError, ValueError, sqlite3.Error):
                        self.log_state[key] = 0
            cur_v4, v4_lines = read_new_v4_entries(
                path, self.log_state[key])
            if v4_lines:
                tag = self._v4_label(path)
                lines += [f"[v4 {tag}]{l}" for l in v4_lines]
            v4_updates[key] = cur_v4
        cur_ev, ev_lines = read_new_events(
            self.db_path, self.log_state["last_event_id"])
        lines = [f"[v3] {l}" for l in ev_lines] + lines
        if not lines:
            return
        try:
            await self.channel.send(
                "🧪 **debug log**\n" + "\n".join(lines)[:1900])
        except discord.HTTPException as exc:
            log.warning("log push failed: %s", exc)
            return
        self.log_state["last_event_id"] = cur_ev
        self.log_state.update(v4_updates)
        save_state(**self.log_state)

    async def _publish(self, content: str, bypass_throttle: bool) -> None:
        if content == self.last_content:
            return
        if not bypass_throttle and time.monotonic() - self.last_edit < MIN_EDIT_INTERVAL:
            return
        if self.board is None:
            if self.channel is None:
                try:
                    self.channel = await self.fetch_channel(self.channel_id)
                except discord.HTTPException as exc:
                    self._warn_perm(exc)
                    return
            self.board = await self.channel.send(content)
            save_state(message_id=self.board.id)
            self._perm_warned = False
            log.info("posted board message %s", self.board.id)
        else:
            try:
                await self.board.edit(content=content)
            except discord.Forbidden as exc:
                self._warn_perm(exc)
                return
            except discord.HTTPException as exc:
                log.warning("board edit failed: %s", exc)
                return
            self._perm_warned = False
            log.info("board updated%s", " (reply push)" if bypass_throttle else "")
        self.last_content = content
        self.last_edit = time.monotonic()

    def _warn_perm(self, exc):
        if not self._perm_warned:
            self._perm_warned = True
            log.error("Discord permission error (%s) — is the bot invited and "
                      "allowed in the target channel?", exc)


def main() -> int:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN not set; check the EnvironmentFile")
        return 2
    try:
        channel_id = int(os.environ.get("CHANNEL_ID", "0"))
    except ValueError:
        log.error("CHANNEL_ID must be an integer")
        return 2
    db_path = os.environ.get("DB_PATH", "/home/<USER>/dnd-dm-bot/data/campaign.db")
    resolve_game_chat(db_path)
    bot = HealthBoardBot(channel_id, db_path)
    bot.run(token, log_handler=None)  # logging already goes to stdout/journald
    return 0


if __name__ == "__main__":
    sys.exit(main())
