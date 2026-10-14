"""A stand-in for the rationale model: records what it was sent, then answers, fails or stalls."""

import asyncio

from langchain_core.messages import AIMessage


class FakeModel:
    def __init__(self) -> None:
        self.mode = "ok"  # "ok", "error", "slow", "empty" or "empty_once"
        self.reply = "Fake rationale: the policy decided this from the hit summary."
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.mode == "error":
            raise RuntimeError("model unavailable")
        if self.mode == "slow":
            await asyncio.sleep(30)
        if self.mode == "empty_once":
            self.mode = "ok"
            return AIMessage(content="")
        return AIMessage(content="" if self.mode == "empty" else self.reply)
