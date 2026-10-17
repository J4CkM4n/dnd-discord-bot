"""Ephemeral controls for versioned private script conversion proposals."""

import logging

import discord

from core.script_solutions import MAX_SOLUTION_PROMPT

log = logging.getLogger("discord_adapter")


class ScriptSolutionPrompt(discord.ui.Modal, title="補充要求並重新產生修法"):
    def __init__(self, bot, draft_id, solution_id, prompt=""):
        super().__init__(timeout=900)
        self.bot, self.draft_id, self.solution_id = bot, draft_id, solution_id
        self.prompt = discord.ui.TextInput(
            label="希望保留的規則、可接受的改編或其他要求",
            style=discord.TextStyle.paragraph, required=False,
            max_length=MAX_SOLUTION_PROMPT, default=prompt or None)
        self.add_item(self.prompt)

    async def on_submit(self, interaction):
        await self.bot._suggest_script(interaction, self.draft_id, str(self.prompt), self.solution_id)

    async def on_error(self, interaction, error):
        log.warning("private script solution modal failed | error=%s", type(error).__name__)
        await self.bot._private_reply(interaction, "未能處理修法要求；請用 /script status 重試。")


class ScriptSolutionView(discord.ui.View):
    def __init__(self, bot, draft):
        super().__init__(timeout=900)
        self.bot, self.draft_id = bot, draft["draft_id"]
        solution = draft.get("solution")
        self.solution_id = solution["id"] if solution else ""
        pending = solution and solution["status"] == "pending"
        if solution:
            self.remove_item(self.generate)
        if not pending:
            self.remove_item(self.accept)
            self.remove_item(self.deny)
        for item in self.children:
            item.custom_id = f"script-solution:{item.custom_id}:{self.draft_id}:{self.solution_id}"

    @discord.ui.button(label="產生修法建議", custom_id="generate", style=discord.ButtonStyle.primary)
    async def generate(self, interaction, button):
        await self.bot._suggest_script(interaction, self.draft_id, "", self.solution_id)

    @discord.ui.button(label="接受並重新轉換", custom_id="accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction, button):
        await self.bot._decide_script_solution(interaction, self.draft_id, self.solution_id, accept=True)

    @discord.ui.button(label="拒絕建議", custom_id="deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        await self.bot._decide_script_solution(interaction, self.draft_id, self.solution_id, accept=False)

    @discord.ui.button(label="補充要求／重新建議", custom_id="regenerate", style=discord.ButtonStyle.secondary)
    async def regenerate(self, interaction, button):
        await self.bot._prompt_script_solution(interaction, self.draft_id, self.solution_id)

    async def on_error(self, interaction, error, item):
        log.warning("private script solution button failed | error=%s", type(error).__name__)
        await self.bot._private_reply(interaction, "未能處理修法操作；請用 /script status 重試。")
