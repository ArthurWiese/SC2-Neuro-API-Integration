from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import overload, TYPE_CHECKING

from aiohttp.client_ws import ClientWebSocketResponse

# pyre-fixme[21]
from s2clientprotocol import sc2api_pb2 as sc_pb
from s2clientprotocol.query_pb2 import RequestQuery
from data import Status
if TYPE_CHECKING:
    from SC2_integration import TerminalApp


class ProtocolError(Exception):
    @property
    def is_game_over_error(self) -> bool:
        return self.args[0] in ["['Game has already ended']", "['Not supported if game has already ended']"]


class ConnectionAlreadyClosedError(ProtocolError):
    pass


class Protocol:
    def __init__(self, integration: TerminalApp) -> None:
        """
        A class for communicating with an SCII application.
        :param integration: the terminal application instance
        """
        self._integration: TerminalApp = integration
        self._status: Status | None = None

    async def __request(self, request: sc_pb.Request) -> sc_pb.Response:
        if self._integration.sc2api_ws is None:
            self._integration.print_line("Cannot send: Connection is not established.", 0)
            raise ConnectionAlreadyClosedError("Connection is not established.")
        
        if getattr(self._integration.sc2api_ws, "closed", False):
            self._integration.print_line("Cannot send: Connection is closed.", 0)
            raise ConnectionAlreadyClosedError("Connection is closed.")
        
        self._integration.print_line(f"Sending request: {request!r}", 2)
        try:
            await self._integration.sc2api_ws.send_bytes(request.SerializeToString())
        except (TypeError, AttributeError) as exc:
            self._integration.print_line(f"Cannot send: Connection already closed or invalid. {exc}", 0)
            raise ConnectionAlreadyClosedError(f"Connection already closed or invalid: {exc}") from exc
        self._integration.print_line("Request sent", 2)

        response = sc_pb.Response()
        try:
            response_bytes = await self._integration.sc2api_ws.receive_bytes()
        except TypeError as exc:
            if self._status == Status.ended:
                self._integration.print_line("Cannot receive: Game has already ended.", 0)
                raise ConnectionAlreadyClosedError("Game has already ended") from exc
            self._integration.print_line("Cannot receive: Connection already closed.", 0)
            raise ConnectionAlreadyClosedError("Connection already closed.") from exc
        except asyncio.CancelledError:
            # If request is sent, the response must be received before reraising cancel
            try:
                await self._integration.sc2api_ws.receive_bytes()
            except asyncio.CancelledError:
                self._integration.print_line("Requests must not be cancelled multiple times", 0)
                sys.exit(2)
            raise

        response.ParseFromString(response_bytes)
        self._integration.print_line("Response received: " + str(response), 2)
        return response

    @overload
    async def _execute(self, create_game: sc_pb.RequestCreateGame) -> sc_pb.Response: ...
    @overload
    async def _execute(self, join_game: sc_pb.RequestJoinGame) -> sc_pb.Response: ...
    @overload
    async def _execute(self, restart_game: sc_pb.RequestRestartGame) -> sc_pb.Response: ...
    @overload
    async def _execute(self, start_replay: sc_pb.RequestStartReplay) -> sc_pb.Response: ...
    @overload
    async def _execute(self, leave_game: sc_pb.RequestLeaveGame) -> sc_pb.Response: ...
    @overload
    async def _execute(self, quick_save: sc_pb.RequestQuickSave) -> sc_pb.Response: ...
    @overload
    async def _execute(self, quick_load: sc_pb.RequestQuickLoad) -> sc_pb.Response: ...
    @overload
    async def _execute(self, quit: sc_pb.RequestQuit) -> sc_pb.Response: ...
    @overload
    async def _execute(self, game_info: sc_pb.RequestGameInfo) -> sc_pb.Response: ...
    @overload
    async def _execute(self, action: sc_pb.RequestAction) -> sc_pb.Response: ...
    @overload
    async def _execute(self, observation: sc_pb.RequestObservation) -> sc_pb.Response: ...
    @overload
    async def _execute(self, obs_action: sc_pb.RequestObserverAction) -> sc_pb.Response: ...
    @overload
    async def _execute(self, step: sc_pb.RequestStep) -> sc_pb.Response: ...
    @overload
    async def _execute(self, data: sc_pb.RequestData) -> sc_pb.Response: ...
    @overload
    async def _execute(self, query: RequestQuery) -> sc_pb.Response: ...
    @overload
    async def _execute(self, save_replay: sc_pb.RequestSaveReplay) -> sc_pb.Response: ...
    @overload
    async def _execute(self, map_command: sc_pb.RequestMapCommand) -> sc_pb.Response: ...
    @overload
    async def _execute(self, replay_info: sc_pb.RequestReplayInfo) -> sc_pb.Response: ...
    @overload
    async def _execute(self, available_maps: sc_pb.RequestAvailableMaps) -> sc_pb.Response: ...
    @overload
    async def _execute(self, save_map: sc_pb.RequestSaveMap) -> sc_pb.Response: ...
    @overload
    async def _execute(self, ping: sc_pb.RequestPing) -> sc_pb.Response: ...
    @overload
    async def _execute(self, debug: sc_pb.RequestDebug) -> sc_pb.Response: ...
    async def _execute(self, **kwargs) -> sc_pb.Response:
        if len(kwargs) != 1:
            self._integration.print_line("Only one request allowed by the API", 0)
            assert False, "Only one request allowed by the API"

        response: sc_pb.Response = await self.__request(sc_pb.Request(**kwargs))

        new_status = Status(response.status)
        if new_status != self._status:
            self._integration.print_line(f"Client status changed to {new_status} (was {self._status})", 2)
        self._status = new_status

        if response.error:
            self._integration.print_line(f"Response contained an error: {response.error}", 0)
            raise ProtocolError(f"{response.error}")

        return response

    async def ping(self):
        result = await self._execute(ping=sc_pb.RequestPing())
        return result

    async def quit(self) -> None:
        with suppress(ConnectionAlreadyClosedError, ConnectionResetError):
            await self._execute(quit=sc_pb.RequestQuit())
