"""
Home Assistant PlayerProvider for Music Assistant.

Allows using media_player entities in HA to be used as players in MA.
Requires the Home Assistant Plugin.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from hass_client.exceptions import FailedCommand
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlayerFeature, PlayerState, PlayerType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia

from music_assistant.constants import (
    CONF_ENFORCE_MP3,
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_ENFORCE_MP3_DEFAULT_ENABLED,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.datetime import from_iso_string
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.hass import DOMAIN as HASS_DOMAIN
from music_assistant.providers.hass.constants import (
    OFF_STATES,
    UNAVAILABLE_STATES,
    MediaPlayerEntityFeature,
    StateMap,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from hass_client.models import CompressedState, EntityStateEvent
    from hass_client.models import Device as HassDevice
    from hass_client.models import Entity as HassEntity
    from hass_client.models import State as HassState
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.hass import HomeAssistantProvider

CONF_PLAYERS = "players"


DEFAULT_PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_ENFORCE_MP3_DEFAULT_ENABLED,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
)
ESPHOME_V2_MODELS = (
    # The Home Assistant Voice PE introduces a new ESPHome mediaplayer
    # that supports FLAC 48khz/16 bits and has some other optimizations
    # this player is also used in some other (voice) ESPHome projects
    # so until the new media player component is merged into ESPHome
    # we keep a list here of model names that use the new player
    "Home Assistant Voice PE",
    "Koala Satellite",
    "Respeaker Lite Satellite",
    "Satellite1",
)
ESPHOME_V2_MODELS_PLAYER_CONFIG_ENTRIES = (
    # New ESPHome mediaplayer (used in Voice PE) uses FLAC 48khz/16 bits
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    create_sample_rates_config_entry(48000, 16, hidden=True, supported_sample_rates=[48000]),
    # although the Voice PE supports announcements, it does not support volume for announcements
    *HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES,
)


async def _get_hass_media_players(
    hass_prov: HomeAssistantProvider,
) -> AsyncGenerator[HassState, None]:
    """Return all HA state objects for (valid) media_player entities."""
    for state in await hass_prov.hass.get_states():
        if not state["entity_id"].startswith("media_player"):
            continue
        if "mass_player_type" in state["attributes"]:
            # filter out mass players
            continue
        if "friendly_name" not in state["attributes"]:
            # filter out invalid/unavailable players
            continue
        supported_features = MediaPlayerEntityFeature(state["attributes"]["supported_features"])
        if MediaPlayerEntityFeature.PLAY_MEDIA not in supported_features:
            continue
        yield state


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    hass_prov: HomeAssistantProvider = mass.get_provider(HASS_DOMAIN)
    if not hass_prov:
        msg = "The Home Assistant Plugin needs to be set-up first"
        raise SetupFailedError(msg)
    prov = HomeAssistantPlayers(mass, manifest, config)
    prov.hass_prov = hass_prov
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    hass_prov: HomeAssistantProvider = mass.get_provider(HASS_DOMAIN)
    player_entities: list[ConfigValueOption] = []
    if hass_prov and hass_prov.hass.connected:
        async for state in _get_hass_media_players(hass_prov):
            name = f"{state['attributes']['friendly_name']} ({state['entity_id']})"
            player_entities.append(ConfigValueOption(name, state["entity_id"]))
    return (
        ConfigEntry(
            key=CONF_PLAYERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Player entities",
            required=True,
            options=player_entities,
            description="Specify which HA media_player entity id's you "
            "like to import as players in Music Assistant.",
        ),
    )


class HomeAssistantPlayers(PlayerProvider):
    """Home Assistant PlayerProvider for Music Assistant."""

    hass_prov: HomeAssistantProvider

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        player_ids: list[str] = self.config.get_value(CONF_PLAYERS)
        # prefetch the device- and entity registry
        device_registry = {x["id"]: x for x in await self.hass_prov.hass.get_device_registry()}
        entity_registry = {
            x["entity_id"]: x for x in await self.hass_prov.hass.get_entity_registry()
        }
        # setup players from hass entities
        async for state in _get_hass_media_players(self.hass_prov):
            if state["entity_id"] not in player_ids:
                continue
            await self._setup_player(state, entity_registry, device_registry)
        # register for entity state updates
        await self.hass_prov.hass.subscribe_entities(self._on_entity_state_update, player_ids)
        # remove any leftover players (after reconfigure of players)
        for player in self.players:
            if player.player_id not in player_ids:
                self.mass.players.remove(player.player_id)

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        player = self.mass.players.get(player_id)
        if player and player.device_info.model in ESPHOME_V2_MODELS:
            # optimized config for new ESPHome mediaplayer
            return base_entries + ESPHOME_V2_MODELS_PLAYER_CONFIG_ENTRIES
        return base_entries + DEFAULT_PLAYER_CONFIG_ENTRIES

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        try:
            await self.hass_prov.hass.call_service(
                domain="media_player",
                service="media_stop",
                target={"entity_id": player_id},
            )
        except FailedCommand as exc:
            # some HA players do not support STOP
            if "does not support this service" not in str(exc):
                raise
            if player := self.mass.players.get(player_id):
                if PlayerFeature.PAUSE in player.supported_features:
                    await self.cmd_pause(player_id)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player", service="media_play", target={"entity_id": player_id}
        )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="media_pause",
            target={"entity_id": player_id},
        )

    async def play_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        is_esphome_v2 = self.mass.players.get(player_id).device_info.model in ESPHOME_V2_MODELS
        if self.mass.config.get_raw_player_config_value(
            player_id, CONF_ENFORCE_MP3, not is_esphome_v2
        ):
            media.uri = media.uri.replace(".flac", ".mp3")
        player = self.mass.players.get(player_id, True)
        assert player
        extra_data = {
            # passing metadata to the player
            # so far only supported by google cast, but maybe others can follow
            "metadata": {
                "title": media.title,
                "artist": media.artist,
                "metadataType": 3,
                "album": media.album,
                "albumName": media.album,
                "images": [{"url": media.image_url}] if media.image_url else None,
                "imageUrl": media.image_url,
            },
        }
        if player.extra_data.get("hass_domain") == "esphome":
            # tell esphome mediaproxy to bypass the proxy,
            # as MA already delivers an optimized stream
            extra_data["bypass_proxy"] = True

        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="play_media",
            service_data={
                "media_content_id": media.uri,
                "media_content_type": "music",
                "enqueue": "replace",
                "extra": extra_data,
            },
            target={"entity_id": player_id},
        )
        # optimistically set the elapsed_time as some HA players do not report this
        if player := self.mass.players.get(player_id):
            player.elapsed_time = 0
            player.elapsed_time_last_updated = time.time()
            player.current_media = media

    async def play_announcement(
        self, player_id: str, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        player = self.mass.players.get(player_id, True)
        self.logger.info(
            "Playing announcement %s on %s",
            announcement.uri,
            player.display_name,
        )
        if volume_level is not None:
            self.logger.warning(
                "Announcement volume level is not supported for player %s",
                player.display_name,
            )
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="play_media",
            service_data={
                "media_content_id": announcement.uri,
                "media_content_type": "music",
                "announce": True,
            },
            target={"entity_id": player_id},
        )
        # Wait until the announcement is finished playing
        # This is helpful for people who want to play announcements in a sequence
        media_info = await async_parse_tags(announcement.uri)
        duration = media_info.duration or 5
        await asyncio.sleep(duration)
        self.logger.debug(
            "Playing announcement on %s completed",
            player.display_name,
        )

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="turn_on" if powered else "turn_off",
            target={"entity_id": player_id},
        )

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="volume_set",
            service_data={"volume_level": volume_level / 100},
            target={"entity_id": player_id},
        )

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="volume_mute",
            service_data={"is_volume_muted": muted},
            target={"entity_id": player_id},
        )

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        # NOTE: not in use yet, as we do not support syncgroups in MA for HA players
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="join",
            service_data={"group_members": [player_id]},
            target={"entity_id": target_player},
        )

    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is grouped to.

            - player_id: player_id of the player to handle the command.
        """
        # NOTE: not in use yet, as we do not support syncgroups in MA for HA players
        await self.hass_prov.hass.call_service(
            domain="media_player",
            service="unjoin",
            target={"entity_id": player_id},
        )

    async def _setup_player(
        self,
        state: HassState,
        entity_registry: dict[str, HassEntity],
        device_registry: dict[str, HassDevice],
    ) -> None:
        """Handle setup of a Player from an hass entity."""
        hass_device: HassDevice | None = None
        hass_domain: str | None = None
        if entity_registry_entry := entity_registry.get(state["entity_id"]):
            hass_device = device_registry.get(entity_registry_entry["device_id"])
            hass_domain = entity_registry_entry["platform"]

        dev_info: dict[str, Any] = {}
        if hass_device and (model := hass_device.get("model")):
            dev_info["model"] = model
        if hass_device and (manufacturer := hass_device.get("manufacturer")):
            dev_info["manufacturer"] = manufacturer
        if hass_device and (model_id := hass_device.get("model_id")):
            dev_info["model_id"] = model_id
        if hass_device and (sw_version := hass_device.get("sw_version")):
            dev_info["software_version"] = sw_version
        if hass_device and (connections := hass_device.get("connections")):
            for key, value in connections:
                if key == "mac":
                    dev_info["mac_address"] = value

        player = Player(
            player_id=state["entity_id"],
            provider=self.lookup_key,
            type=PlayerType.PLAYER,
            name=state["attributes"]["friendly_name"],
            available=state["state"] not in UNAVAILABLE_STATES,
            device_info=DeviceInfo.from_dict(dev_info),
            state=StateMap.get(state["state"], PlayerState.IDLE),
            extra_data={
                "hass_domain": hass_domain,
                "hass_device_id": hass_device["id"] if hass_device else None,
            },
        )
        # work out supported features
        hass_supported_features = MediaPlayerEntityFeature(
            state["attributes"]["supported_features"]
        )
        if MediaPlayerEntityFeature.PAUSE in hass_supported_features:
            player.supported_features.add(PlayerFeature.PAUSE)
        if MediaPlayerEntityFeature.VOLUME_SET in hass_supported_features:
            player.supported_features.add(PlayerFeature.VOLUME_SET)
        if MediaPlayerEntityFeature.VOLUME_MUTE in hass_supported_features:
            player.supported_features.add(PlayerFeature.VOLUME_MUTE)
        if MediaPlayerEntityFeature.MEDIA_ANNOUNCE in hass_supported_features:
            player.supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if hass_domain and MediaPlayerEntityFeature.GROUPING in hass_supported_features:
            player.supported_features.add(PlayerFeature.SET_MEMBERS)
            player.can_group_with = {
                x["entity_id"]
                for x in entity_registry.values()
                if x["entity_id"].startswith("media_player") and x["platform"] == hass_domain
            }
        if (
            MediaPlayerEntityFeature.TURN_ON in hass_supported_features
            and MediaPlayerEntityFeature.TURN_OFF in hass_supported_features
        ):
            player.supported_features.add(PlayerFeature.POWER)
            player.powered = state["state"] not in OFF_STATES

        self._update_player_attributes(player, state["attributes"])
        await self.mass.players.register_or_update(player)

    def _on_entity_state_update(self, event: EntityStateEvent) -> None:
        """Handle Entity State event."""

        def update_player_from_state_msg(entity_id: str, state: CompressedState) -> None:
            """Handle updating MA player with updated info in a HA CompressedState."""
            player = self.mass.players.get(entity_id)
            if player is None:
                # edge case - one of our subscribed entities was not available at startup
                # and now came available - we should still set it up
                player_ids: list[str] = self.config.get_value(CONF_PLAYERS)
                if entity_id not in player_ids:
                    return  # should not happen, but guard just in case
                self.mass.create_task(self._late_add_player(entity_id))
                return
            if "s" in state:
                player.state = StateMap.get(state["s"], PlayerState.IDLE)
                player.available = state["s"] not in UNAVAILABLE_STATES
                if PlayerFeature.POWER in player.supported_features:
                    player.powered = state["s"] not in OFF_STATES
            if "a" in state:
                self._update_player_attributes(player, state["a"])
            self.mass.players.update(entity_id)

        if entity_additions := event.get("a"):
            for entity_id, state in entity_additions.items():
                update_player_from_state_msg(entity_id, state)
        if entity_changes := event.get("c"):
            for entity_id, state_diff in entity_changes.items():
                if "+" not in state_diff:
                    continue
                update_player_from_state_msg(entity_id, state_diff["+"])

    def _update_player_attributes(self, player: Player, attributes: dict[str, Any]) -> None:
        """Update Player attributes from HA state attributes."""
        for key, value in attributes.items():
            if key == "media_position":
                player.elapsed_time = value
            if key == "media_position_updated_at":
                player.elapsed_time_last_updated = from_iso_string(value).timestamp()
            if key == "volume_level":
                player.volume_level = int(value * 100)
            if key == "volume_muted":
                player.volume_muted = value
            if key == "media_content_id":
                player.current_item_id = value
            if key == "group_members":
                if value and value[0] == player.player_id:
                    player.group_childs.set(value)
                    player.synced_to = None
                elif value and value[0] != player.player_id:
                    player.group_childs.clear()
                    player.synced_to = value[0]
                else:
                    player.group_childs.clear()
                    player.synced_to = None

    async def _late_add_player(self, entity_id: str) -> None:
        """Handle setup of Player from HA entity that became available after startup."""
        # prefetch the device- and entity registry
        device_registry = {x["id"]: x for x in await self.hass_prov.hass.get_device_registry()}
        entity_registry = {
            x["entity_id"]: x for x in await self.hass_prov.hass.get_entity_registry()
        }
        async for state in _get_hass_media_players(self.hass_prov):
            if state["entity_id"] != entity_id:
                continue
            await self._setup_player(state, entity_registry, device_registry)
