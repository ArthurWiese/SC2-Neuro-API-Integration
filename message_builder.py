from __future__ import annotations

from typing import Any


class NeuroAPIMessageBuilder:
    """Build outgoing Neuro API (game -> Neuro) messages.

    Message format:
    {
        "command": str,
        "game": str,
        "data": dict[str, Any]?
    }
    """

    _VALID_PRIORITIES = {"low", "medium", "high", "critical"}

    def __init__(self, game_title: str = "StarCraft 2") -> None:
        self.game_title = game_title

    def startup(self) -> dict[str, Any]:
        return {
            "command": "startup",
            "game": self.game_title,
        }

    def context(self, message: str, silent: bool = True) -> dict[str, Any]:
        return {
            "command": "context",
            "game": self.game_title,
            "data": {
                "message": message,
                "silent": silent,
            },
        }

    def actions_register(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "command": "actions/register",
            "game": self.game_title,
            "data": {
                "actions": actions,
            },
        }

    def actions_unregister(self, action_names: list[str]) -> dict[str, Any]:
        return {
            "command": "actions/unregister",
            "game": self.game_title,
            "data": {
                "action_names": action_names,
            },
        }

    def actions_force(
        self,
        query: str,
        action_names: list[str],
        state: str | None = None,
        ephemeral_context: bool = False,
        priority: str = "low",
    ) -> dict[str, Any]:
        if priority not in self._VALID_PRIORITIES:
            raise ValueError(
                f"Invalid priority '{priority}'. Expected one of: {sorted(self._VALID_PRIORITIES)}"
            )

        data: dict[str, Any] = {
            "query": query,
            "ephemeral_context": ephemeral_context,
            "priority": priority,
            "action_names": action_names,
        }
        if state is not None:
            data["state"] = state

        return {
            "command": "actions/force",
            "game": self.game_title,
            "data": data,
        }

    def action_result(self, action_id: str, success: bool, message: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": action_id,
            "success": success,
        }
        if message is not None:
            data["message"] = message

        return {
            "command": "action/result",
            "game": self.game_title,
            "data": data,
        }

