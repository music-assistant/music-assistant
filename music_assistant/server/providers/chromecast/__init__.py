"""Chromecast Player provider for Music Assistant, utilizing the pychromecast library."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from logging import Logger
from typing import TYPE_CHECKING
from uuid import UUID

from pychromecast import (
    APP_BUBBLEUPNP,
    APP_MEDIA_RECEIVER,
    Chromecast,
    get_chromecast_from_cast_info,
)
from pychromecast.controllers.media import STREAM_TYPE_BUFFERED, STREAM_TYPE_LIVE
from pychromecast.controllers.multizone import MultizoneController, MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED

from music_assistant.common.models.enums import (
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import PlayerUnavailableError, QueueEmpty
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_PLAYERS, MASS_LOGO_ONLINE
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.models.player_provider import PlayerProvider
from music_assistant.server.providers.chromecast.helpers import CastStatusListener, ChromecastInfo

if TYPE_CHECKING:
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.socket_client import ConnectionStatus

    from music_assistant.common.models.config_entries import PlayerConfig


PLAYER_CONFIG_ENTRIES = tuple()


@dataclass
class CastPlayer:
    """Wrapper around Chromecast with some additional attributes."""

    player_id: str
    cast_info: ChromecastInfo
    cc: Chromecast
    player: Player
    logger: Logger
    is_stereo_pair: bool = False
    status_listener: CastStatusListener | None = None
    mz_controller: MultizoneController | None = None
    next_item: str | None = None
    flow_mode_active: bool = False


class ChromecastProvider(PlayerProvider):
    """Player provider for Chromecast based players."""

    mz_mgr: MultizoneManager | None = None
    browser: CastBrowser | None = None
    castplayers: dict[str, CastPlayer]

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        self.castplayers = {}
        # silence the cast logger a bit
        logging.getLogger("pychromecast.socket_client").setLevel(logging.INFO)
        logging.getLogger("pychromecast.controllers").setLevel(logging.INFO)
        self.mz_mgr = MultizoneManager()
        self.browser = CastBrowser(
            SimpleCastListener(
                add_callback=self._on_chromecast_discovered,
                remove_callback=self._on_chromecast_removed,
                update_callback=self._on_chromecast_discovered,
            ),
            self.mass.zeroconf,
        )
        # start discovery in executor
        await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def close(self) -> None:
        """Handle close/cleanup of the provider."""
        if not self.browser:
            return
        # stop discovery
        await self.mass.loop.run_in_executor(None, self.browser.stop_discovery)
        # stop all chromecasts
        for castplayer in list(self.castplayers.values()):
            await self._disconnect_chromecast(castplayer)

    def on_player_config_changed(self, config: PlayerConfig) -> None:  # noqa: ARG002
        """Call (by config manager) when the configuration of a player changes."""

        # run discovery to catch any re-enabled players
        async def restart_discovery():
            await self.mass.loop.run_in_executor(None, self.browser.stop_discovery)
            await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

        self.mass.create_task(restart_discovery())

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.media_controller.stop)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.media_controller.play)

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player."""
        castplayer = self.castplayers[player_id]
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            player_id=player_id,
            seek_position=seek_position,
            fade_in=fade_in,
            # prefer FLAC as it seems to work on all CC players
            content_type=ContentType.FLAC,
            flow_mode=flow_mode,
        )
        castplayer.flow_mode_active = flow_mode

        # in flow mode, we just send the url and the metadata is of no use
        if flow_mode:
            await asyncio.to_thread(
                castplayer.cc.play_media,
                url,
                content_type="audio/flac",
                title="Music Assistant",
                thumb=MASS_LOGO_ONLINE,
                media_info={
                    "customData": {
                        "queue_item_id": queue_item.queue_item_id,
                    }
                },
            )
            return

        cc_queue_items = [self._create_queue_item(queue_item, url)]
        queuedata = {
            "type": "QUEUE_LOAD",
            "repeatMode": "REPEAT_OFF",  # handled by our queue controller
            "shuffle": False,  # handled by our queue controller
            "queueType": "PLAYLIST",
            "startIndex": 0,  # Item index to play after this request or keep same item if undefined
            "items": cc_queue_items,
        }
        # make sure that media controller app is launched
        await self._launch_app(castplayer)
        # send queue info to the CC
        castplayer.next_item = None
        media_controller = castplayer.cc.media_controller
        await asyncio.to_thread(media_controller.send_message, queuedata, True)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.media_controller.pause)

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        castplayer = self.castplayers[player_id]
        if powered:
            await self._launch_app(castplayer)
        else:
            await asyncio.to_thread(castplayer.cc.quit_app)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.set_volume, volume_level / 100)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.set_volume_muted, muted)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        - every 360 seconds if the player if not powered
        - every 30 seconds if the player is powered
        - every 10 seconds if the player is playing

        Use this method to request any info that is not automatically updated and/or
        to detect if the player is still alive.
        If this method raises the PlayerUnavailable exception,
        the player is marked as unavailable until
        the next successful poll or event where it becomes available again.
        If the player does not need any polling, simply do not override this method.
        """
        castplayer = self.castplayers[player_id]
        try:
            await asyncio.to_thread(castplayer.cc.media_controller.update_status)
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    ### Discovery callbacks

    def _on_chromecast_discovered(self, uuid, _):
        """Handle Chromecast discovered callback."""
        if self.mass.closing:
            return

        disc_info: CastInfo = self.browser.devices[uuid]

        if disc_info.uuid is None:
            self.logger.error("Discovered chromecast without uuid %s", disc_info)
            return

        player_id = str(disc_info.uuid)

        enabled = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/enabled", True)
        if not enabled:
            self.logger.debug("Ignoring disabled player: %s", player_id)
            return

        self.logger.debug("Discovered new or updated chromecast %s", disc_info)

        castplayer = self.castplayers.get(player_id)
        if not castplayer:
            cast_info = ChromecastInfo.from_cast_info(disc_info)
            cast_info.fill_out_missing_chromecast_info(self.mass.zeroconf)
            if cast_info.is_dynamic_group:
                self.logger.warning("Discovered a dynamic cast group which will be ignored.")
                return

            # Instantiate chromecast object
            castplayer = CastPlayer(
                player_id,
                cast_info=cast_info,
                cc=get_chromecast_from_cast_info(
                    disc_info,
                    self.mass.zeroconf,
                ),
                player=Player(
                    player_id=player_id,
                    provider=self.domain,
                    type=PlayerType.GROUP if cast_info.is_audio_group else PlayerType.PLAYER,
                    name=cast_info.friendly_name,
                    available=False,
                    powered=False,
                    device_info=DeviceInfo(
                        model=cast_info.model_name,
                        address=cast_info.host,
                        manufacturer=cast_info.manufacturer,
                    ),
                    supported_features=(
                        PlayerFeature.POWER,
                        PlayerFeature.VOLUME_MUTE,
                        PlayerFeature.VOLUME_SET,
                    ),
                    max_sample_rate=96000,
                ),
                logger=self.logger.getChild(cast_info.friendly_name),
            )
            self.castplayers[player_id] = castplayer

            castplayer.status_listener = CastStatusListener(self, castplayer, self.mz_mgr)
            if cast_info.is_audio_group:
                mz_controller = MultizoneController(cast_info.uuid)
                castplayer.cc.register_handler(mz_controller)
                castplayer.mz_controller = mz_controller
            castplayer.cc.start()

            self.mass.loop.call_soon_threadsafe(self.mass.players.register, castplayer.player)

        # if player was already added, the player will take care of reconnects itself.
        castplayer.cast_info.update(disc_info)
        self.mass.loop.call_soon_threadsafe(self.mass.players.update, player_id)

    def _on_chromecast_removed(self, uuid, service, cast_info):  # noqa: ARG002
        """Handle zeroconf discovery of a removed Chromecast."""
        # noqa: ARG001
        player_id = str(service[1])
        friendly_name = service[3]
        self.logger.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        # we ignore this event completely as the Chromecast socket client handles this itself

    ### Callbacks from Chromecast Statuslistener

    def on_new_cast_status(self, castplayer: CastPlayer, status: CastStatus) -> None:
        """Handle updated CastStatus."""
        castplayer.logger.debug(
            "Received cast status - app_id: %s - volume: %s",
            status.app_id,
            status.volume_level,
        )
        castplayer.player.name = castplayer.cast_info.friendly_name
        castplayer.player.powered = status.app_id in (
            "705D30C6",
            APP_MEDIA_RECEIVER,
            APP_BUBBLEUPNP,
        )
        castplayer.is_stereo_pair = (
            castplayer.cast_info.is_audio_group
            and castplayer.mz_controller
            and castplayer.mz_controller.members
            and compare_strings(castplayer.mz_controller.members[0], castplayer.player_id)
        )
        castplayer.player.volume_level = int(status.volume_level * 100)
        castplayer.player.volume_muted = status.volume_muted
        if castplayer.is_stereo_pair:
            castplayer.player.type = PlayerType.PLAYER
        self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)

    def on_new_media_status(self, castplayer: CastPlayer, status: MediaStatus):
        """Handle updated MediaStatus."""
        castplayer.logger.debug("Received media status update: %s", status.player_state)
        prev_item_id = castplayer.player.current_item_id
        # player state
        if status.player_is_playing:
            castplayer.player.state = PlayerState.PLAYING
        elif status.player_is_paused:
            castplayer.player.state = PlayerState.PAUSED
        else:
            castplayer.player.state = PlayerState.IDLE

        # elapsed time
        castplayer.player.elapsed_time_last_updated = time.time()
        if status.player_is_playing:
            castplayer.player.elapsed_time = status.adjusted_current_time
        else:
            castplayer.player.elapsed_time = status.current_time

        # current media
        queue_item_id = status.media_custom_data.get("queue_item_id")
        castplayer.player.current_item_id = queue_item_id
        castplayer.player.current_url = status.content_id
        self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)

        # enqueue next item if needed
        if castplayer.player.state == PlayerState.PLAYING and (
            prev_item_id != castplayer.player.current_item_id
            or not castplayer.next_item
            or castplayer.next_item == castplayer.player.current_item_id
        ):
            asyncio.run_coroutine_threadsafe(
                self._enqueue_next_track(castplayer, queue_item_id), self.mass.loop
            )

    def on_new_connection_status(self, castplayer: CastPlayer, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus."""
        castplayer.logger.debug("Received connection status update - status: %s", status.status)

        if status.status == CONNECTION_STATUS_DISCONNECTED:
            castplayer.player.available = False
            self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)
            return

        new_available = status.status == CONNECTION_STATUS_CONNECTED
        if new_available != castplayer.player.available:
            self.logger.debug(
                "[%s] Cast device availability changed: %s",
                castplayer.cast_info.friendly_name,
                status.status,
            )
            castplayer.player.available = new_available
            castplayer.player.device_info = DeviceInfo(
                model=castplayer.cast_info.model_name,
                address=castplayer.cast_info.host,
                manufacturer=castplayer.cast_info.manufacturer,
            )
            self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)
            if new_available and not castplayer.cast_info.is_audio_group:
                # Poll current group status
                for group_uuid in self.mz_mgr.get_multizone_memberships(castplayer.cast_info.uuid):
                    group_media_controller = self.mz_mgr.get_multizone_mediacontroller(group_uuid)
                    if not group_media_controller:
                        continue
                    self.on_multizone_new_media_status(
                        castplayer, group_uuid, group_media_controller.status
                    )

    def on_multizone_new_media_status(
        self, castplayer: CastPlayer, group_uuid: UUID, media_status: MediaStatus  # noqa: ARG002
    ):
        """Handle updates of audio group media status."""
        castplayer.logger.debug("Received multizone media status update")
        # self.mz_media_status[group_uuid] = media_status
        # self.mz_media_status_received[group_uuid] = dt_util.utcnow()
        # self.schedule_update_ha_state()

    ### Helpers / utils

    async def _enqueue_next_track(self, castplayer: CastPlayer, current_queue_item_id: str) -> None:
        """Enqueue the next track of the MA queue on the CC queue."""
        if castplayer.flow_mode_active:
            # not possible when we're in flow mode
            return

        if not current_queue_item_id:
            return  # guard
        try:
            next_item, crossfade = self.mass.players.queues.player_ready_for_next_track(
                castplayer.player_id, current_queue_item_id
            )
        except QueueEmpty:
            return

        if castplayer.next_item == next_item.queue_item_id:
            return  # already set ?!
        castplayer.next_item = next_item.queue_item_id

        if crossfade:
            self.logger.warning(
                "Crossfade requested but Chromecast does not support crossfading,"
                " consider using flow mode to enable crossfade on a Chromecast."
            )

        url = await self.mass.streams.resolve_stream_url(
            queue_item=next_item,
            player_id=castplayer.player_id,
            content_type=ContentType.FLAC,
            auto_start_runner=False,
        )
        cc_queue_items = [self._create_queue_item(next_item, url)]

        queuedata = {
            "type": "QUEUE_INSERT",
            "insertBefore": None,
            "items": cc_queue_items,
        }
        media_controller = castplayer.cc.media_controller
        queuedata["mediaSessionId"] = media_controller.status.media_session_id

        await asyncio.sleep(0.5)  # throttle commands to CC a bit or it will crash
        await asyncio.to_thread(media_controller.send_message, queuedata, True)

    async def _launch_app(self, castplayer: CastPlayer) -> None:
        """Launch the default Media Receiver App on a Chromecast."""
        event = asyncio.Event()

        def launched_callback():
            self.mass.loop.call_soon_threadsafe(event.set)

        def launch():
            # controller = BubbleUPNPController()
            # castplayer.cc.register_handler(controller)
            # controller.launch(launched_callback)
            castplayer.cc.media_controller.launch(launched_callback)

        castplayer.logger.debug("Launching BubbleUPNPController as active app.")
        await self.mass.loop.run_in_executor(None, launch)
        await event.wait()

    async def _disconnect_chromecast(self, castplayer: CastPlayer) -> None:
        """Disconnect Chromecast object if it is set."""
        castplayer.logger.debug("Disconnecting from chromecast socket")
        await self.mass.loop.run_in_executor(None, castplayer.cc.disconnect, 10)
        castplayer.mz_controller = None
        castplayer.status_listener.invalidate()
        castplayer.status_listener = None
        self.castplayers.pop(castplayer.player_id, None)

    @staticmethod
    def _create_queue_item(queue_item: QueueItem, stream_url: str):
        """Create CC queue item from MA QueueItem."""
        duration = int(queue_item.duration) if queue_item.duration else None
        if queue_item.media_type == MediaType.TRACK:
            stream_type = STREAM_TYPE_BUFFERED
            metadata = {
                "metadataType": 3,
                "albumName": queue_item.media_item.album.name,
                "songName": queue_item.media_item.name,
                "artist": queue_item.media_item.artist.name,
                "title": queue_item.name,
                "images": [{"url": queue_item.image.url}] if queue_item.image else None,
            }
        else:
            stream_type = STREAM_TYPE_LIVE
            metadata = {
                "metadataType": 0,
                "title": queue_item.name,
                "images": [{"url": queue_item.image.url}] if queue_item.image else None,
            }
        return {
            "autoplay": True,
            "preloadTime": 10,
            "playbackDuration": duration,
            "startTime": 0,
            "activeTrackIds": [],
            "media": {
                "contentId": stream_url,
                "customData": {
                    "uri": queue_item.uri,
                    "queue_item_id": queue_item.queue_item_id,
                },
                "contentType": "audio/flac",
                "streamType": stream_type,
                "metadata": metadata,
                "duration": duration,
            },
        }
