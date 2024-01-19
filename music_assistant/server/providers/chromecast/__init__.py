"""Chromecast Player provider for Music Assistant, utilizing the pychromecast library."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from logging import Logger
from typing import TYPE_CHECKING
from uuid import UUID

import pychromecast
from pychromecast.controllers.bubbleupnp import BubbleUPNPController
from pychromecast.controllers.media import STREAM_TYPE_BUFFERED, STREAM_TYPE_LIVE
from pychromecast.controllers.multizone import MultizoneController, MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import PlayerUnavailableError
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_CROSSFADE, CONF_LOG_LEVEL, CONF_PLAYERS, MASS_LOGO_ONLINE
from music_assistant.server.models.player_provider import PlayerProvider

from .helpers import CastStatusListener, ChromecastInfo

if TYPE_CHECKING:
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.socket_client import ConnectionStatus

    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


CONF_ALT_APP = "alt_app"


PLAYER_CONFIG_ENTRIES = (
    ConfigEntry(
        key=CONF_CROSSFADE,
        type=ConfigEntryType.BOOLEAN,
        label="Enable crossfade",
        default_value=False,
        description="Enable a crossfade transition between (queue) tracks. \n"
        "Note that Chromecast does not natively support crossfading so Music Assistant "
        "uses a 'flow mode' workaround for this at the cost of on-player metadata.",
        advanced=False,
    ),
    ConfigEntry(
        key=CONF_ALT_APP,
        type=ConfigEntryType.BOOLEAN,
        label="Use alternate Media app",
        default_value=False,
        description="Using the BubbleUPNP Media controller for playback improves "
        "the playback experience but may not work on non-Google hardware.",
        advanced=True,
    ),
    CONF_ENTRY_CROSSFADE_DURATION,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = ChromecastProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return tuple()  # we do not have any config entries (yet)


@dataclass
class CastPlayer:
    """Wrapper around Chromecast with some additional attributes."""

    player_id: str
    cast_info: ChromecastInfo
    cc: pychromecast.Chromecast
    player: Player
    logger: Logger
    status_listener: CastStatusListener | None = None
    mz_controller: MultizoneController | None = None
    active_group: str | None = None
    current_queue_item_id: str | None = None


class ChromecastProvider(PlayerProvider):
    """Player provider for Chromecast based players."""

    mz_mgr: MultizoneManager | None = None
    browser: CastBrowser | None = None
    castplayers: dict[str, CastPlayer]
    _discover_lock: threading.Lock

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self._discover_lock = threading.Lock()
        self.castplayers = {}
        self.mz_mgr = MultizoneManager()
        self.browser = CastBrowser(
            SimpleCastListener(
                add_callback=self._on_chromecast_discovered,
                remove_callback=self._on_chromecast_removed,
                update_callback=self._on_chromecast_discovered,
            ),
            self.mass.zeroconf,
        )
        # silence pychromecast logging
        log_level = self.config.get_value(CONF_LOG_LEVEL)
        if log_level == "DEBUG":
            logging.getLogger("pychromecast").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pychromecast").setLevel(logging.INFO)
        # start discovery in executor
        await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        if not self.browser:
            return

        # stop discovery
        def stop_discovery():
            """Stop the chromecast discovery threads."""
            if self.browser._zc_browser:
                with contextlib.suppress(RuntimeError):
                    self.browser._zc_browser.cancel()

            self.browser.host_browser.stop.set()
            self.browser.host_browser.join()

        await self.mass.loop.run_in_executor(None, stop_discovery)
        # stop all chromecasts
        for castplayer in list(self.castplayers.values()):
            await self._disconnect_chromecast(castplayer)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return base_entries + PLAYER_CONFIG_ENTRIES

    def on_player_config_changed(
        self, config: PlayerConfig, changed_keys: set[str]  # noqa: ARG002
    ) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        if "enabled" in changed_keys and config.player_id not in self.castplayers:
            self.mass.create_task(self.mass.config.reload_provider, self.instance_id)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.media_controller.stop)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        castplayer = self.castplayers[player_id]
        await asyncio.to_thread(castplayer.cc.media_controller.play)

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

    async def play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        castplayer = self.castplayers[player_id]
        # Google cast does not support crossfading so we use flow mode to provide this feature
        use_flow_mode = await self.mass.config.get_player_config_value(player_id, CONF_CROSSFADE)
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.FLAC,
            seek_position=seek_position,
            fade_in=fade_in,
            flow_mode=use_flow_mode,
        )
        if use_flow_mode:
            # In flow mode, all queue tracks are sent to the player as continuous stream.
            # This comes at the cost of metadata (cast does not support ICY metadata).
            await asyncio.to_thread(
                castplayer.cc.play_media,
                url,
                content_type=f'audio/{url.split(".")[-1].split("?")[0]}',
                title="Music Assistant",
                thumb=MASS_LOGO_ONLINE,
            )
            return
        # handle normal playback using the chromecast queue to play items one by one
        cc_queue_items = [self._create_cc_queue_item(queue_item, url)]
        queuedata = {
            "type": "QUEUE_LOAD",
            "repeatMode": "REPEAT_OFF",  # handled by our queue controller
            "shuffle": False,  # handled by our queue controller
            "queueType": "PLAYLIST",
            "startIndex": 0,  # Item index to play after this request or keep same item if undefined
            "items": cc_queue_items,
        }
        # make sure that the media controller app is launched
        await self._launch_app(castplayer)
        # send queue info to the CC
        media_controller = castplayer.cc.media_controller
        await asyncio.to_thread(media_controller.send_message, queuedata, True)

    async def enqueue_next_queue_item(self, player_id: str, queue_item: QueueItem):
        """Handle enqueuing of the next queue item on the player."""
        castplayer = self.castplayers[player_id]
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.FLAC,
        )
        queuedata = {
            "type": "QUEUE_INSERT",
            "insertBefore": None,
            "items": [self._create_cc_queue_item(queue_item, url)],
        }
        media_controller = castplayer.cc.media_controller
        queuedata["mediaSessionId"] = media_controller.status.media_session_id
        await asyncio.to_thread(media_controller.send_message, queuedata, True)

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
        # only update status of media controller if player is on
        if not castplayer.player.powered:
            return
        if not castplayer.cc.media_controller.is_active:
            return
        try:
            await asyncio.to_thread(castplayer.cc.media_controller.update_status)
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    ### Discovery callbacks

    def _on_chromecast_discovered(self, uuid, _):
        """Handle Chromecast discovered callback."""
        if self.mass.closing:
            return

        with self._discover_lock:
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
            if castplayer:
                # if player was already added, the player will take care of reconnects itself.
                castplayer.cast_info.update(disc_info)
                self.mass.loop.call_soon_threadsafe(self.mass.players.update, player_id)
                return
            # new player discovered
            cast_info = ChromecastInfo.from_cast_info(disc_info)
            cast_info.fill_out_missing_chromecast_info(self.mass.zeroconf)
            if cast_info.is_dynamic_group:
                self.logger.debug("Discovered a dynamic cast group which will be ignored.")
                return
            if cast_info.is_multichannel_child:
                self.logger.debug(
                    "Discovered a passive (multichannel) endpoint which will be ignored."
                )
                return

            # Disable TV's by default
            # (can be enabled manually by the user)
            enabled_by_default = True
            for exclude in ("tv", "/12", "PUS", "OLED"):
                if exclude.lower() in cast_info.friendly_name.lower():
                    enabled_by_default = False

            # Instantiate chromecast object
            castplayer = CastPlayer(
                player_id,
                cast_info=cast_info,
                cc=pychromecast.get_chromecast_from_cast_info(
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
                        address=f"{cast_info.host}:{cast_info.port}",
                        manufacturer=cast_info.manufacturer,
                    ),
                    supported_features=(
                        PlayerFeature.POWER,
                        PlayerFeature.VOLUME_MUTE,
                        PlayerFeature.VOLUME_SET,
                        PlayerFeature.ENQUEUE_NEXT,
                        PlayerFeature.PAUSE,
                    ),
                    max_sample_rate=96000,
                    supports_24bit=True,
                    enabled_by_default=enabled_by_default,
                ),
                logger=self.logger.getChild(cast_info.friendly_name),
            )
            self.castplayers[player_id] = castplayer

            castplayer.status_listener = CastStatusListener(self, castplayer, self.mz_mgr)
            if cast_info.is_audio_group and not cast_info.is_multichannel_group:
                mz_controller = MultizoneController(cast_info.uuid)
                castplayer.cc.register_handler(mz_controller)
                castplayer.mz_controller = mz_controller

            castplayer.cc.start()
            self.mass.loop.call_soon_threadsafe(
                self.mass.players.register_or_update, castplayer.player
            )

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
        if status is None:
            return  # guard
        castplayer.logger.debug(
            "Received cast status - app_id: %s - volume: %s",
            status.app_id,
            status.volume_level,
        )
        castplayer.player.name = castplayer.cast_info.friendly_name
        castplayer.player.volume_level = int(status.volume_level * 100)
        castplayer.player.volume_muted = status.volume_muted
        if castplayer.active_group:
            # use mute as power when group is active
            castplayer.player.powered = not status.volume_muted
        else:
            castplayer.player.powered = (
                castplayer.cc.app_id is not None
                and castplayer.cc.app_id != pychromecast.IDLE_APP_ID
            )
        # handle stereo pairs
        if castplayer.cast_info.is_multichannel_group:
            castplayer.player.type = PlayerType.PLAYER
            castplayer.player.group_childs = set()
        # handle cast groups
        if castplayer.cast_info.is_audio_group and not castplayer.cast_info.is_multichannel_group:
            castplayer.player.type = PlayerType.GROUP
            castplayer.player.group_childs = {
                str(UUID(x)) for x in castplayer.mz_controller.members
            }
            castplayer.player.supported_features = (
                PlayerFeature.POWER,
                PlayerFeature.VOLUME_SET,
            )

        # send update to player manager
        self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)

    def on_new_media_status(self, castplayer: CastPlayer, status: MediaStatus):
        """Handle updated MediaStatus."""
        castplayer.logger.debug("Received media status update: %s", status.player_state)
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

        # active source
        if status.content_id and castplayer.player_id in status.content_id:
            castplayer.player.active_source = castplayer.player_id
        else:
            castplayer.player.active_source = castplayer.cc.app_display_name

        # current media
        castplayer.player.current_item_id = status.content_id
        self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)

        # handle end of MA queue - reset current_item_id
        if (
            castplayer.player.state == PlayerState.IDLE
            and castplayer.player.current_item_id
            and (queue := self.mass.player_queues.get(castplayer.player_id))
            and queue.next_item is None
        ):
            castplayer.player.current_item_id = None

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
                address=f"{castplayer.cast_info.host}:{castplayer.cast_info.port}",
                manufacturer=castplayer.cast_info.manufacturer,
            )
            self.mass.loop.call_soon_threadsafe(self.mass.players.update, castplayer.player_id)
            if new_available and not castplayer.cast_info.is_audio_group:
                # Poll current group status
                for group_uuid in self.mz_mgr.get_multizone_memberships(castplayer.cast_info.uuid):
                    group_media_controller = self.mz_mgr.get_multizone_mediacontroller(group_uuid)
                    if not group_media_controller:
                        continue

    ### Helpers / utils

    async def _launch_app(self, castplayer: CastPlayer) -> None:
        """Launch the default Media Receiver App on a Chromecast."""
        event = asyncio.Event()
        if use_alt_app := await self.mass.config.get_player_config_value(
            castplayer.player_id, CONF_ALT_APP
        ):
            app_id = pychromecast.config.APP_BUBBLEUPNP
        else:
            app_id = pychromecast.config.APP_MEDIA_RECEIVER

        if castplayer.cc.app_id == app_id:
            return  # already active

        def launched_callback():
            self.mass.loop.call_soon_threadsafe(event.set)

        def launch():
            # Quit the previous app before starting splash screen or media player
            if castplayer.cc.app_id is not None:
                castplayer.cc.quit_app()
            # Use BubbleUPNP media receiver app if configured
            # which enables a more rich display but does not work on all players
            # so its configurable to turn it on/off
            if use_alt_app:
                castplayer.logger.debug(
                    "Launching BubbleUPNPController (%s) as active app.", app_id
                )
                controller = BubbleUPNPController()
                castplayer.cc.register_handler(controller)
                controller.launch(launched_callback)
            else:
                castplayer.logger.debug(
                    "Launching Default Media Receiver (%s) as active app.", app_id
                )
                castplayer.cc.media_controller.launch(launched_callback)

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

    def _create_cc_queue_item(self, queue_item: QueueItem, stream_url: str):
        """Create CC queue item from MA QueueItem."""
        duration = int(queue_item.duration) if queue_item.duration else None
        image_url = self.mass.metadata.get_image_url(queue_item.image) if queue_item.image else ""
        if queue_item.media_type == MediaType.TRACK and queue_item.media_item:
            stream_type = STREAM_TYPE_BUFFERED
            metadata = {
                "metadataType": 3,
                "albumName": queue_item.media_item.album.name
                if queue_item.media_item.album
                else "",
                "songName": queue_item.media_item.name,
                "artist": queue_item.media_item.artists[0].name
                if queue_item.media_item.artists
                else "",
                "title": queue_item.media_item.name,
                "images": [{"url": image_url}] if image_url else None,
            }
        else:
            stream_type = STREAM_TYPE_LIVE
            metadata = {
                "metadataType": 0,
                "title": queue_item.name,
                "images": [{"url": image_url}] if image_url else None,
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
