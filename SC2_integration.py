"""Entry point for the SC2 integration shell."""

import json
import math
import os
import re
import websockets
import asyncio
import subprocess
import threading
import queue
import tkinter as tk
from pathlib import Path, WindowsPath
from typing import Callable
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
    VERBOSITY = 1  # 0 = warning/error, 1 = info, 2 = verbose/debug, 3 = everything
    
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
        self.transcript_db_path: str | None = None
        self.neuro_url: str | None = None
        self._ui_queue: queue.Queue[tuple[str, int, bool]] = queue.Queue()
        self._ui_callback_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._closing: bool = False
        self.ocr_regions: list[tuple[float, float, float, float]] | None = None
        self._ocr_overlay: tk.Toplevel | None = None
        self._ocr_canvas: tk.Canvas | None = None
        self._ocr_edit_regions: list[list[int]] | None = None
        self._ocr_edit_screen_size: tuple[int, int] | None = None
        self._ocr_drag_state: tuple[int, str, int, int, tuple[int, int, int, int]] | None = None
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

        while True:
            try:
                callback = self._ui_callback_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except (tk.TclError, OSError, ValueError, TypeError) as exc:
                self.print_line(f"Error: OCR region configuration failed: {exc}", 0)

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
                    ("transcript_db_path", "<path>", "Set the path to the Handy speech transcript database (voice context)"),
                    ("neuro_url", "<URL>", "Set the websocket server url used to connect to Neuro"),
                    ("configure_ocr", "", "Place two screen regions for OCR"),
                    ("start_integration", "", "Start Neuro integration"),
                    ("stop_integration", "", "Stop Neuro integration"),
                    ("sc2_launch", "", "Launch StarCraft II and connect to API"),
                    ("sc2_quit", "", "Quit StarCraft II"),
                    ("sc2_disconnect", "", "Disconnect from StarCraft II API"),
                    ("sc2_reconnect", "", "Reconnect to StarCraft II API"),
                    ("quit, exit", "", "Close the terminal window"),
                    ("listening_port", "int", "Configure the port that StarCraft II should listen on for API connections (default: 5000)"),
                    ("verbosity", "0/1/2/3", "Set the verbosity of terminal output. 0 = warnings/errors, 1 = info, 2 = verbose/debug, 3 = everything (default: 1)"),
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
                    return ["Error: verbosity command requires an argument. Usage: verbosity <0/1/2/3>"]
                if argument_text not in {"0", "1", "2", "3"}:
                    return ["Error: verbosity must be 0, 1, 2, or 3"]
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
                    if self.game_path is not None:
                        self.print_line(f"Current game path is {self.game_path}", 1)
                    return ["Error: game_path requires a path argument. Usage: game_path <path to StarCraft 2 folder or StarCraft II.exe>"]
                return self._set_game_path(argument_text)
            
            case "banks_path":
                if not argument_text:
                    if self.banks_path is not None:
                        self.print_line(f"Current banks path is {self.banks_path}", 1)
                    return ["Error: banks_path requires a path argument. Usage: banks_path <path to Banks folder in Documents\\StarCraft II\\Accounts>"]
                return self._set_banks_path(argument_text)

            case "transcript_db_path":
                if not argument_text:
                    if self.transcript_db_path is not None:
                        self.print_line(f"Current Handy transcript database path is {self.transcript_db_path}", 1)
                    return ["Error: transcript_db_path requires a path argument. Usage: transcript_db_path <path to Handy transcript database>"]
                return self._set_transcript_db_path(argument_text)

            case "neuro_url":
                if not argument_text:
                    if self.neuro_url is not None:
                        self.print_line(f"Current Neuro URL is {self.neuro_url}", 1)
                    else:
                        self.print_line("Neuro URL is not set.", 1)
                    return ["Error: neuro_url requires a URL argument. Usage: neuro_url <websocket server url>"]
                return self._set_neuro_url(argument_text)

            case "configure_ocr":
                if not self.is_windows:
                    return ["Error: OCR region configuration is currently supported only on Windows."]
                self._ui_callback_queue.put(self._show_ocr_overlay)
                return ["Opening OCR region configuration."]

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

    def _show_ocr_overlay(self) -> None:
        if self._closing:
            return
        if self._ocr_overlay is not None:
            self._ocr_overlay.deiconify()
            self._ocr_overlay.lift()
            self._ocr_overlay.focus_force()
            return

        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        if self._ocr_edit_regions is None or self._ocr_edit_screen_size is None:
            if self.ocr_regions is None:
                self._ocr_edit_regions = [
                    [int(screen_width * 0.12), int(screen_height * 0.2), int(screen_width * 0.35), int(screen_height * 0.14)],
                    [int(screen_width * 0.53), int(screen_height * 0.2), int(screen_width * 0.35), int(screen_height * 0.14)],
                ]
            else:
                self._ocr_edit_regions = [
                    [
                        round(x * screen_width),
                        round(y * screen_height),
                        round(width * screen_width),
                        round(height * screen_height),
                    ]
                    for x, y, width, height in self.ocr_regions
                ]
            self._ocr_edit_screen_size = (screen_width, screen_height)

        transparent_color = "#010203"
        overlay = tk.Toplevel(self.root)
        try:
            overlay.overrideredirect(True)
            overlay.geometry(f"{screen_width}x{screen_height}+0+0")
            overlay.configure(bg=transparent_color)
            overlay.attributes("-topmost", True)
            overlay.attributes("-transparentcolor", transparent_color)
            overlay.bind("<Escape>", self._cancel_ocr_configuration)

            canvas = tk.Canvas(
                overlay,
                bg=transparent_color,
                highlightthickness=0,
                borderwidth=0,
            )
            canvas.pack(fill="both", expand=True)
            canvas.bind("<ButtonPress-1>", self._begin_ocr_region_drag)
            canvas.bind("<B1-Motion>", self._drag_ocr_region)
            canvas.bind("<ButtonRelease-1>", self._end_ocr_region_drag)
            canvas.bind("<Motion>", self._update_ocr_cursor)

            controls = tk.Frame(overlay, bg="#171b21", padx=12, pady=8)
            tk.Label(
                controls,
                text="Drag each colored label to move its rectangle; drag an edge or corner to resize. Press Esc to cancel.",
                bg="#171b21",
                fg="#e7ebf0",
                font=("Segoe UI", 10),
            ).pack(side="left", padx=(0, 12))
            tk.Button(
                controls,
                text="Save OCR configuration",
                command=self._save_ocr_regions,
            ).pack(side="left")
            canvas.create_window(12, 12, anchor="nw", window=controls)
        except tk.TclError as exc:
            overlay.destroy()
            self._ocr_edit_regions = None
            self._ocr_edit_screen_size = None
            self.print_line(f"Error: Could not create the OCR overlay: {exc}", 0)
            return

        self._ocr_overlay = overlay
        self._ocr_canvas = canvas
        self._draw_ocr_regions()
        overlay.focus_force()

    def _draw_ocr_regions(self) -> None:
        if self._ocr_canvas is None or self._ocr_edit_regions is None:
            return

        canvas = self._ocr_canvas
        canvas.delete("ocr-region")
        colors = ("#00ffff", "#ff55ff")
        for index, (x, y, width, height) in enumerate(self._ocr_edit_regions):
            color = colors[index]
            canvas.create_rectangle(
                x,
                y,
                x + width,
                y + height,
                outline=color,
                width=3,
                tags="ocr-region",
            )
            label_right = min(x + width - 4, x + 260)
            canvas.create_rectangle(
                x + 4,
                y + 4,
                label_right,
                y + 30,
                fill=color,
                outline=color,
                tags="ocr-region",
            )
            canvas.create_text(
                x + 10,
                y + 6,
                anchor="nw",
                text=f"OCR region {index + 1} - drag to move",
                fill="#0f1115",
                font=("Segoe UI", 11, "bold"),
                tags="ocr-region",
            )
            handle_size = 5
            for handle_x in (x, x + width):
                for handle_y in (y, y + height):
                    canvas.create_rectangle(
                        handle_x - handle_size,
                        handle_y - handle_size,
                        handle_x + handle_size,
                        handle_y + handle_size,
                        fill=color,
                        outline=color,
                        tags="ocr-region",
                    )

    def _ocr_region_hit(self, x: int, y: int) -> tuple[int, str] | None:
        if self._ocr_edit_regions is None:
            return None

        edge_margin = 12
        for index in reversed(range(len(self._ocr_edit_regions))):
            left, top, width, height = self._ocr_edit_regions[index]
            right, bottom = left + width, top + height
            if not (left <= x <= right and top <= y <= bottom):
                continue

            if left + 4 <= x <= min(left + width - 4, left + 260) and top + 4 <= y <= top + 30:
                return index, "move"

            horizontal = "w" if x - left <= edge_margin else "e" if right - x <= edge_margin else ""
            vertical = "n" if y - top <= edge_margin else "s" if bottom - y <= edge_margin else ""
            if horizontal or vertical:
                return index, vertical + horizontal if vertical else horizontal
            return index, "move"
        return None

    def _begin_ocr_region_drag(self, event: tk.Event) -> None:
        hit = self._ocr_region_hit(event.x, event.y)
        if hit is None or self._ocr_edit_regions is None:
            self._ocr_drag_state = None
            return

        index, mode = hit
        self._ocr_drag_state = (index, mode, event.x, event.y, tuple(self._ocr_edit_regions[index]))

    def _drag_ocr_region(self, event: tk.Event) -> None:
        if self._ocr_drag_state is None or self._ocr_edit_regions is None or self._ocr_edit_screen_size is None:
            return

        index, mode, start_x, start_y, original = self._ocr_drag_state
        x, y, width, height = original
        screen_width, screen_height = self._ocr_edit_screen_size
        dx, dy = event.x - start_x, event.y - start_y
        min_size = 40

        if mode == "move":
            x = min(max(0, x + dx), max(0, screen_width - width))
            y = min(max(0, y + dy), max(0, screen_height - height))
        else:
            right, bottom = x + width, y + height
            if "w" in mode:
                x = min(max(0, x + dx), right - min_size)
                width = right - x
            if "e" in mode:
                width = min(max(min_size, width + dx), screen_width - x)
            if "n" in mode:
                y = min(max(0, y + dy), bottom - min_size)
                height = bottom - y
            if "s" in mode:
                height = min(max(min_size, height + dy), screen_height - y)

        self._ocr_edit_regions[index] = [x, y, width, height]
        self._draw_ocr_regions()

    def _end_ocr_region_drag(self, _event: tk.Event) -> None:
        self._ocr_drag_state = None

    def _update_ocr_cursor(self, event: tk.Event) -> None:
        if self._ocr_canvas is None:
            return
        hit = self._ocr_region_hit(event.x, event.y)
        cursor = "arrow" if hit is None else "fleur" if hit[1] == "move" else "sizing"
        self._ocr_canvas.configure(cursor=cursor)

    def _cancel_ocr_configuration(self, _event: tk.Event | None = None) -> str:
        self._destroy_ocr_overlay()
        self.print_line("OCR region configuration cancelled; no changes were saved.", 1)
        return "break"

    def _save_ocr_regions(self) -> None:
        if self._ocr_edit_regions is None or self._ocr_edit_screen_size is None:
            self.print_line("Error: No OCR configuration is active. Use 'configure_ocr' first.", 0)
            return

        screen_width, screen_height = self._ocr_edit_screen_size
        regions = [
            (x / screen_width, y / screen_height, width / screen_width, height / screen_height)
            for x, y, width, height in self._ocr_edit_regions
        ]
        config_data = {
            "coordinate_space": "normalized",
            "regions": [
                {"x": x, "y": y, "width": width, "height": height}
                for x, y, width, height in regions
            ],
        }
        try:
            self._save_configuration("ocr_regions", config_data)
        except (OSError, TypeError, ValueError) as exc:
            self.print_line(f"Error: Could not save OCR regions: {exc}", 0)
            return

        self.ocr_regions = regions
        self._destroy_ocr_overlay()
        self.print_line("OCR regions saved and overlay closed.", 1)

    def _destroy_ocr_overlay(self) -> None:
        if self._ocr_overlay is not None:
            try:
                self._ocr_overlay.destroy()
            except tk.TclError:
                pass
        self._ocr_overlay = None
        self._ocr_canvas = None
        self._ocr_edit_regions = None
        self._ocr_edit_screen_size = None
        self._ocr_drag_state = None
    
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

    def _set_transcript_db_path(self, path_str: str) -> list[str]:
        path = WindowsPath(path_str.strip())
        
        self.transcript_db_path = str(path)
        self._save_configuration("transcript_db_path", self.transcript_db_path)
        
        return [f"Handy transcript database path set to: {self.transcript_db_path}"]

    def _set_neuro_url(self, url_str: str) -> list[str]:
        url = url_str.strip()
        parsed = urlparse(url)

        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            return ["Error: neuro_url must be a valid websocket URL"]

        self.neuro_url = url
        self._save_configuration("neuro_url", self.neuro_url)

        return [f"Neuro URL set to: {self.neuro_url}"]

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

                transcript_db_path = config.get("transcript_db_path")
                if transcript_db_path:
                    if not self.is_windows:
                        path = Path(transcript_db_path)
                    else:
                        path = WindowsPath(transcript_db_path)
                    self.transcript_db_path = path
                    self.print_line(f"Handy transcript database path set to: {self.transcript_db_path}", 1)

                neuro_url = config.get("neuro_url")
                if neuro_url:
                    parsed = urlparse(str(neuro_url).strip())
                    if parsed.scheme in {"ws", "wss"} and parsed.netloc:
                        self.neuro_url = str(neuro_url).strip()
                        self.print_line(f"Configured Neuro URL: {self.neuro_url}", 1)
                    else:
                        self.print_line("Error: Configured Neuro URL is invalid", 0)

                verbosity_cfg = config.get("verbosity")
                if verbosity_cfg is not None:
                    try:
                        v = int(verbosity_cfg)
                        if v in (0, 1, 2, 3):
                            self.VERBOSITY = v
                        else:
                            self.print_line("Error: verbosity in configuration invalid; expected 0/1/2", 0)
                    except (ValueError, TypeError):
                        self.print_line("Error: verbosity in configuration invalid", 0)

                ocr_regions_cfg = config.get("ocr_regions")
                if ocr_regions_cfg is not None:
                    try:
                        self.ocr_regions = self._parse_ocr_regions(ocr_regions_cfg)
                        self.print_line("Configured OCR screen regions loaded.", 1)
                    except ValueError as exc:
                        self.print_line(f"Error: Configured OCR regions are invalid: {exc}", 0)
        except (json.JSONDecodeError, IOError):
            self.print_line("Error: Could not read configure.json", 0)

    @staticmethod
    def _parse_ocr_regions(configuration_data: object) -> list[tuple[float, float, float, float]]:
        if not isinstance(configuration_data, dict) or configuration_data.get("coordinate_space") != "normalized":
            raise ValueError("expected normalized screen coordinates")

        configured_regions = configuration_data.get("regions")
        if not isinstance(configured_regions, list) or len(configured_regions) != 2:
            raise ValueError("exactly two regions are required")

        parsed_regions: list[tuple[float, float, float, float]] = []
        for region in configured_regions:
            if not isinstance(region, dict):
                raise ValueError("each region must contain x, y, width, and height")
            try:
                x, y, width, height = (
                    float(region[key])
                    for key in ("x", "y", "width", "height")
                )
            except (KeyError, OverflowError, TypeError, ValueError) as exc:
                raise ValueError("each region must contain numeric coordinates") from exc

            if not all(math.isfinite(value) for value in (x, y, width, height)):
                raise ValueError("coordinates must be finite numbers")
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                raise ValueError("regions must fit within the screen")
            parsed_regions.append((x, y, width, height))

        return parsed_regions

    def _save_configuration(self, configuration_name: str, configuration_data: object | None) -> None:
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
