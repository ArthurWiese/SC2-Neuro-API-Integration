"""Entry point for the SC2 integration shell."""

import json
import os
import re
import websockets
import asyncio
import subprocess
import threading
import queue
import tkinter as tk
from pathlib import Path, WindowsPath
from urllib.parse import urlparse
import aiohttp
from aiohttp.client_ws import ClientWebSocketResponse
from tkinter import ttk

import sc2api_connection_handler
import protocol
from neuro_integration_runtime import NeuroIntegrationRuntimeMixin


class TerminalApp(NeuroIntegrationRuntimeMixin):
    BG = "#0f1115"
    SURFACE = "#171b21"
    SURFACE_ELEVATED = "#1d232b"
    BORDER = "#2a313b"
    TEXT = "#e7ebf0"
    MUTED = "#9aa3af"
    ACCENT = "#4da3ff"
    ACCENT_SOFT = "#244a74"
    WARNING_COLOR = "#ff5555"
    VERBOSITY = 1  # 0 = warning/error, 1 = info, 2 = verbose/debug
    
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("SC2 Neuro Integration Terminal")
        self.root.geometry("1300x660")
        self.root.minsize(960, 480)
        self.root.resizable(True, True)
        self.root.configure(bg=self.BG)

        self.command_history: list[str] = []
        self.history_index: int = -1

        self.config_file = Path("configure.json")
        self.game_path: str | None = None
        self.banks_path: str | None = None
        self.NEURO_URL: str | None = None
        self._ui_queue: queue.Queue[tuple[str, int, bool]] = queue.Queue()
        self._closing: bool = False
        self.connection_handler = sc2api_connection_handler.SC2ConnectionHandler(self)
        self._runtime_init()

        self.sc2api_launch_arg_ip = ["-listen", "127.0.0.1"]
        self.sc2api_launch_arg_port = ["-port", "5000"]
        self.sc2api_process: subprocess.Popen | None = None
        self.sc2api_session: aiohttp.ClientSession | None = None
        self.sc2api_ws: ClientWebSocketResponse | None = None
        self.game_launched = False
        
        # Persistent event loop for async operations
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.style.configure("Card.TFrame", background=self.SURFACE, relief="flat")
        self.style.configure(
            "Pane.TLabel",
            background=self.SURFACE,
            foreground=self.MUTED,
            font=("Segoe UI", 10),
        )
        self.style.configure(
            "Command.TEntry",
            fieldbackground=self.SURFACE_ELEVATED,
            background=self.SURFACE_ELEVATED,
            foreground=self.TEXT,
            insertcolor=self.TEXT,
            padding=(12, 8),
        )
        self.style.map(
            "Command.TEntry",
            fieldbackground=[("focus", self.SURFACE_ELEVATED), ("readonly", self.SURFACE_ELEVATED)],
        )
        self.style.configure(
            "Terminal.Vertical.TScrollbar",
            background=self.SURFACE_ELEVATED,
            troughcolor=self.SURFACE,
            arrowcolor=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            gripcount=0,
            relief="flat",
        )
        self.style.configure(
            "Terminal.Horizontal.TScrollbar",
            background=self.SURFACE_ELEVATED,
            troughcolor=self.SURFACE,
            arrowcolor=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            gripcount=0,
            relief="flat",
        )

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._build_terminal_card()

        self.root.bind("<Control-l>", self._clear_history)
        self.root.bind("<Control-q>", self.close)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(50, self._drain_ui_queue)
        
        # Start persistent event loop
        self._start_event_loop()

        # Terminal init output
        self.is_windows = self._check_windows()

        if self.is_windows:
            self.print_line("Detected platform is Windows", 2)
        else:
            self.print_line("Detected platform is not Windows", 0)
            self.print_line("Warning: This application is designed for Windows and may not function correctly on other operating systems.", 0)

        self._load_configuration()

        self.print_line("\nSC2 Neuro Integration Terminal ready!", 1)
        self.print_line("Type 'help' for available commands.", 1)
        self.command_entry.focus_set()

    def _check_windows(self) -> bool:
        return os.name == "nt"

    def _start_event_loop(self) -> None:
        """Start a persistent event loop in a background thread."""
        def run_loop():
            self._event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._event_loop)
            self._event_loop.run_forever()

        self._loop_thread = threading.Thread(target=run_loop, daemon=True)
        self._loop_thread.start()

    def _run_async(self, coro):
        """Run a coroutine in the persistent event loop and wait for result."""
        if self._event_loop is None:
            raise RuntimeError("Event loop not initialized")
        future = asyncio.run_coroutine_threadsafe(coro, self._event_loop)
        return future.result()  # This blocks until the coroutine completes

    def _build_terminal_card(self) -> None:
        card = tk.Frame(
            self.root,
            bg=self.SURFACE,
            highlightbackground=self.BORDER,
            highlightcolor=self.BORDER,
            highlightthickness=1,
            bd=0,
        )
        card.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        card.rowconfigure(1, weight=0)

        terminal_surface = tk.Frame(card, bg=self.SURFACE)
        terminal_surface.grid(row=0, column=0, sticky="nsew", padx=14, pady=(14, 10))
        terminal_surface.columnconfigure(0, weight=1)
        terminal_surface.rowconfigure(0, weight=1)

        self.history = tk.Text(
            terminal_surface,
            wrap="none",
            undo=False,
            borderwidth=0,
            highlightthickness=0,
            background=self.SURFACE,
            foreground=self.TEXT,
            insertbackground=self.TEXT,
            selectbackground=self.ACCENT_SOFT,
            selectforeground=self.TEXT,
            font=("Cascadia Mono", 11),
            padx=12,
            pady=10,
            spacing1=2,
            spacing2=2,
            spacing3=4,
        )
        self.history.configure(state="disabled")

        vertical_scrollbar = ttk.Scrollbar(
            terminal_surface,
            orient="vertical",
            command=self.history.yview,
            style="Terminal.Vertical.TScrollbar",
        )
        horizontal_scrollbar = ttk.Scrollbar(
            terminal_surface,
            orient="horizontal",
            command=self.history.xview,
            style="Terminal.Horizontal.TScrollbar",
        )
        self.history.configure(yscrollcommand=vertical_scrollbar.set, xscrollcommand=horizontal_scrollbar.set)

        self.history.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        command_bar = tk.Frame(card, bg=self.SURFACE_ELEVATED, highlightthickness=0)
        command_bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 14))
        command_bar.columnconfigure(1, weight=1)

        ttk.Label(command_bar, text=">", style="Pane.TLabel").grid(row=0, column=0, padx=(0, 10), pady=12, sticky="w")
        self.command_var = tk.StringVar()
        self.command_entry = ttk.Entry(command_bar, textvariable=self.command_var, style="Command.TEntry")
        self.command_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=10)

        self.command_entry.bind("<Return>", self._handle_command)
        self.command_entry.bind("<Up>", self._history_up)
        self.command_entry.bind("<Down>", self._history_down)

    def print_line(self, text: str, level: int = 2, override_verbosity: bool = False) -> None:
        if level > self.VERBOSITY and not override_verbosity:
            return

        if self._closing or not self._ui_is_available():
            return

        if threading.current_thread() is not threading.main_thread():
            self._ui_queue.put((text, level, override_verbosity))
            return

        self._append_line(text, level)

    def _ui_is_available(self) -> bool:
        try:
            return bool(self.root.winfo_exists()) and bool(self.history.winfo_exists())
        except tk.TclError:
            return False

    def _append_line(self, text: str, level: int) -> None:
        if self._closing or not self._ui_is_available():
            return

        self.history.configure(state="normal")
        if level == 0:
            self.history.insert("end", f"{text}\n", "error")
            self.history.tag_config("error", foreground=self.WARNING_COLOR)
        else:
            self.history.insert("end", f"{text}\n")
        self.history.see("end")
        self.history.configure(state="disabled")

    def _drain_ui_queue(self) -> None:
        if self._closing or not self._ui_is_available():
            return

        while True:
            try:
                text, level, override_verbosity = self._ui_queue.get_nowait()
            except queue.Empty:
                break

            self.print_line(text, level, override_verbosity)

        if not self._closing and self._ui_is_available():
            self.root.after(50, self._drain_ui_queue)

    def _clear_history(self, _event: tk.Event | None = None) -> str:
        if self._closing or not self._ui_is_available():
            return "break"

        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.configure(state="disabled")
        return "break"

    def _handle_command(self, _event: tk.Event | None = None) -> str:
        command_text = self.command_var.get().strip()
        self.command_var.set("")
        self.history_index = -1

        if not command_text:
            return "break"

        self.command_history.append(command_text)
        self.print_line(f"> {command_text}", 1, override_verbosity=True)

        command_name = command_text.partition(" ")[0].lower()
        if command_name == "clear":
            self._clear_history()
            self.print_line("History cleared.", 1)
            return "break"

        if command_name in {"quit", "exit"}:
            # Run the quit flow asynchronously so `_quit_SC2` executes before closing the UI
            threading.Thread(target=self._run_command_worker, args=(command_text,), daemon=True).start()
            return "break"

        threading.Thread(target=self._run_command_worker, args=(command_text,), daemon=True).start()

        return "break"

    def _run_command_worker(self, command_text: str) -> None:
        try:
            lines = self._run_async(self.dispatch_command(command_text))
        except (RuntimeError, TimeoutError, FileNotFoundError, ValueError, aiohttp.ClientError, OSError) as exc:
            self.print_line(f"Error: {exc}", 0)
            return

        for line in lines or []:
            if line.startswith("  "):   # Scuff override for help command
                self.print_line(line, 1, override_verbosity=True)
            else:
                self.print_line(line, 0 if line.startswith("Error:") else 1)

    def _history_up(self, _event: tk.Event | None = None) -> str:
        if not self.command_history:
            return "break"

        if self.history_index == -1:
            self.history_index = len(self.command_history) - 1
        elif self.history_index > 0:
            self.history_index -= 1
        else:
            return "break"

        self.command_var.set(self.command_history[self.history_index])
        self.command_entry.icursor("end")
        return "break"

    def _history_down(self, _event: tk.Event | None = None) -> str:
        if not self.command_history or self.history_index == -1:
            return "break"

        if self.history_index < len(self.command_history) - 1:
            self.history_index += 1
            self.command_var.set(self.command_history[self.history_index])
        else:
            self.history_index = -1
            self.command_var.set("")

        self.command_entry.icursor("end")
        return "break"

    async def dispatch_command(self, command_text: str):
        command_name, _, argument_text = command_text.partition(" ")
        normalized_name = command_name.lower()

        match normalized_name:
            case "help" | "?":
                self.print_line("  Command\t\tArguments\t\tDescription", 1, override_verbosity=True)
                help_rows = [
                    ("help", "", "Show this help text"),
                    ("clear", "", "Clear history"),
                    ("game_path", "<path>", "Set StarCraft II installation path (folder or StarCraft II.exe file)"),
                    ("banks_path", "<path>", "Set banks path (...\\Documents\\StarCraft II\\Accounts\\...\\...\\Banks)"),
                    ("neuro_url", "<URL>", "Set the websocket server url used to connect to Neuro"),
                    ("start_integration", "", "Start Neuro integration"),
                    ("stop_integration", "", "Stop Neuro integration"),
                    ("sc2_launch", "", "Launch StarCraft II and connect to API"),
                    ("sc2_quit", "", "Quit StarCraft II"),
                    ("sc2_disconnect", "", "Disconnect from StarCraft II API"),
                    ("sc2_reconnect", "", "Reconnect to StarCraft II API"),
                    ("quit, exit", "", "Close the terminal window"),
                    ("listening_port", "int", "Configure the port that StarCraft II should listen on for API connections (default: 5000)"),
                    ("verbosity", "0/1/2", "Set the verbosity of terminal output. 0 = warnings/errors, 1 = info, 2 = verbose/debug (default: 1)"),
                    ("protocol_help", "", "Show available protocol commands that can be sent to the SC2 API"),
                ]
                help_rows = sorted(help_rows, key=lambda x: x[0])
                return [f"  {command_name}\t\t{args}\t\t{desc}" for command_name, args, desc in help_rows]

            case "clear":
                self._clear_history()
                return ["History cleared."]

            case "quit" | "exit":
                if self.sc2api_process is not None:
                    await self._quit_SC2()
                if self.integration_running:
                    await self._stop_integration()
                self.close()
                return []

            case "verbosity":
                if not argument_text:
                    self.print_line(f"Current verbosity is at {self.VERBOSITY}", 1)
                    return ["Error: verbosity command requires an argument. Usage: verbosity <0/1/2>"]
                if argument_text not in {"0", "1", "2"}:
                    return ["Error: verbosity must be 0, 1, or 2"]
                self.VERBOSITY = int(argument_text)
                # Persist verbosity to configuration
                try:
                    self._save_configuration("verbosity", str(self.VERBOSITY))
                except Exception:
                    # Don't fail the command if saving fails; just warn
                    self.print_line("Warning: Could not save verbosity to configuration.", 0)
                return [f"Verbosity set to {self.VERBOSITY}"]
            
            case "listening_port":
                # Needs to check if argument is a valid port number and port is unused
                if not argument_text:
                    self.print_line(f"Current listening port is {self.sc2api_launch_arg_port[1]}", 1)
                    return ["Error: listening_port requires a port number argument. Usage: listening_port <port number>"]
                if not argument_text.isdigit() or int(argument_text) < 1024 or int(argument_text) > 65535:
                    return ["Error: Port number must be an integer between 1024 and 65535"]
                # Check if port is already in use
                server_running = await self._is_websocket_in_use()
                if server_running:
                    return [f"Error: Port {argument_text} is already in use. Please choose a different port or close the application using it."]
                self.sc2api_launch_arg_port[1] = argument_text
                return [f"Listening port set to {self.sc2api_launch_arg_port[1]}"]
            
            case "game_path":
                if not argument_text:
                    self.print_line(f"Current game path is {self.game_path}", 1)
                    return ["Error: game_path requires a path argument. Usage: game_path <path to StarCraft 2 folder or StarCraft II.exe>"]
                return self._set_game_path(argument_text)
            
            case "banks_path":
                if not argument_text:
                    self.print_line(f"Current banks path is {self.banks_path}", 1)
                    return ["Error: banks_path requires a path argument. Usage: banks_path <path to Banks folder in Documents\\StarCraft II\\Accounts>"]
                return self._set_banks_path(argument_text)

            case "neuro_url":
                if not argument_text:
                    if self.NEURO_URL is not None:
                        self.print_line(f"Current Neuro URL is {self.NEURO_URL}", 1)
                    else:
                        self.print_line("Neuro URL is not set.", 1)
                    return ["Error: neuro_url requires a URL argument. Usage: neuro_url <websocket server url>"]
                return self._set_neuro_url(argument_text)

            case "start_integration" | "start":
                return await self._start_integration()

            case "stop_integration":
                return await self._stop_integration()


            case "sc2_launch" | "launch":
                return await self._launch_SC2()
            
            case "sc2_quit":
                return await self._quit_SC2()
            
            case "sc2_reconnect":
                return await self._reconnect_SC2()
            
            case "sc2_disconnect":
                return await self._disconnect_SC2()
            
            case "protocol_help" | "protocol?" | "protocol_commands":
                return self._print_protocol_help()
            
        if self.game_launched:
            match normalized_name:
                case "ping":
                    try:
                        await self._prot().ping()
                        return ["Ping successful: Connection to SC2 API is healthy."]
                    except Exception as exc:
                        return [f"Ping failed: {exc}"]

        if argument_text:
            return [f"Unknown command: {command_name}", f"Arguments received: {argument_text}"]

        return [f"Unknown command: {command_text}"]

    def _print_protocol_help(self) -> list[str]:
        self.print_line("  Command\t\tDescription", 1, override_verbosity=True)
        protocol_rows = [
            ("ping", "", "Check connectivity"),
            ("protocol_help", "", "Show this help text"),
        ]
        protocol_rows = sorted(protocol_rows, key=lambda x: x[0])
        return [f"  {command_name}\t\t{args}\t\t{desc}" for command_name, args, desc in protocol_rows]
    
    def close(self, _event: tk.Event | None = None) -> str | None:
        self._closing = True

        # Stop integration runtime before stopping the event loop.
        if self.integration_running:
            try:
                self._run_async(self._stop_integration())
            except (RuntimeError, TimeoutError, OSError):
                pass

        # Stop the event loop before destroying the UI
        if self._event_loop is not None:
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        return None
    
    async def _is_websocket_in_use(self) -> bool:
        try:
            async with websockets.connect(f"ws://{self.sc2api_launch_arg_ip[1]}:{self.sc2api_launch_arg_port[1]}") as _websocket:
                return True
        except ConnectionRefusedError:
            return False
        except TimeoutError:
            return False
        except OSError:
            return False
        except (websockets.InvalidURI, websockets.InvalidHandshake, websockets.ConnectionClosedError) as exc:
            self.print_line(f"Unexpected error while checking websocket: {exc}", 0)
            return False

    def _set_game_path(self, path_str: str) -> list[str]:
        path = WindowsPath(path_str.strip()) 

        if not path.exists():
            return ["Error: Path not found"]

        if path.is_dir():
            exe_path = path / "StarCraft II.exe"
        elif path.name.lower() == "starcraft ii.exe":
            exe_path = path
        else:
            return ["Error: Path must be a folder containing StarCraft II.exe or the file itself"]

        if not exe_path.exists():
            return ["Error: StarCraft installation not found"]

        self.game_path = str(exe_path.parent)
        self._save_configuration("game_path", self.game_path)

        return [f"StarCraft II installation found at: {self.game_path}"]
    
    def _set_banks_path(self, path_str: str) -> list[str]:
        path = WindowsPath(path_str.strip())
        
        if not path.exists():
            return ["Error: Path not found"]
        
        if not path.is_dir():
            return ["Error: Path must be a directory"]
        
        # Path structure: ...\Documents\StarCraft II\Accounts\<numbers>\<alphanumeric+dashes>\Banks
        try:
            banks_dir = path
            account_name_dir = path.parent
            account_id_dir = path.parent.parent
            accounts_dir = path.parent.parent.parent
            sc2_dir = path.parent.parent.parent.parent

            if banks_dir.name != "Banks" or accounts_dir.name != "Accounts" or sc2_dir.name != "StarCraft II" or not account_id_dir.name.isdigit() or not re.match(r"^[a-zA-Z0-9\-]+$", account_name_dir.name):
                return ["Error: Invalid path structure. Expected ...\\Documents\\StarCraft II\\Accounts\\<id>\\<name>\\Banks"]
            
        except (IndexError, AttributeError):
            return ["Error: Invalid path structure. Expected ...\\Documents\\StarCraft II\\Accounts\\<id>\\<name>\\Banks"]
        
        self.banks_path = str(path)
        self._save_configuration("banks_path", self.banks_path)
        
        return [f"Banks path found at: {self.banks_path}"]

    def _load_configuration(self) -> None:
        if not self.config_file.exists():
            return

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

                game_path = config.get("game_path")
                if game_path:
                    if not self.is_windows:
                        path = Path(game_path)
                    else:
                        path = WindowsPath(game_path)
                    exe_path = path / "StarCraft II.exe"
                    if exe_path.exists():
                        self.game_path = game_path
                        self.print_line(f"StarCraft II installation found at: {self.game_path}", 1)
                    else:
                        self.print_line("Error: StarCraft installation path is invalid", 0)

                banks_path = config.get("banks_path")
                if banks_path:
                    if not self.is_windows:
                        path = Path(banks_path)
                    else:
                        path = WindowsPath(banks_path)
                    if path.exists() and path.is_dir():
                        self.banks_path = banks_path
                        self.print_line(f"Banks path found at: {self.banks_path}", 1)
                    else:
                        self.print_line("Error: Banks path is invalid", 0)

                neuro_url = config.get("neuro_url")
                if neuro_url:
                    parsed = urlparse(str(neuro_url).strip())
                    if parsed.scheme in {"ws", "wss"} and parsed.netloc:
                        self.NEURO_URL = str(neuro_url).strip()
                        self.print_line(f"Configured Neuro URL: {self.NEURO_URL}", 1)
                    else:
                        self.print_line("Error: Configured Neuro URL is invalid", 0)

                verbosity_cfg = config.get("verbosity")
                if verbosity_cfg is not None:
                    try:
                        v = int(verbosity_cfg)
                        if v in (0, 1, 2):
                            self.VERBOSITY = v
                        else:
                            self.print_line("Error: verbosity in configuration invalid; expected 0/1/2", 0)
                    except (ValueError, TypeError):
                        self.print_line("Error: verbosity in configuration invalid", 0)
        except (json.JSONDecodeError, IOError):
            self.print_line("Error: Could not read configure.json", 0)

    def _save_configuration(self, configuration_name: str, configuration_data: str | None) -> None:
        config = {}
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except (json.JSONDecodeError, IOError):
                config = {}

        if configuration_data is not None:
            config[configuration_name] = configuration_data

        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    async def _launch_SC2(self) -> list[str]:
        if not self.game_path:
            return ["Error: game_path is not set. Use game_path <path> first."]
    
        await self.connection_handler.launch_game()
        await self.connection_handler.connect()
        await self.connection_handler.test_connection()
        self.game_launched = True

        return ["Connection to SC2 API established."]
    
    async def _quit_SC2(self) -> list[str]:
        return await self.connection_handler.quit_game()
    
    async def _reconnect_SC2(self) -> list[str]:
        await self.connection_handler.disconnect()
        await self.connection_handler.connect()
        await self.connection_handler.test_connection()

        return ["Reconnected to SC2 API."]
    
    async def _disconnect_SC2(self) -> list[str]:
        return await self.connection_handler.disconnect()
    
    def _prot(self) -> protocol.Protocol:
        return protocol.Protocol(self)
    



def main() -> None:
    root = tk.Tk()
    TerminalApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

