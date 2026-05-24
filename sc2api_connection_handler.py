import asyncio
import subprocess
import threading
from pathlib import Path, WindowsPath

import aiohttp

import protocol


class SC2ConnectionHandler:
    def __init__(self, integration):
        self.integration = integration
        self._stop_event = threading.Event()

    async def __aenter__(self):
        self._stop_event.clear()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.disconnect()

    def _resolve_game_root(self) -> Path | WindowsPath:
        game_root_text = self.integration.game_path

        try:
            # Windows-only: stored paths are Windows paths
            return WindowsPath(game_root_text)
        except OSError as exc:
            raise RuntimeError(f"Could not resolve stored game_path: {exc}") from exc

    async def launch_game(self):
        game_root = self._resolve_game_root()
        exe_path = game_root / "Support64" / "SC2Switcher_x64.exe"

        if not exe_path.exists():
            self.integration.print_line(f"Error: SC2 switcher not found at: {exe_path}", 0)
            raise FileNotFoundError(f"SC2 switcher not found at: {exe_path}")

        self._stop_event.clear()

        # Windows native launch
        self.integration.sc2api_process = subprocess.Popen(
            [str(exe_path), *self.integration.sc2api_launch_arg_ip, *self.integration.sc2api_launch_arg_port],
            cwd=str(exe_path.parent),
        )

        return [
            f"StarCraft II launch requested. Will listen for API connections at {self.integration.sc2api_launch_arg_ip[1]}:{self.integration.sc2api_launch_arg_port[1]}"
        ]

    async def connect(self):
        if self.integration.sc2api_process is None:
            raise RuntimeError("Game process is not running. Please launch the game first.")
        
        ws_url = f"ws://{self.integration.sc2api_launch_arg_ip[1]}:{self.integration.sc2api_launch_arg_port[1]}/sc2api"
        # Clear the stop event so we can actually attempt to connect (important for reconnects)
        self._stop_event.clear()

        # Give SC2 time to boot and open the listening port before attempting any connections.
        self.integration.print_line(f"Waiting to connect to SC2 API at {ws_url}...", 1)
        await asyncio.sleep(5)

        host = self.integration.sc2api_launch_arg_ip[1]
        port = int(self.integration.sc2api_launch_arg_port[1])

        for attempt in range(3):
            if self._stop_event.is_set() or self.integration.sc2api_process is None:
                return ["Connection attempt cancelled."]

            # TCP preflight check to verify the port is reachable before attempting websocket upgrade.
            if not await self._tcp_port_check(host, port):
                self.integration.print_line(f"Port {host}:{port} not yet reachable; retrying...", 2)
                await asyncio.sleep(3)
                continue

            if self._stop_event.is_set() or self.integration.sc2api_process is None:
                return ["Connection attempt cancelled."]

            try:
                if self.integration.sc2api_session is None or self.integration.sc2api_session.closed:
                    # Set explicit timeout on the session so ws_connect() cannot hang indefinitely.
                    timeout = aiohttp.ClientTimeout(total=10)
                    self.integration.sc2api_session = aiohttp.ClientSession(timeout=timeout)
                self.integration.print_line("Attempting to connect to SC2 API...", 1)
                self.integration.sc2api_ws = await self.integration.sc2api_session.ws_connect(ws_url)
                return ["Connection to SC2 API established."]
            except asyncio.TimeoutError:
                await self._cleanup_session()
                self.integration.print_line("Websocket connection timed out; retrying...", 2)
                await asyncio.sleep(3)
            except aiohttp.ClientError as exc:
                self.integration.print_line(f"Error occurred while connecting to SC2 API: {exc}", 0)
                await self._cleanup_session()
                if attempt == 2:
                    self.integration.print_line("SC2 API connection timed out.", 0)
                    raise TimeoutError("SC2 API connection timed out.") from exc
                self.integration.print_line("Connection refused? Still waiting ...", 2)
                await asyncio.sleep(3)

        self.integration.print_line("SC2 API connection timed out.", 0)
        raise TimeoutError("SC2 API connection timed out.")

    async def test_connection(self):
        # Ensure we actually have an open websocket before attempting protocol ping.
        if self.integration.sc2api_ws is None or getattr(self.integration.sc2api_ws, "closed", False):
            raise RuntimeError("Not connected to SC2 API. Test aborted.")
        
        prot = protocol.Protocol(self.integration)
        await prot.ping()
        return ["Connection successfully tested"]

    async def _tcp_port_check(self, host: str, port: int) -> bool:
        """Check if a port is reachable via TCP. Returns True if port is open, False otherwise."""
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=2.0
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (asyncio.TimeoutError, OSError, ConnectionRefusedError):
            return False

    async def disconnect(self):
        self._stop_event.set()
        self.integration.print_line(f"Closing connection to SC2 API at {self.integration.sc2api_launch_arg_ip[1]}:{self.integration.sc2api_launch_arg_port[1]}/sc2api",1,)

        if self.integration.sc2api_ws is not None:
            await self.integration.sc2api_ws.close()
            self.integration.sc2api_ws = None

        await self._cleanup_session()
        return ["Disconnected from SC2 API."]

    async def quit_game(self):
        if not self.integration.sc2api_process and not self.integration.game_launched:
            return ["SC2 with SC2API listener not running."]
        await self.disconnect()
        self.integration.print_line("Quitting game and cleaning up connection.", 2)
        self.integration.game_launched = False

        # Simply kill the SC2_x64.exe process directly
        try:
            subprocess.run(["taskkill", "/IM", "SC2_x64.exe", "/F"], capture_output=True, timeout=5, check=True)
            self.integration.print_line("StarCraft II process terminated successfully.", 2)
        except subprocess.TimeoutExpired:
            self.integration.print_line("Warning: timeout while terminating SC2_x64.exe", 0)
        except Exception as e:
            self.integration.print_line(f"Warning: could not terminate SC2_x64.exe: {e}", 0)

        self.integration.sc2api_process = None
        self.integration.print_line("Cleanup complete", 1)
        return ["Sent quit command to SC2."]

    async def _cleanup_session(self):
        if self.integration.sc2api_session is not None:
            await self.integration.sc2api_session.close()
            self.integration.sc2api_session = None
