"""Private drafts and immutable approvals. No campaign or attachment URLs here."""

import json
import sqlite3
import uuid
from contextlib import contextmanager

from .persistence import StorageError, _json
from .script_solutions import MAX_SOLUTION_PROMPT, validate_solution


class ScriptError(ValueError):
    """Safe private workflow feedback (never provider exception text)."""


class ScriptStore:
    def __init__(self, repository):
        self.repository = repository

    @contextmanager
    def transaction(self):
        try:
            with self.repository.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        except sqlite3.Error:
            raise StorageError("Cannot store private adventure data") from None

    def create(self, guild_id, channel_id, user_id, source, total):
        draft_id = uuid.uuid4().hex
        with self.transaction() as conn:
            conn.execute("INSERT INTO script_drafts(guild_id,channel_id,draft_id,submitted_by,source_text,status,total) "
                         "VALUES (?,?,?,?,?,'preparing',?) ON CONFLICT(guild_id,channel_id) DO UPDATE SET "
                         "draft_id=excluded.draft_id,submitted_by=excluded.submitted_by,source_text=excluded.source_text,"
                         "status='preparing',completed=0,total=excluded.total,definition=NULL,issues='[]',"
                         "conversion_guidance='[]',solution=NULL,created_at=CURRENT_TIMESTAMP",
                         (str(guild_id), str(channel_id), draft_id, str(user_id), source, total))
        return draft_id

    @staticmethod
    def _draft(cursor):
        row = cursor.fetchone()
        if row is None:
            return None
        result = dict(zip([c[0] for c in cursor.description], row))
        result["definition"] = json.loads(result["definition"]) if result["definition"] else None
        result["issues"] = json.loads(result["issues"])
        result["conversion_guidance"] = json.loads(result["conversion_guidance"])
        result["solution"] = json.loads(result["solution"]) if result["solution"] else None
        return result

    def draft(self, guild_id, channel_id):
        with self.transaction() as conn:
            return self._draft(conn.execute("SELECT * FROM script_drafts WHERE guild_id=? AND channel_id=?",
                                           (str(guild_id), str(channel_id))))

    def preparing(self):
        with self.transaction() as conn:
            return [(r[0], r[1]) for r in conn.execute(
                "SELECT guild_id,channel_id FROM script_drafts WHERE status='preparing'")]

    def update(self, draft_id, *, completed=0, status="preparing", definition=None, issues=()):
        with self.transaction() as conn:
            return bool(conn.execute("UPDATE script_drafts SET completed=?,status=?,definition=?,issues=? "
                                     "WHERE draft_id=? AND status='preparing'",
                                     (completed, status, _json(definition) if definition else None,
                                      _json(list(issues)), draft_id)).rowcount)

    def suggesting(self):
        with self.transaction() as conn:
            rows = conn.execute("SELECT guild_id,channel_id,solution FROM script_drafts "
                                "WHERE status IN ('blocked','failed') AND solution IS NOT NULL")
            return [(g, c, s["id"]) for g, c, value in rows
                    if (s := json.loads(value))["status"] == "generating"]

    def solution_draft(self, guild_id, channel_id, draft_id, user_id, solution_id=None):
        draft = self.draft(guild_id, channel_id)
        self._owned(draft, draft_id, user_id)
        self._solution_current(draft, solution_id)
        return draft

    @staticmethod
    def _solution_current(draft, solution_id):
        if draft["status"] not in ("blocked", "failed"):
            raise ScriptError("只有轉換失敗或有問題的草稿可以提出及處理修法建議。")
        current_id = (draft["solution"] or {}).get("id", "")
        if solution_id is not None and current_id != solution_id:
            raise ScriptError("修法建議已更新；請用 /script status 審閱最新版本。")

    def begin_solution(self, guild_id, channel_id, draft_id, user_id, prompt="", solution_id=None):
        if not isinstance(prompt, str) or len(prompt) > MAX_SOLUTION_PROMPT:
            raise ScriptError(f"補充要求最多 {MAX_SOLUTION_PROMPT} 字元。")
        with self.transaction() as conn:
            draft = self._draft(conn.execute("SELECT * FROM script_drafts WHERE guild_id=? AND channel_id=?",
                                            (str(guild_id), str(channel_id))))
            self._owned(draft, draft_id, user_id)
            self._solution_current(draft, solution_id)
            previous = draft["solution"]
            solution = {"id": uuid.uuid4().hex, "status": "generating", "prompt": prompt.strip(), "proposal": None}
            if previous:
                solution["previous"] = {k: previous.get(k) for k in ("status", "prompt", "proposal")}
            conn.execute("UPDATE script_drafts SET solution=? WHERE draft_id=?", (_json(solution), draft_id))
        return solution["id"]

    def finish_solution(self, draft_id, solution_id, *, proposal=None, error=None):
        with self.transaction() as conn:
            draft = self._draft(conn.execute("SELECT * FROM script_drafts WHERE draft_id=?", (draft_id,)))
            if not draft or draft["status"] not in ("blocked", "failed"):
                return False
            solution = draft["solution"]
            if not solution or solution["id"] != solution_id or solution["status"] != "generating":
                return False
            solution.update(status="failed" if error else "pending", proposal=proposal)
            if error:
                solution["error"] = error
            conn.execute("UPDATE script_drafts SET solution=? WHERE draft_id=?", (_json(solution), draft_id))
        return True

    def decide_solution(self, guild_id, channel_id, draft_id, solution_id, user_id, *, accept):
        with self.transaction() as conn:
            draft = self._draft(conn.execute("SELECT * FROM script_drafts WHERE guild_id=? AND channel_id=?",
                                            (str(guild_id), str(channel_id))))
            self._owned(draft, draft_id, user_id)
            self._solution_current(draft, solution_id)
            solution = draft["solution"]
            if not solution or solution["status"] != "pending" or validate_solution(solution["proposal"]):
                raise ScriptError("這份建議目前不能接受或拒絕；請審閱最新建議或重新產生。")
            if not accept:
                solution["status"] = "denied"
                conn.execute("UPDATE script_drafts SET solution=? WHERE draft_id=?", (_json(solution), draft_id))
                return draft_id
            guidance = draft["conversion_guidance"] + [{"solution_id": solution_id, **solution["proposal"]}]
            new_id = uuid.uuid4().hex
            conn.execute("UPDATE script_drafts SET draft_id=?,status='preparing',completed=0,definition=NULL,"
                         "issues='[]',conversion_guidance=?,solution=NULL,created_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                         (new_id, _json(guidance), draft_id))
            return new_id

    def approve(self, guild_id, channel_id, draft_id, user_id):
        from .adventures import validate_definition
        with self.transaction() as conn:
            draft = self._draft(conn.execute("SELECT * FROM script_drafts WHERE guild_id=? AND channel_id=?",
                                            (str(guild_id), str(channel_id))))
            self._owned(draft, draft_id, user_id)
            if draft["status"] == "approved":
                version_id = conn.execute("SELECT version_id FROM adventure_versions WHERE draft_id=?", (draft_id,)).fetchone()[0]
                conn.execute("INSERT INTO script_selections VALUES (?,?,?) ON CONFLICT(guild_id,channel_id) "
                             "DO UPDATE SET version_id=excluded.version_id", (str(guild_id), str(channel_id), version_id))
                return version_id
            if draft["status"] != "ready" or draft["issues"] or validate_definition(draft["definition"]):
                raise ScriptError("草稿尚未完成或有轉換問題，不能批准。請先用 /script status 檢查。")
            version_id = uuid.uuid4().hex
            conn.execute("INSERT INTO adventure_versions(version_id,draft_id,guild_id,channel_id,submitted_by,source_text,definition,conversion_guidance) "
                         "VALUES (?,?,?,?,?,?,?,?)", (version_id, draft_id, str(guild_id), str(channel_id), str(user_id),
                                                     draft["source_text"], _json(draft["definition"]),
                                                     _json(draft["conversion_guidance"])))
            conn.execute("INSERT INTO script_selections VALUES (?,?,?) ON CONFLICT(guild_id,channel_id) "
                         "DO UPDATE SET version_id=excluded.version_id", (str(guild_id), str(channel_id), version_id))
            conn.execute("UPDATE script_drafts SET status='approved' WHERE draft_id=?", (draft_id,))
            return version_id

    @staticmethod
    def _owned(draft, draft_id, user_id):
        if draft is None or draft["draft_id"] != draft_id:
            raise ScriptError("草稿已被替換或移除；請重新用 /script status 取得草稿 ID。")
        if draft["submitted_by"] != str(user_id):
            raise ScriptError("只有提交這份草稿的現任管理員可以修改、批准或丟棄。")

    def discard(self, guild_id, channel_id, draft_id, user_id):
        with self.transaction() as conn:
            draft = self._draft(conn.execute("SELECT * FROM script_drafts WHERE guild_id=? AND channel_id=?",
                                            (str(guild_id), str(channel_id))))
            self._owned(draft, draft_id, user_id)
            if draft["status"] == "approved":
                raise ScriptError("這份草稿已批准；請用 /script clear 取消下次遊戲的選擇。")
            conn.execute("DELETE FROM script_drafts WHERE draft_id=?", (draft_id,))

    def clear(self, guild_id, channel_id):
        with self.transaction() as conn:
            conn.execute("DELETE FROM script_selections WHERE guild_id=? AND channel_id=?",
                         (str(guild_id), str(channel_id)))

    def selected(self, guild_id, channel_id):
        if not guild_id:
            return None
        with self.transaction() as conn:
            row = conn.execute("SELECT v.version_id,v.definition FROM adventure_versions v "
                               "JOIN script_selections s ON s.version_id=v.version_id "
                               "WHERE s.guild_id=? AND s.channel_id=? AND v.guild_id=s.guild_id AND v.channel_id=s.channel_id",
                               (str(guild_id), str(channel_id))).fetchone()
            return {"version_id": row[0], "definition": json.loads(row[1])} if row else None
