"""Base/builtin provider with support for players using slimproto."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from aioslimproto.client import SlimClient
from aioslimproto.const import EventType as SlimEventType
from aioslimproto.discovery import start_discovery

from music_assistant.common.models.enums import PlayerFeature, PlayerState, PlayerType
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.server.models.player_provider import PlayerProvider

# TODO: Implement display support


class SlimprotoProvider(PlayerProvider):
    """Base/builtin provider for players using the SLIM protocol (aka slimproto)."""

    _socket_servers: tuple[asyncio.Server | asyncio.BaseTransport] | None = None
    _socket_clients: dict[str, SlimClient] | None = None

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        self._socket_clients = {}
        # autodiscovery of the slimproto server does not work
        # when the port is not the default (3483) so we hardcode it for now
        slimproto_port = 3483
        self.logger.info("Starting SLIMProto server on port %s", slimproto_port)
        self._socket_servers = (
            # start slimproto server
            await asyncio.start_server(self._create_client, "0.0.0.0", slimproto_port),
            # setup discovery
            await start_discovery(slimproto_port, None, self.mass.port),
        )

    async def close(self) -> None:
        """Handle close/cleanup of the provider."""
        if self._socket_clients is not None:
            for client in list(self._socket_clients.values()):
                client.disconnect()
        self._socket_clients = {}
        if self._socket_servers is not None:
            for _server in self._socket_servers:
                _server.close()
            self._socket_servers = None

    async def _create_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Create player from new connection on the socket."""
        addr = writer.get_extra_info("peername")
        self.logger.debug("Socket client connected: %s", addr)

        def client_callback(
            event_type: SlimEventType, client: SlimClient, data: Any = None
        ):
            player_id = client.player_id

            # handle player disconnect
            if event_type == SlimEventType.PLAYER_DISCONNECTED:
                prev = self._socket_clients.pop(player_id, None)
                if prev is None:
                    # already cleaned up
                    return
                if player := self.mass.players.get(player_id):
                    player.available = False
                    self.mass.players.update(player_id)
                return

            # handle player (re)connect
            if event_type == SlimEventType.PLAYER_CONNECTED:
                prev = self._socket_clients.pop(player_id, None)
                if prev is not None:
                    # player reconnected while we did not yet cleanup the old socket
                    prev.disconnect()
                self._socket_clients[player_id] = client

            player = self.mass.players.get(player_id)
            if not player:
                player = Player(
                    player_id=player_id,
                    provider=self.domain,
                    type=PlayerType.PLAYER,
                    name=client.name,
                    available=True,
                    powered=client.powered,
                    device_info=DeviceInfo(
                        model=client.device_model,
                        address=client.device_address,
                        manufacturer=client.device_type,
                    ),
                    supported_features=(
                        PlayerFeature.ACCURATE_TIME,
                        PlayerFeature.POWER,
                        PlayerFeature.SYNC,
                        PlayerFeature.VOLUME_MUTE,
                        PlayerFeature.VOLUME_SET,
                    ),
                )
                self.mass.players.register(player)

            # update player state on player events
            player.available = True
            player.current_url = client.current_url
            player.elapsed_time = client.elapsed_seconds
            player.elapsed_time_last_updated = time.time()
            player.name = client.name
            player.powered = client.powered
            player.state = PlayerState(client.state.value)
            player.volume_level = client.volume_level
            player.volume_muted = client.muted
            self.mass.players.update(player_id)

        # construct SlimClient from socket client
        SlimClient(reader, writer, client_callback)

    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.
            - player_id: player_id of the player to handle the command.
        """
        if client := self._socket_clients.get(player_id):
            await client.stop()

    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.
            - player_id: player_id of the player to handle the command.
        """
        if client := self._socket_clients.get(player_id):
            await client.play()

    async def cmd_play_url(self, player_id: str, url: str) -> None:
        """
        Send PLAY MEDIA command to given player.
            - player_id: player_id of the player to handle the command.
            - url: the url to start playing on the player.
        """
        if client := self._socket_clients.get(player_id):
            await client.play_url(url)

    async def cmd_pause(self, player_id: str) -> None:
        """
        Send PAUSE command to given player.
            - player_id: player_id of the player to handle the command.
        """
        if client := self._socket_clients.get(player_id):
            await client.pause()

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """
        Send POWER command to given player.
            - player_id: player_id of the player to handle the command.
            - powered: bool if player should be powered on or off.
        """
        if client := self._socket_clients.get(player_id):
            await client.power(powered)
        # TODO: unsync client at poweroff if synced

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send VOLUME_SET command to given player.
            - player_id: player_id of the player to handle the command.
            - volume_level: volume level (0..100) to set on the player.
        """
        if client := self._socket_clients.get(player_id):
            await client.volume_set(volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """
        Send VOLUME MUTE command to given player.
            - player_id: player_id of the player to handle the command.
            - muted: bool if player should be muted.
        """
        if client := self._socket_clients.get(player_id):
            await client.mute(muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """
        Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        # will only be called for players with SYNC feature set.
        raise NotImplementedError()

    async def cmd_unsync(self, player_id: str) -> None:
        """
        Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        # will only be called for players with SYNC feature set.
        raise NotImplementedError()