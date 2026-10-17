"""Live, guild-scoped bot permissions, independent of campaign history."""

import os
import re
import sqlite3

from .agent_config import ConfigError
from .persistence import StorageError


def initial_admin_id(value=None):
    value = os.getenv("ADMIN_ID", "") if value is None else value
    value = value.strip()
    if value and (not re.fullmatch(r"[0-9]{1,20}", value)
                  or not 0 < int(value) < 2**64):
        raise ConfigError("ADMIN_ID must contain one valid Discord user ID, or be empty")
    return str(int(value)) if value else ""


class AdminPolicy:
    def __init__(self, repository, initial_id=None):
        self.repository = repository
        self.initial_id = initial_admin_id(initial_id)

    def authorized(self, guild_id, user_id, *, manage_server=False, member=True, bot=False):
        if not guild_id or not user_id or not member or bot:
            return False
        if str(user_id) == self.initial_id or manage_server:
            return True
        try:
            with self.repository.connect() as conn:
                return conn.execute("SELECT 1 FROM bot_admins WHERE guild_id=? AND user_id=?",
                                    (str(guild_id), str(user_id))).fetchone() is not None
        except sqlite3.Error:
            raise StorageError("Cannot read bot admin permissions") from None

    def change(self, guild_id, user_id, actor_id, *, promote):
        """Caller checks current membership and authorization; no cached grants."""
        if not promote and str(user_id) == self.initial_id:
            return False
        try:
            with self.repository.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if promote:
                    result = conn.execute(
                        "INSERT OR IGNORE INTO bot_admins(guild_id,user_id,promoted_by) VALUES (?,?,?)",
                        (str(guild_id), str(user_id), str(actor_id)))
                else:
                    result = conn.execute("DELETE FROM bot_admins WHERE guild_id=? AND user_id=?",
                                          (str(guild_id), str(user_id)))
                changed = bool(result.rowcount)
                conn.execute("INSERT INTO admin_audit(guild_id,user_id,actor_id,action,changed) "
                             "VALUES (?,?,?,?,?)", (str(guild_id), str(user_id), str(actor_id),
                                                   "promote" if promote else "demote", int(changed)))
                return changed
        except sqlite3.Error:
            raise StorageError("Cannot update bot admin permissions") from None

    def list(self, guild_id):
        try:
            with self.repository.connect() as conn:
                return [dict(zip(("user_id", "promoted_by", "promoted_at"), row)) for row in conn.execute(
                    "SELECT user_id,promoted_by,promoted_at FROM bot_admins WHERE guild_id=? ORDER BY promoted_at,user_id",
                    (str(guild_id),))]
        except sqlite3.Error:
            raise StorageError("Cannot list bot admins") from None
