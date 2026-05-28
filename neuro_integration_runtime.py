from __future__ import annotations

import asyncio
from collections import deque
import json
from pathlib import Path
from watchdog.observers import Observer
from typing import Any, Callable, TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp
import xml.etree.ElementTree as ET
import psutil

from bank_file_io import (
    BankFileEventHandler,
    clear_force_action_section,
    deactivate_everything,
    parse_bank_file,
    write_bank_values,
    clear_game_context_flags,
)

if TYPE_CHECKING:
    from SC2_integration import TerminalApp


class NeuroIntegrationRuntimeMixin:
    def _runtime_init(self) -> None:
        self._neuro_builder = getattr(self, "_neuro_builder", None)
        if self._neuro_builder is None:
            from message_builder import NeuroAPIMessageBuilder

            self._neuro_builder = NeuroAPIMessageBuilder(game_title="StarCraft 2")

        self.integration_running: bool = False
        self._integration_task: asyncio.Task | None = None
        self._integration_stop_event: asyncio.Event | None = None
        self._neuro_session: aiohttp.ClientSession | None = None
        self._neuro_ws: aiohttp.ClientWebSocketResponse | None = None
        self._neuro_listener_task: asyncio.Task | None = None
        self._bank_monitor_task: asyncio.Task | None = None
        self._sc2_watchdog_task: asyncio.Task | None = None
        self._game_state_active_watchdog_task: asyncio.Task | None = None
        self._action_queue_worker_task: asyncio.Task | None = None
        self._bank_file_path: Path | None = None
        self._bank_watcher_observer: Any | None = None
        self._bank_change_queue: asyncio.Queue[str] | None = None
        self._in_mission: bool | None = None
        self._game_is_paused: bool = False
        self._game_is_blocking: bool = False
        self._game_state_active_value: int | None = None
        self._game_state_active_last_changed_time: float | None = None
        self._game_state_active_timeout_handled_value: int | None = None
        self._action_queue: deque[dict[str, Any]] = deque()
        self._action_queue_condition: asyncio.Condition = asyncio.Condition()
        self._active_actions: dict[str, dict[str, Any]] = {}
        self._last_parsed_bank_data: dict[str, dict[str, Any]] = {}
        self._bank_write_lock: asyncio.Lock | None = None
        self._bank_update_in_progress: bool = False
        self._action_queue_blocked_until: float = 0.0


    def _set_neuro_url(self, url_str: str) -> list[str]:
        url = url_str.strip()
        parsed = urlparse(url)

        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            return ["Error: neuro_url must be a valid websocket URL"]

        self.NEURO_URL = url
        self._save_configuration("neuro_url", self.NEURO_URL)

        return [f"Neuro URL set to: {self.NEURO_URL}"]

    async def _start_integration(self) -> list[str]:
        if self.integration_running:
            return ["Integration is already running."]

        if not self.banks_path:
            return ["Error: banks_path is not set. Use banks_path <path> first."]

        if not self.NEURO_URL:
            return ["Error: neuro_url is not set. Use neuro_url <websocket server url> first."]

        parsed = urlparse(self.NEURO_URL)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            return ["Error: neuro_url is invalid. Use neuro_url <websocket server url> to set a valid URL."]

        self._integration_stop_event = asyncio.Event()
        self._bank_write_lock = asyncio.Lock()
        self._action_queue_blocked_until = 0.0
        self._integration_task = asyncio.create_task(self._integration_worker(), name="neuro-integration-worker")
        self.integration_running = True

        return ["Integration started. Running bootstrap in background."]

    async def _stop_integration(self) -> list[str]:
        if not self.integration_running:
            return ["Integration is not running."]

        if self._integration_stop_event is not None:
            self._integration_stop_event.set()

        if self._integration_task is not None:
            self._integration_task.cancel()
            try:
                await self._integration_task
            except asyncio.CancelledError:
                pass

        await self._cleanup_integration_runtime()
        self.integration_running = False

        return ["Integration stopped."]

    async def _integration_worker(self) -> None:
        try:
            await self._connect_neuro_websocket()
            await self._send_neuro_startup()

            self._bank_file_path = await self._wait_for_integration_bank_file()
            # await self._process_initial_bank_state(bank_file)

            self._bank_monitor_task = asyncio.create_task(self._monitor_bank_changes(), name="bank-monitor")

            # Start SC2 process watchdog that will deactivate bank flags if SC2 exits
            if self._sc2_watchdog_task is None:
                self._sc2_watchdog_task = asyncio.create_task(self._monitor_sc2_process(), name="sc2-watchdog")

            # Is the game paused or not active check
            if self._game_state_active_watchdog_task is None:
                self._game_state_active_watchdog_task = asyncio.create_task(self._monitor_game_state_active_timeout(), name="game-state-active-watchdog")

            if self._action_queue_worker_task is None:
                self._action_queue_worker_task = asyncio.create_task(self._process_action_queue(), name="action-queue-worker")
            
            self.print_line("Integration bootstrap complete.", 1)

            while self._integration_stop_event is not None and not self._integration_stop_event.is_set():
                if not self._neuro_connection_is_healthy():
                    recovered = await self._recover_neuro_websocket()
                    if not recovered:
                        await asyncio.sleep(2.0)
                        continue

                if self._bank_monitor_task is None or self._bank_monitor_task.done():
                    self._bank_monitor_task = asyncio.create_task(self._monitor_bank_changes(), name="bank-monitor")

                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError, ValueError, ET.ParseError) as exc:
            self.print_line(f"Integration error: {exc}", 0)
            await self._cleanup_integration_runtime()
            self.integration_running = False

    async def _connect_neuro_websocket(self) -> None:
        if self.NEURO_URL is None:
            raise RuntimeError("Neuro URL is not configured")

        self.print_line(f"Connecting to Neuro websocket at {self.NEURO_URL}...", 1)

        timeout = aiohttp.ClientTimeout(total=15)
        session = aiohttp.ClientSession(timeout=timeout)
        try:
            ws = await session.ws_connect(self.NEURO_URL)
        except Exception:
            await session.close()
            raise

        self._neuro_session = session
        self._neuro_ws = ws
        self.print_line("Connected to Neuro websocket.", 1)

        self._neuro_listener_task = asyncio.create_task(self._listen_neuro_messages(), name="neuro-listener")

    def _neuro_connection_is_healthy(self) -> bool:
        if self._neuro_ws is None or getattr(self._neuro_ws, "closed", False):
            return False
        if self._neuro_listener_task is None or self._neuro_listener_task.done():
            return False
        return True

    async def _recover_neuro_websocket(self) -> bool:
        if self._integration_stop_event is not None and self._integration_stop_event.is_set():
            return False

        self.print_line("Neuro websocket disconnected; attempting reconnection...", 1)
        await self._close_neuro_connection()

        backoff_seconds = 1.0
        for _attempt in range(4):
            if self._integration_stop_event is not None and self._integration_stop_event.is_set():
                return False

            try:
                await self._connect_neuro_websocket()
                await self._send_neuro_startup()
                if self._bank_file_path is not None and self._bank_file_path.exists():
                    await self._process_initial_bank_state(self._bank_file_path)
                self.print_line("Neuro websocket reconnected successfully.", 1)
                return True
            except (aiohttp.ClientError, OSError, TimeoutError, RuntimeError, ValueError) as exc:
                self.print_line(f"Neuro reconnect attempt failed: {exc}", 0)
                await self._close_neuro_connection()
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 8.0)

        return False

    async def _close_neuro_connection(self) -> None:
        if self._neuro_listener_task is not None:
            self._neuro_listener_task.cancel()
            try:
                await self._neuro_listener_task
            except asyncio.CancelledError:
                pass
            self._neuro_listener_task = None

        if self._neuro_ws is not None:
            try:
                await self._neuro_ws.close()
            finally:
                self._neuro_ws = None

        if self._neuro_session is not None:
            try:
                await self._neuro_session.close()
            finally:
                self._neuro_session = None

    async def _wait_for_integration_bank_file(self) -> Path:
        if self.banks_path is None:
            raise RuntimeError("banks_path is not configured")

        bank_file = Path(self.banks_path) / "NeuroIntegration.SC2Bank"
        self.print_line(f"Waiting for bank file at {bank_file}", 1)
        last_reminder_time = asyncio.get_running_loop().time()
        reminder_interval_seconds = 30.0
    
        while True:
            if self._integration_stop_event is not None and self._integration_stop_event.is_set():
                raise asyncio.CancelledError()
            if bank_file.exists() and bank_file.is_file():
                self.print_line("Found 'NeuroIntegration.SC2Bank' bank file", 1)
                await asyncio.sleep(0.5)  # Small delay to ensure file is fully written before processing
                return bank_file
            current_time = asyncio.get_running_loop().time()
            if (current_time - last_reminder_time) >= reminder_interval_seconds:
                self.print_line(f"Waiting for bank file at {bank_file}", 1)
                last_reminder_time = current_time
            await asyncio.sleep(0.5)

    # async def _process_initial_bank_state(self, bank_file: Path) -> None:
    #     bank_data = parse_bank_file(bank_file)

    #     game_state = bank_data.get("game_state", {})
    #     in_mission = game_state.get("in_mission", False)
    #     self._in_mission = in_mission

    #     if not in_mission:
    #         self._game_is_paused = False
    #         await self._send_neuro_context("Currently in intermission")
    #         return
    #     else:
    #         self._record_game_state_active_value(int(game_state.get("active", 0)))


    async def _monitor_bank_changes(self) -> None:
        if self.banks_path is None or self._bank_file_path is None:
            raise RuntimeError("banks_path or bank file path is not configured")

        self._bank_change_queue = asyncio.Queue()
        handler = BankFileEventHandler(self, self._bank_file_path.name)
        observer = Observer()
        observer.schedule(handler, str(Path(self.banks_path)), recursive=False)
        observer.start()
        self._bank_watcher_observer = observer
        self.print_line("Bank watcher started.", 2)

        try:
            while self._integration_stop_event is not None and not self._integration_stop_event.is_set():
                if self._bank_change_queue is None:
                    await asyncio.sleep(0.25)
                    continue

                try:
                    changed_path = await asyncio.wait_for(self._bank_change_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if self._bank_file_path is None:
                    continue

                pending_path: str | None = await self._drain_bank_change_queue(changed_path)
                while pending_path is not None:
                    if Path(pending_path).name.lower() != self._bank_file_path.name.lower():
                        break

                    try:
                        await self._handle_bank_file_updated()
                    except (OSError, ET.ParseError, RuntimeError, ValueError) as exc:
                        self.print_line(f"Bank update handling error: {exc}", 0)

                    pending_path = await self._drain_bank_change_queue()
        finally:
            if self._bank_watcher_observer is not None:
                self._bank_watcher_observer.stop()
                self._bank_watcher_observer.join(timeout=2.0)
                self._bank_watcher_observer = None
            self._bank_change_queue = None

    async def _drain_bank_change_queue(self, initial_path: str | None = None) -> str | None:
        if self._bank_change_queue is None:
            return initial_path

        latest_path = initial_path
        while True:
            try:
                queued_path = self._bank_change_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            latest_path = queued_path

        return latest_path

    def _notify_bank_file_changed(self, src_path: str) -> None:
        """Called from the watchdog thread when a matching filesystem event occurs.
        This schedules a put into the async queue.
        """
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._event_loop
        if loop is None:
            return

        def _put():
            if self._bank_change_queue is not None:
                try:
                    self._bank_change_queue.put_nowait(src_path)
                except asyncio.QueueFull:
                    pass

        try:
            loop.call_soon_threadsafe(_put)
        except Exception:
            pass

    async def _handle_bank_file_updated(self) -> None:
        if self._bank_file_path is None or not self._bank_file_path.exists():
            self._clear_game_state_active_watchdog_state()
            self._game_is_paused = False
            self._action_queue.clear()
            await self._notify_action_queue_state_changed()
            return

        try:
            bank_data = parse_bank_file(self._bank_file_path)
        except FileNotFoundError:
            self._clear_game_state_active_watchdog_state()
            self._game_is_paused = False
            self._action_queue.clear()
            await self._notify_action_queue_state_changed()
            return
        except ET.ParseError:
            self.print_line("Bank parse failed on file change; retrying on the next change event.", 0)
            return
        
        if not bank_data or "game_state" not in bank_data:
            self.print_line("Bank file is empty or incomplete. Deregistering all action commands.", 0)
            # Game probably entered intermission if the bank file is empty or incomplete
            await self._send_neuro_context("Entered intermission; game cannot process commands until the next mission starts.")
            await asyncio.sleep(2)
            await self._cleanup_bank_file()
            return

        if bank_data == self._last_parsed_bank_data:
            return

        self._last_parsed_bank_data = bank_data

        game_state = bank_data.get("game_state", {})
        new_in_mission = game_state.get("in_mission", False)

        if new_in_mission:
            active_changed = self._record_game_state_active_value(game_state.get("active", 0))
            if active_changed:
                # Notify action queue worker that a new active value was recorded
                # This opens a 0.3s processing window for the next queued action
                await self._notify_action_queue_state_changed()
                if self._game_is_paused:
                    self._game_is_paused = False
                    await self._send_neuro_context("Game is now unpaused.")
        else:
            self._clear_game_state_active_watchdog_state()
            self._game_is_paused = False

        if self._in_mission and not new_in_mission:
            self._in_mission = False
            await self._send_neuro_context("Entered intermission; game cannot process commands until the next mission starts.")
            await asyncio.sleep(2)
            await self._cleanup_bank_file()
            # await self._run_serialized_bank_write(lambda: deactivate_everything(self._bank_file_path))
            # await self._handle_bank_file_updated()
            # self._action_queue.clear()
            # self.print_line("Entered intermission; deactivated all bank flags.", 2)
            return
        elif not self._in_mission and new_in_mission:
            self._in_mission = True
            await self._send_neuro_context("Entered mission; game can now process commands.")
            self.print_line("Entered mission.", 2)
        elif self._in_mission is None:
            self._in_mission = new_in_mission

        new_is_blocking = game_state.get("is_blocking", False)

        if self._game_is_blocking and not new_is_blocking:
            self._game_is_blocking = False
            if self._in_mission:
                await self._send_neuro_context("Game is no longer blocking commands.")
                self.print_line("Game is no longer blocking commands.", 2)
        elif not self._game_is_blocking and new_is_blocking:
            self._game_is_blocking = True
            await self._send_neuro_context("Probably entered a cutscene; Game is blocking and can't process action commands. " \
                                            "Commands will get added to a queue to be processed when the game unblocks.")
            self.print_line("Game is blocking commands", 2)
        elif self._game_is_blocking is None:
            self._game_is_blocking = new_is_blocking
        await self._notify_action_queue_state_changed()

        game_context = bank_data.get("game_context", {})
        possible_actions = bank_data.get("possible_actions", {})
        force_action = bank_data.get("force_action", {})
        
        if self._in_mission is True:
            skip = await self._skip_if_unsafe_bank_write_window()
            if skip:
                return

        self._bank_update_in_progress = True
        await self._notify_action_queue_state_changed()

        await self._clear_queue(game_state)

        await self._update_game_context(game_context)

        await self._sync_possible_actions(possible_actions)
    
        await self._process_force_action(force_action)
    
        self._bank_update_in_progress = False
        await self._notify_action_queue_state_changed()

    async def _skip_if_unsafe_bank_write_window(self) -> bool:
        last_changed_time = self._game_state_active_last_changed_time
        if last_changed_time is not None:
            elapsed_seconds = asyncio.get_running_loop().time() - last_changed_time
            if elapsed_seconds < 0.3:
                return False
            return True

            # async with self._action_queue_condition:
            #     await self._action_queue_condition.wait()
        return False
    
    async def _clear_queue(self, game_state: dict[str, Any]) -> None:
        clear_queue = game_state.get("clear_queue", False)
        if clear_queue:
            if len(self._action_queue) != 0:
                action_summary = ""
                for queued_action in self._action_queue:
                    action_summary += self._format_action_command_for_context(queued_action)
                self.print_line(f"Clearing action queue due to game request. Queued actions: {self._action_queue}", 2)
                await self._send_neuro_context(f"Clearing action queue due to game request. Actions that will not be processed: {action_summary}")
                self._action_queue.clear()
                await self._notify_action_queue_state_changed()
            update = {"game_state": {"clear_queue": False}}
            await self._run_serialized_bank_write(lambda: write_bank_values(self._bank_file_path, update))

    def _format_action_command_for_context(self, action_command: dict[str, Any]) -> str:
        action_name = action_command.get("name")
        action_args = action_command.get("args")

        arguments: list[str] = []
        for argument_name in sorted(action_args):
            argument_value = action_args[argument_name]
            arguments.append(f"{argument_value}")

        if arguments:
            return f"{action_name} with argument/s: {', '.join(arguments)}. "
        else:
            return f"{action_name}"

    async def _update_game_context(self, game_context: dict[str, Any]) -> None:
        if not isinstance(game_context, dict) or self._bank_file_path is None:
            return

        contexts: list[str] = []
        silent = True
        for key, value in game_context.items():
            if not isinstance(key, str):
                continue
            if key.endswith("_new") and value:
                base = key[:-4]
                contexts.append(game_context.get(base, base))
                if not game_context.get(f"{base}_silent"):
                    silent = False
        
        try:
            await self._run_serialized_bank_write(lambda: clear_game_context_flags(self._bank_file_path))
        except Exception:
            self.print_line("Failed to clear game_context flags in bank file after sending.", 0)

        if contexts:
            try:
                message = "\n".join(contexts)
                await self._send_neuro_context(message, silent=silent)
            except Exception as exc:
                self.print_line(f"Failed to send game context to Neuro: {exc}", 0)

    async def _sync_possible_actions(self, possible_actions_section: dict[str, Any]) -> None:
        extracted_actions = self._extract_actions(possible_actions_section)
        latest_actions = {action["name"]: action for action in extracted_actions}

        current_names = set(self._active_actions.keys())
        latest_names = set(latest_actions.keys())

        new_names = sorted(latest_names - current_names)
        missing_names = sorted(current_names - latest_names)
        changed_names = sorted(name for name in latest_names & current_names if self._active_actions.get(name) != latest_actions[name])

        if missing_names:
            await self._send_neuro_unregister_actions(missing_names)
            queue_actions_removed = [action for action in self._action_queue if action.get("name") in missing_names]
            if queue_actions_removed:
                summary = ""
                for removed in queue_actions_removed:
                    summary += self._format_action_command_for_context(removed)
                self.print_line(f"Removing queued actions due to them being unregistered: {summary}", 2)
                await self._send_neuro_context(f"Removing queued actions due to them being unregistered: {summary}")
                self._action_queue = deque(action for action in self._action_queue if action.get("name") not in missing_names)
            for name in missing_names:
                self._active_actions.pop(name, None)

        names_to_register = new_names + [name for name in changed_names if name not in new_names]
        if names_to_register:
            await self._send_neuro_register_actions([latest_actions[name] for name in names_to_register])
            for name in names_to_register:
                self._active_actions[name] = latest_actions[name]

        # Keep the local cache aligned even if the action did not need re-registration.
        for name in latest_names & current_names:
            self._active_actions[name] = latest_actions[name]

    def _extract_actions(self, possible_actions: dict[str, Any]) -> list[dict[str, Any]]:
        action_names: set[str] = set()
        for key in possible_actions:
            if key.endswith("_active"):
                action_names.add(key[:-7])

        actions: list[dict[str, Any]] = []
        for action_name in sorted(action_names):
            active_value = possible_actions.get(f"{action_name}_active")
            if not active_value:
                continue

            uses_raw = possible_actions.get(f"{action_name}_uses")
            try:
                uses = int(uses_raw)
            except (TypeError, ValueError):
                self.print_line(f"Invalid uses value for active action '{action_name}': {uses_raw!r}", 0)
                continue

            if uses == 0:
                self.print_line(f"Active action '{action_name}' has 0 uses; removing it from active actions.", 0)
                continue

            description = self._extract_action_description(possible_actions.get(f"{action_name}_description"), action_name)
            schema = self._build_action_schema(action_name, possible_actions)

            action_data: dict[str, Any] = {
                "name": action_name,
                "description": description,
                "uses": uses,
            }
            if schema:
                action_data["schema"] = schema

            actions.append(action_data)

        return actions

    def _extract_action_description(self, raw_value: Any, action_name: str) -> str:
        if isinstance(raw_value, str) and raw_value.strip():
            return raw_value.strip()
        if raw_value is not None:
            return str(raw_value)
        return f"Available game action: {action_name}"

    def _build_action_schema(self, action_name: str, possible_actions: dict[str, Any]) -> dict[str, Any]:
        argument_entries: list[tuple[int, str]] = []
        prefix = f"{action_name}_arg_"

        for key, value in possible_actions.items():
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            if not suffix.isdigit():
                continue
            if not isinstance(value, str):
                continue
            argument_entries.append((int(suffix), value.strip()))

        if not argument_entries:
            return {}

        argument_entries.sort(key=lambda item: item[0])
        properties: dict[str, Any] = {}
        required: list[str] = []

        for index, expected_type in argument_entries:
            argument_name = f"arg_{index}"
            properties[argument_name] = self._json_schema_for_bank_type(expected_type)
            required.append(argument_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def _json_schema_for_bank_type(self, expected_type: str) -> dict[str, Any]:
        normalized = expected_type.strip().lower()

        if normalized in {"string", "str", "text"}:
            return {"type": "string"}
        if normalized in {"int", "integer"}:
            return {"type": "integer"}
        if normalized in {"float", "fixed", "number", "decimal"}:
            return {"type": "number"}
        if normalized in {"bool", "boolean", "flag"}:
            return {"type": "boolean"}
        if normalized == "array":
            return {"type": "array"}
        if normalized == "object":
            return {"type": "object"}

        return {"type": "string"}
    
    async def _send_neuro_startup(self) -> None:
        if self._neuro_ws is None:
            raise RuntimeError("Neuro websocket is not connected")

        startup_payload = self._neuro_builder.startup()
        await self._neuro_ws.send_str(json.dumps(startup_payload))
        self.print_line("Integration -> Neuro: Startup command.", 2)

    async def _send_neuro_context(self, message: str, silent: bool = True) -> None:
        if message is None:
            return
        if self._neuro_ws is None:
            raise RuntimeError("Neuro websocket is not connected")
        payload = self._neuro_builder.context(message=message, silent=silent)
        await self._neuro_ws.send_str(json.dumps(payload))
        self.print_line("Integration -> Neuro: Context command.", 2)
        self.print_line(f"Context message: {message}", 2)
            
    async def _send_neuro_register_actions(self, actions: list[dict[str, Any]]) -> None:
        if self._neuro_ws is None:
            raise RuntimeError("Neuro websocket is not connected")
        sanitized_actions: list[dict[str, Any]] = []
        for action in actions:
            sanitized_action = dict(action)
            sanitized_action.pop("uses", None)
            sanitized_actions.append(sanitized_action)

        payload = self._neuro_builder.actions_register(actions=sanitized_actions)
        await self._neuro_ws.send_str(json.dumps(payload))
        self.print_line("Integration -> Neuro: Register actions command.", 2)
        self.print_line(f"Registered actions: {json.dumps(sanitized_actions)}", 2)

    async def _send_neuro_unregister_actions(self, action_names: list[str]) -> None:
        if self._neuro_ws is None:
            raise RuntimeError("Neuro websocket is not connected")
        payload = self._neuro_builder.actions_unregister(action_names=action_names)
        await self._neuro_ws.send_str(json.dumps(payload))
        self.print_line("Integration -> Neuro: Unregister actions command.", 2)
        self.print_line(f"Unregistered actions: {json.dumps(action_names)}", 2)

    async def _unregister_all_active_actions(self) -> None:
        if not self._active_actions:
            return

        action_names = sorted(self._active_actions)
        await self._send_neuro_unregister_actions(action_names)
        self._active_actions.clear()

    async def _send_neuro_force_actions(
        self,
        query: str,
        action_names: list[str],
        state: str | None = None,
        ephemeral_context: bool = False,
        priority: str = "low",
    ) -> None:
        if self._neuro_ws is None:
            raise RuntimeError("Neuro websocket is not connected")

        payload = self._neuro_builder.actions_force(
            query=query,
            action_names=action_names,
            state=state,
            ephemeral_context=ephemeral_context,
            priority=priority,
        )
        await self._neuro_ws.send_str(json.dumps(payload))
        self.print_line("Integration -> Neuro: Force actions command.", 2)
        self.print_line(
            f"Force actions: query={query!r}, state={state!r}, ephemeral_context={ephemeral_context}, priority={priority!r}, actions={json.dumps(action_names)}",
            2,
        )

    async def _send_neuro_action_result(self, action_id: str, success: bool, message: str | None = None) -> None:
        if self._neuro_ws is None:
            raise RuntimeError("Neuro websocket is not connected")
        payload = self._neuro_builder.action_result(action_id=action_id, success=success, message=message)
        await self._neuro_ws.send_str(json.dumps(payload))


    async def _process_force_action(self, force_action_section: dict[str, Any]) -> None:
        if not isinstance(force_action_section, dict):
            return

        force_groups: list[tuple[list[str], str, str, bool, str]] = []
        any_sent = False

        for key, query_value in force_action_section.items():
            if not isinstance(key, str) or not key.endswith("_query"):
                continue

            group_key = key[:-6]
            action_names = self._parse_force_action_group_names(group_key)
            if not action_names:
                continue

            state_value = force_action_section.get(f"{group_key}_state")
            ephemeral_value = force_action_section.get(f"{group_key}_ephemeral_context")
            priority_value = force_action_section.get(f"{group_key}_priority")

            priority = priority_value.strip().lower()
            if priority not in {"low", "medium", "high", "critical"}:
                continue

            if any(action_name not in self._active_actions for action_name in action_names):
                continue

            force_groups.append((action_names, query_value, state_value, ephemeral_value, priority))

        try:
            for action_names, query_value, state_value, ephemeral_value, priority in force_groups:
                try:
                    await self._send_neuro_force_actions(
                        query=query_value,
                        action_names=action_names,
                        state=state_value,
                        ephemeral_context=ephemeral_value,
                        priority=priority,
                    )
                    any_sent = True
                except Exception as exc:
                    self.print_line(f"Failed to send force actions for {json.dumps(action_names)}: {exc}", 0)
        finally:
            if any_sent and self._bank_file_path is not None:
                try:
                    await self._run_serialized_bank_write(lambda: clear_force_action_section(self._bank_file_path))
                except Exception as exc:
                    self.print_line(f"Failed to clear force_action section in bank file: {exc}", 0)

    def _parse_force_action_group_names(self, group_key: str) -> list[str]:
        action_names: list[str] = []
        seen: set[str] = set()

        for raw_name in group_key.split(","):
            action_name = raw_name.strip()
            if not action_name or action_name in seen:
                continue
            seen.add(action_name)
            action_names.append(action_name)

        return action_names
    
    async def _listen_neuro_messages(self) -> None:
        if self._neuro_ws is None:
            return

        try:
            async for msg in self._neuro_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_neuro_text_message(msg.data)
                elif msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                    self.print_line("Neuro websocket closed.", 1)
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.print_line("Neuro websocket reported an error.", 0)
                    break
        except asyncio.CancelledError:
            pass
        except (aiohttp.ClientError, OSError, RuntimeError) as exc:
            self.print_line(f"Neuro listener error: {exc}", 0)

    async def _handle_neuro_text_message(self, payload_text: str) -> None:
        self.print_line(f"Received message from Neuro: {payload_text}", 2)

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            self.print_line(f"Received invalid JSON from Neuro: {payload_text}", 0)
            return

        if not isinstance(payload, dict):
            self.print_line(f"Received malformed message from Neuro: {payload_text}", 0)
            return

        command = payload.get("command")
        if command not in ["action", "actions/reregister_all"]:
            self.print_line(f"Received unknown command from Neuro: {command}", 0)
            return
        
        if command == "actions/reregister_all":
            await self._handle_neuro_reregister_all_command()
            return

        data = payload.get("data")
        if not isinstance(data, dict):
            self.print_line(f"Received malformed command data from Neuro: {payload_text}", 0)
            return

        await self._handle_neuro_action_command(data)
    
    async def _handle_neuro_reregister_all_command(self) -> None:
        if not self._active_actions:
            self.print_line("Received reregister all command from Neuro but there are no active actions to register.", 2)
            return

        await self._send_neuro_register_actions(list(self._active_actions.values()))
        self.print_line(f"Re-registered all {len(self._active_actions)} active action(s) with Neuro.", 2)

    async def _handle_neuro_action_command(self, data: dict[str, Any]) -> None:
        action_id = str(data.get("id") or "").strip()
        action_name = str(data.get("name") or "").strip()
        if not action_id or not action_name:
            self.print_line(f"Received malformed action command from Neuro: {data}", 0)
            return

        if action_name not in self._active_actions:
            self.print_line(f"Received action command {action_name} but it is not in the list of active actions.", 0)
            await self._send_neuro_action_result(action_id, False, f"Unknown action '{action_name}'. Sending all available actions.")
            await self._handle_neuro_reregister_all_command()
            return

        action_definition = self._active_actions[action_name]
        action_args = self._parse_action_arguments(data.get("data"), action_definition.get("schema"))
        if isinstance(action_args, Exception):
            await self._send_neuro_action_result(action_id, False, f"Invalid action arguments: {action_args}. Sending all available actions.")
            await self._handle_neuro_reregister_all_command()
            return

        await self._send_neuro_action_result(action_id, True, f"Action '{action_name}' is being executed.")

        await self._enqueue_action_command({"id": action_id, "name": action_name, "args": action_args})
       
        await self._notify_action_queue_state_changed()

    def _parse_action_arguments(self, raw_data: Any, schema: dict[str, Any] | None = None) -> dict[str, Any] | None | Exception:
        if raw_data is None:
            if schema:
                return ValueError("missing action arguments")
            return None
        if isinstance(raw_data, dict):
            arguments = raw_data
        elif isinstance(raw_data, str):
            stripped = raw_data.strip()
            if stripped == "":
                if schema:
                    return ValueError("missing action arguments")
                return None
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as exc:
                return exc
            if not isinstance(decoded, dict):
                return ValueError("arguments must decode to a JSON object")
            arguments = decoded
        else:
            return ValueError("arguments must be JSON object text")

        if schema is None or not schema:
            return arguments

        validation_error = self._validate_action_arguments(arguments, schema)
        if validation_error is not None:
            return validation_error

        return arguments

    def _validate_action_arguments(self, arguments: dict[str, Any], schema: dict[str, Any]) -> ValueError | None:
        if schema.get("type") != "object":
            return ValueError("action schema must be an object schema")

        required = schema.get("required", [])
        properties = schema.get("properties", {})
        additional_properties = schema.get("additionalProperties", True)

        if not isinstance(required, list) or not isinstance(properties, dict):
            return ValueError("invalid action schema")

        for property_name in required:
            if property_name not in arguments:
                return ValueError(f"missing required argument '{property_name}'")

        if not additional_properties:
            unexpected_arguments = sorted(set(arguments) - set(properties))
            if unexpected_arguments:
                return ValueError(f"unexpected argument(s): {', '.join(unexpected_arguments)}")

        for property_name, property_schema in properties.items():
            if property_name not in arguments:
                continue
            type_error = self._validate_json_schema_value(arguments[property_name], property_schema, property_name)
            if type_error is not None:
                return type_error

        return None

    def _validate_json_schema_value(self, value: Any, schema: Any, property_name: str) -> ValueError | None:
        if not isinstance(schema, dict):
            return ValueError(f"invalid schema for '{property_name}'")

        expected_type = schema.get("type")
        if expected_type == "string":
            if not isinstance(value, str):
                return ValueError(f"argument '{property_name}' must be a string")
        elif expected_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return ValueError(f"argument '{property_name}' must be an integer")
        elif expected_type == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return ValueError(f"argument '{property_name}' must be a number")
        elif expected_type == "boolean":
            if not isinstance(value, bool):
                return ValueError(f"argument '{property_name}' must be a boolean")
        elif expected_type == "array":
            if not isinstance(value, list):
                return ValueError(f"argument '{property_name}' must be an array")
        elif expected_type == "object":
            if not isinstance(value, dict):
                return ValueError(f"argument '{property_name}' must be an object")

        return None

    async def _process_action_queue(self) -> None:
        try:
            while self._integration_stop_event is not None and not self._integration_stop_event.is_set():
                async with self._action_queue_condition:
                    loop = asyncio.get_running_loop()
                    while (
                        self._integration_stop_event is not None
                        and not self._integration_stop_event.is_set()
                        and (
                            not self._action_queue
                            or not self._in_mission
                            or self._game_is_paused
                            or self._game_is_blocking
                            or self._bank_update_in_progress
                            or loop.time() < self._action_queue_blocked_until
                        )
                    ):
                        timeout = None
                        if loop.time() < self._action_queue_blocked_until:
                            timeout = self._action_queue_blocked_until - loop.time()
                        if timeout is None:
                            await self._action_queue_condition.wait()
                        else:
                            try:
                                await asyncio.wait_for(self._action_queue_condition.wait(), timeout=timeout)
                            except asyncio.TimeoutError:
                                pass

                    if self._integration_stop_event is not None and self._integration_stop_event.is_set():
                        break

                    if not self._action_queue:
                        continue

                    # Check if within 0.3s of the last active value change
                    loop = asyncio.get_running_loop()
                    now = loop.time()
                    last_change = self._game_state_active_last_changed_time
                    
                    if last_change is None or (now - last_change) > 0.3:
                        await self._action_queue_condition.wait()
                        continue

                    # Is within the 0.3s window
                    action_command = self._action_queue.popleft()

                await self._execute_queued_action_command(action_command)
                self._action_queue_blocked_until = asyncio.get_running_loop().time() + 1.0
                await self._notify_action_queue_state_changed()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.print_line(f"Action queue worker error: {exc}", 0)

    async def _enqueue_action_command(self, action_command: dict[str, Any]) -> None:
        queue_full = len(self._action_queue) >= 3
        removed_action: dict[str, Any] | None = None
        if queue_full:
            removed_action = self._action_queue.popleft()
            await self._send_neuro_context(f"Action command queue is full. The oldest queued command was removed to make room for a newer command.\nRemoved queued command: {self._format_action_command_for_context(removed_action)}")
        self._action_queue.append(action_command)

    async def _execute_queued_action_command(self, action_command: dict[str, Any]) -> None:
        action_id = str(action_command.get("id") or "").strip()
        action_name = str(action_command.get("name") or "").strip()
        action_args = action_command.get("args")

        if not action_id or not action_name:
            return

        if self._bank_file_path is None:
            self.print_line("Queued action could not be executed because the bank file path is not available.", 0)
            await self._send_neuro_action_result(action_id, False, "Bank file path is not available.")
            return

        updates: dict[str, dict[str, Any]] = {"do_action": {action_name: True}}
        if isinstance(action_args, dict):
            for argument_name, argument_value in action_args.items():
                updates["do_action"][f"{action_name}_{argument_name}"] = argument_value

        action_definition = self._active_actions.get(action_name)
        if not isinstance(action_definition, dict):
            self.print_line(f"Queued action '{action_name}' could not be executed because it is no longer active.", 0)
            await self._send_neuro_action_result(action_id, False, f"Action '{action_name}' is no longer active.")
            return

        current_uses = int(action_definition.get("uses"))

        if current_uses >= 0:
            next_uses = current_uses - 1
            possible_actions_updates = updates.setdefault("possible_actions", {})
            possible_actions_updates[f"{action_name}_uses"] = next_uses
            if next_uses == 0:
                possible_actions_updates[f"{action_name}_active"] = False
        try:
            await self._run_serialized_bank_write(lambda: write_bank_values(self._bank_file_path, updates))
        except (OSError, ET.ParseError, RuntimeError, ValueError) as exc:
            self.print_line(f"Failed to write queued action request to bank file: {exc}", 0)
            await self._send_neuro_action_result(action_id, False, f"Failed to execute action '{action_name}': {exc}")

    def _is_sc2_running_sync(self) -> bool:
        try:
            for p in psutil.process_iter(['name']):
                try:
                    name = (p.info.get('name') or '').lower()
                except Exception:
                    continue
                if name == 'sc2_x64.exe':
                    return True
        except Exception:
            pass
        return False

    async def _monitor_sc2_process(self) -> None:
        """Periodic task that ensures SC2 is running; if not, deactivate bank flags."""
        try:
            deactivated_for_current_outage = False
            while self._integration_stop_event is not None and not self._integration_stop_event.is_set():
                try:
                    running = await asyncio.get_running_loop().run_in_executor(None, self._is_sc2_running_sync)
                    if running:
                        deactivated_for_current_outage = False
                    else:
                        if not deactivated_for_current_outage and self._bank_file_path is not None and self._bank_file_path.exists():
                            try:
                                # await self._run_serialized_bank_write(lambda: deactivate_everything(self._bank_file_path))
                                # self._action_queue.clear()
                                deactivated_for_current_outage = True
                                self.print_line('SC2 not running — clearing bank file.', 2)
                                await self._cleanup_bank_file()
                            except Exception as exc:
                                self.print_line(f'SC2 watchdog failed to deactivate bank file: {exc}', 0)
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self.print_line(f'SC2 watchdog error: {exc}', 0)
                    await asyncio.sleep(5.0)
        finally:
            pass
    
    def _record_game_state_active_value(self, active_value: int) -> bool:
        loop = asyncio.get_running_loop()
        current_time = loop.time()
        if self._game_state_active_value != active_value:
            self._game_state_active_value = active_value
            self._game_state_active_last_changed_time = current_time
            self._game_state_active_timeout_handled_value = None
            return True

        if self._game_state_active_last_changed_time is None:
            self._game_state_active_last_changed_time = current_time
        return False

    def _clear_game_state_active_watchdog_state(self) -> None:
        self._game_state_active_value = None
        self._game_state_active_last_changed_time = None
        self._game_state_active_timeout_handled_value = None

    async def _notify_action_queue_state_changed(self) -> None:
        async with self._action_queue_condition:
            self._action_queue_condition.notify_all()

    async def _monitor_game_state_active_timeout(self) -> None:
        while self._integration_stop_event is not None and not self._integration_stop_event.is_set():
            try:
                if (
                    self._in_mission is True
                    and self._bank_file_path is not None
                    and self._game_state_active_value is not None
                    and self._game_state_active_last_changed_time is not None
                    and self._game_state_active_timeout_handled_value != self._game_state_active_value
                ):
                    elapsed_seconds = asyncio.get_running_loop().time() - self._game_state_active_last_changed_time
                    if elapsed_seconds >= 2.5:
                        try:
                            if not self._game_is_paused:
                                await self._send_neuro_context(
                                    "Game is currently paused and can't process action commands. Commands will get added to a queue to be processed when the game is unpaused."
                                )
                                self._game_is_paused = True
                                self._game_state_active_timeout_handled_value = self._game_state_active_value
                                await self._notify_action_queue_state_changed()
                                self.print_line("game_state.active value has not changed for 2.5s; game is now paused.", 2)
                        except Exception as exc:
                            self.print_line(f"Failed to pause game after active timeout: {exc}", 0)
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.print_line(f"game_state.active watchdog error: {exc}", 0)
                await asyncio.sleep(0.5)

    async def _run_serialized_bank_write(self, write_operation: Callable[[], None]) -> None:
        if self._bank_write_lock is None:
            write_operation()
            return

        async with self._bank_write_lock:
            write_operation()

    async def _cleanup_bank_file(self) -> None:
        if self._bank_file_path is not None:
            try:
                self._bank_file_path.unlink(missing_ok=True)
                self.print_line("Deleted empty/incomplete bank file.", 2)
            except OSError as exc:
                self.print_line(f"Failed to delete empty/incomplete bank file: {exc}", 0)
        try:
            await self._unregister_all_active_actions()
        except Exception as exc:
            self.print_line(f"Failed to unregister active actions after empty bank data: {exc}", 0)
        self._action_queue.clear()
        await self._notify_action_queue_state_changed()

    async def _cleanup_integration_runtime(self) -> None:
        if self._bank_monitor_task is not None:
            self._bank_monitor_task.cancel()
            try:
                await self._bank_monitor_task
            except asyncio.CancelledError:
                pass
            self._bank_monitor_task = None

        if self._sc2_watchdog_task is not None:
            self._sc2_watchdog_task.cancel()
            try:
                await self._sc2_watchdog_task
            except asyncio.CancelledError:
                pass
            self._sc2_watchdog_task = None

        if self._bank_watcher_observer is not None:
            self._bank_watcher_observer.stop()
            self._bank_watcher_observer.join(timeout=2.0)
            self._bank_watcher_observer = None

        if self._game_state_active_watchdog_task is not None:
            self._game_state_active_watchdog_task.cancel()
            try:
                await self._game_state_active_watchdog_task
            except asyncio.CancelledError:
                pass
            self._game_state_active_watchdog_task = None

        if self._action_queue_worker_task is not None:
            self._action_queue_worker_task.cancel()
            try:
                await self._action_queue_worker_task
            except asyncio.CancelledError:
                pass
            self._action_queue_worker_task = None

        await self._close_neuro_connection()

        self._integration_task = None
        self._integration_stop_event = None
        self._bank_write_lock = None
        self._bank_file_path = None
        self._bank_change_queue = None
        self._action_queue.clear()
        self._active_actions = {}
        self._in_mission = None
        self._bank_update_in_progress = False
        self._action_queue_blocked_until = 0.0
        self._game_is_paused = False
        self._game_state_active_value = None
        self._game_state_active_last_changed_time = None
        self._game_state_active_timeout_handled_value = None
        self._last_parsed_bank_data = {}
