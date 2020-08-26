#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import os
from typing import Any, List

from music_assistant.constants import (
    EVENT_HASS_ENTITY_CHANGED,
    EVENT_PLAYER_ADDED,
    EVENT_PLAYER_REMOVED,
    EVENT_PLAYER_UPDATED,
    CONF_NAME
)
from music_assistant.models.media_types import MediaItem, MediaType
from music_assistant.models.player import Player, PlayerState
from music_assistant.models.player_queue import QueueItem, QueueOption
from music_assistant.models.playerprovider import PlayerProvider
from music_assistant.models.provider import ProviderType
from music_assistant.utils import (
    LOGGER,
    callback,
    iter_items,
    try_parse_int,
    try_parse_float
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "playerproviders")


class PlayerManager:
    """several helpers to handle playback through player providers"""

    def __init__(self, mass):
        self.mass = mass
        self._players = {}
        self._providers = {}
        self._player_queues = {}

    async def async_setup(self):
        """Async initialize of module"""
        pass

    @property
    def players(self) -> List[Player]:
        """Return all registered players."""
        return list(self._players.values())

    @property
    def providers(self) -> List[PlayerProvider]:
        """Return all loaded player providers."""
        return self.mass.get_providers(ProviderType.PLAYER_PROVIDER)

    @callback
    def get_player(self, player_id: str) -> Player:
        """Return player by player_id or None if player does not exist."""
        return self._players.get(player_id)

    @callback
    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return provider by player_id or None if player does not exist."""
        player = self.get_player(player_id)
        return self.mass.get_provider(player.provider_id) if player else None

    # ADD/REMOVE/UPDATE HELPERS

    async def async_add_player(self, player: Player) -> None:
        """Register a new player or update an existing one."""
        assert player.player_id and player.player_id
        new_player = player.player_id not in self._players
        if new_player:
            # TODO: turn on player if it was previously turned on ?
            LOGGER.info("New player added: %s/%s", player.player_provider, player.player_id)
            self.mass.async_create_task(self.mass.signal_event(EVENT_PLAYER_ADDED, player))
        else:
            self.mass.async_create_task(self.mass.signal_event(EVENT_PLAYER_UPDATED, player))

    async def async_remove_player(self, player_id: str):
        """Remove a player from the registry."""
        player = self.get_player(player_id)
        if player:
            await player.async_on_remove()
        self._players.pop(player_id, None)
        LOGGER.info("Player removed: %s", player_id)
        self.mass.async_create_task(self.mass.signal_event(EVENT_PLAYER_REMOVED, {"player_id": player_id}))

    async def async_update_player(self, player: Player):
        """Update an existing player (or register as new if non existing)."""
        if not player.player_id in self._players:
            return await self.async_add_player(player)

    async def __create_player(self, player: Player):
        """Create internal Player object with all calculated properties."""
        final_player = Player(
            player_id=player.player_id,
            id=player.id,
            name = self.__get_player_name(player),
            powered = self.__get_player_power_state(player),
            elapsed_time = player.elapsed_time,
            state = self.__get_player_state(player),
            available=player.available,
            current_uri=player.current_uri,
            volume_level=self.__get_player_volume_level(player)
        )

    # SERVICE CALLS / PLAYER COMMANDS

    async def async_cmd_play_media(
        self, player_id: str, media_items: List[MediaItem], queue_opt: QueueOption = QueueOption.Play
    ):
        """
            Play media item(s) on the given player
                :param player_id: player_id of the player to handle the command.
                :param media_item: media item(s) that should be played (single item or list of items)
                :param queue_opt:
                QueueOption.Play -> Insert new items in queue and start playing at the inserted position
                QueueOption.Replace -> Replace queue contents with these items
                QueueOption.Next -> Play item(s) after current playing item
                QueueOption.Add -> Append new items at end of the queue
        """
        player = self._players[player_id]
        if not player:
            return
        # a single item or list of items may be provided
        queue_items = []
        for media_item in media_items:
            # collect tracks to play
            if media_item.media_type == MediaType.Artist:
                tracks = self.mass.music_manager.artist_toptracks(
                    media_item.item_id, provider=media_item.provider
                )
            elif media_item.media_type == MediaType.Album:
                tracks = self.mass.music_manager.album_tracks(
                    media_item.item_id, provider=media_item.provider
                )
            elif media_item.media_type == MediaType.Playlist:
                tracks = self.mass.music_manager.playlist_tracks(
                    media_item.item_id, provider=media_item.provider
                )
            else:
                tracks = iter_items(media_item)  # single track
            async for track in tracks:
                queue_item = QueueItem(track)
                # generate uri for this queue item
                queue_item.uri = "http://%s:%s/stream/%s/%s" % (
                    self.mass.web.local_ip,
                    self.mass.web.http_port,
                    player_id,
                    queue_item.queue_item_id,
                )
                queue_items.append(queue_item)

        # load items into the queue
        if queue_opt == QueueOption.Replace or (
            len(queue_items) > 10 and queue_opt in [QueueOption.Play, QueueOption.Next]
        ):
            return await player.queue.load(queue_items)
        elif queue_opt == QueueOption.Next:
            return await player.queue.insert(queue_items, 1)
        elif queue_opt == QueueOption.Play:
            return await player.queue.insert(queue_items, 0)
        elif queue_opt == QueueOption.Add:
            return await player.queue.append(queue_items)

    async def async_cmd_stop(self, player_id: str):
        """
            Send STOP command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        # TODO: redirect playback related commands to parent player
        # for group_id in self.group_parents:
        #     group_player = self.mass.player_manager.get_player_sync(group_id)
        #     if group_player.state != PlayerState.Off:
        #         return await group_player.stop()
        return await self.get_player_provider(player_id).async_cmd_stop(player_id)

    async def async_cmd_play(self, player_id: str):
        """
            Send STOP command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        return await self.get_player_provider(player_id).async_cmd_play(player_id)
        # TODO: redirect playback related commands to parent player ?
        # for group_id in self.group_parents:
        #     group_player = self.mass.player_manager.get_player_sync(group_id)
        #     if group_player.state != PlayerState.Off:
        #         return await group_player.play()
        # if self.state == PlayerState.Paused:
        #     return await self.cmd_play()
        # elif self.state != PlayerState.Playing:
        #     return await self.queue.resume()

    async def async_cmd_pause(self, player_id: str):
        """
            Send PAUSE command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        return await self.get_player_provider(player_id).async_cmd_pause(player_id)
        # TODO: redirect playback related commands to parent player
        # for group_id in self.group_parents:
        #     group_player = self.mass.player_manager.get_player_sync(group_id)
        #     if group_player.state != PlayerState.Off:
        #         return await group_player.pause()
        # return await self.cmd_pause()

    async def async_cmd_play_pause(self, player_id: str):
        """
            Toggle play/pause on given player.
                :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if player.state == PlayerState.Playing:
            return await self.async_cmd_pause(player_id)
        return await self.async_cmd_play(player_id)

    async def async_cmd_next(self, player_id: str):
        """
            Send NEXT TRACK command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        return await self.get_player_provider(player_id).async_cmd_next(player_id)
        # TODO: handle queue support and parent player command redirects
        # return await self.queue.play_index(self.queue.cur_index + 1)
        # return await player.async_play()
        # for group_id in self.group_parents:
        #     group_player = self.mass.player_manager.get_player_sync(group_id)
        #     if group_player.state != PlayerState.Off:
        #         return await group_player.next()
        # return await self.queue.next()

    async def async_cmd_previous(self, player_id: str):
        """
            Send PREVIOUS TRACK command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        return await self.get_player_provider(player_id).async_cmd_previous(player_id)
        # TODO: handle queue support and parent player command redirects
        # return await self.queue.play_index(self.queue.cur_index - 1)
        # for group_id in self.group_parents:
        #     group_player = self.mass.player_manager.get_player_sync(group_id)
        #     if group_player.state != PlayerState.Off:
        #         return await group_player.previous()
        # return await self.queue.previous()

    async def async_cmd_power_on(self, player_id: str):
        """
            Send POWER ON command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        player_config = self.mass.config.players[player.player_id]
        # turn on player
        await self.get_player_provider(player_id).async_cmd_power_on(player_id)
        # handle mute as power
        if player_config.get("mute_as_power"):
            await self.async_cmd_volume_mute(player_id, False)
        # handle hass integration
        # TODO: move to plugin construction
        if (
            self.mass.hass.enabled
            and player_config.get("hass_power_entity")
            and player_config.get("hass_power_entity_source")
        ):
            cur_source = await self.mass.hass.get_state_async(
                player_config["hass_power_entity"], attribute="source"
            )
            if not cur_source:
                service_data = {
                    "entity_id": player_config["hass_power_entity"],
                    "source": player_config["hass_power_entity_source"],
                }
                await self.mass.hass.call_service(
                    "media_player", "select_source", service_data
                )
        elif self.mass.hass.enabled and player_config.get("hass_power_entity"):
            domain = player_config["hass_power_entity"].split(".")[0]
            service_data = {"entity_id": player_config["hass_power_entity"]}
            await self.mass.hass.call_service(domain, "turn_on", service_data)
        # handle play on power on
        if player_config.get("play_power_on"):
            await self.get_player_provider(player_id).async_cmd_play(player_id)

    async def async_cmd_power_off(self, player_id: str):
        """
            Send POWER OFF command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        player_config = self.mass.config.players[player.player_id]
        # handle mute as power
        if player_config.get("mute_as_power"):
            await self.async_cmd_volume_mute(player_id, True)

        
        await self.get_player_provider(player_id).async_cmd_power_off(player_id)
        # handle hass integration
        if (
            self.mass.hass.enabled
            and player_config.get("hass_power_entity")
            and player_config.get("hass_power_entity_source")
        ):
            cur_source = await self.mass.hass.get_state_async(
                player_config["hass_power_entity"], attribute="source"
            )
            if cur_source == player_config["hass_power_entity_source"]:
                service_data = {"entity_id": player_config["hass_power_entity"]}
                await self.mass.hass.call_service(
                    "media_player", "turn_off", service_data
                )
        elif self.mass.hass.enabled and player_config.get("hass_power_entity"):
            domain = player_config["hass_power_entity"].split(".")[0]
            service_data = {"entity_id": player_config["hass_power_entity"]}
            await self.mass.hass.call_service(domain, "turn_off", service_data)
        # TODO: handle group power
        # if self.is_group:
        #     # player is group, turn off all childs
        #     for child_player_id in self.group_childs:
        #         child_player = await self.mass.player_manager.get_player(child_player_id)
        #         if child_player and child_player.powered:
        #             await child_player.power_off()
        # # if player has group parent(s), check if it should be turned off
        # for group_parent_id in self.group_parents:
        #     group_player = await self.mass.player_manager.get_player(group_parent_id)
        #     if group_player.state != PlayerState.Off:
        #         needs_power = False
        #         for child_player_id in group_player.group_childs:
        #             if child_player_id == self.player_id:
        #                 continue
        #             child_player = await self.mass.player_manager.get_player(child_player_id)
        #             if child_player and child_player.powered:
        #                 needs_power = True
        #                 break
        #         if not needs_power:
        #             await group_player.power_off()

    async def async_cmd_volume_set(self, player_id: str, volume_level: int):
        """
            Send volume level command to given player.
                :param player_id: player_id of the player to handle the command.
                :param volume_level: volume level to set (0..100).
        """
        player = self.get_player(player_id)
        player_config = self.mass.config.players[player.player_id]
        volume_level = try_parse_int(volume_level)
        if volume_level < 0:
            volume_level = 0
        elif volume_level > 100:
            volume_level = 100
        # handle group volume
        if player.is_group:
            cur_volume = player.volume_level
            new_volume = volume_level
            volume_dif = new_volume - cur_volume
            if cur_volume == 0:
                volume_dif_percent = 1 + (new_volume / 100)
            else:
                volume_dif_percent = volume_dif / cur_volume
            for child_player_id in player.group_childs:
                child_player = self._players.get(child_player_id)
                if child_player and child_player.available and child_player.powered:
                    cur_child_volume = child_player.volume_level
                    new_child_volume = cur_child_volume + (
                        cur_child_volume * volume_dif_percent
                    )
                    await self.async_cmd_volume_set(child_player_id, new_child_volume)
        # handle hass integration
        # TODO: move to plugin like implementation
        elif self.mass.hass.enabled and player_config.get("hass_volume_entity"):
            service_data = {
                "entity_id": player_config["hass_volume_entity"],
                "volume_level": volume_level / 100,
            }
            await self.mass.hass.call_service(
                "media_player", "volume_set", service_data
            )
            # just force full volume on actual player if volume is outsourced to hass
            await self.async_cmd_volume_set(player_id, 100)
        else:
            await self.async_cmd_volume_set(player_id, volume_level)

    async def async_cmd_volume_up(self, player_id: str):
        """
            Send volume UP command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        new_level = player.volume_level + 1
        if new_level > 100:
            new_level = 100
        return await self.async_cmd_volume_set(player_id, new_level)

    async def async_cmd_volume_down(self, player_id: str):
        """
            Send volume DOWN command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        new_level = player.volume_level - 1
        if new_level < 0:
            new_level = 0
        return await self.async_cmd_volume_set(player_id, new_level)

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
            Send MUTE command to given player.
                :param player_id: player_id of the player to handle the command.
                :param is_muted: bool with the new mute state.
        """
        player = self.get_player(player_id)
        await player.async_power_on()
        return await self.async_cmd_volume_mute(is_muted)

    async def async_cmd_queue_play_index(self, player_id: str, index: int):
        """
            Play item at index X on player's queue
            :attrib index: (int) index of the queue item that should start playing
        """
        item = self.queue.get_item(index)
        if item:
            return await self.async_play_uri(item.uri)

    @callback
    def __get_player_name(self, player: Player):
        """Get final/calculated player name."""
        return self.mass.config.players[player.player_id].get(CONF_NAME, player.name)

    @callback
    def __get_player_power_state(self, player: Player):
        """Get final/calculated player's power state."""
        if not player.available:
            return False
        player_config = self.mass.config.players[player.player_id]
        # homeassistant integration
        # TODO: move to plugin-like structure (volume_controls and power_controls)
        if (
            self.mass.hass.enabled
            and player_config.get("hass_power_entity")
            and player_config.get("hass_power_entity_source")
        ):
            hass_state = self.mass.hass.get_state(
                player_config["hass_power_entity"], attribute="source"
            )
            return hass_state == player_config["hass_power_entity_source"]
        if self.mass.hass.enabled and player_config.get("hass_power_entity"):
            hass_state = self.mass.hass.get_state(player_configs["hass_power_entity"])
            return hass_state != "off"
        # mute as power
        if player_config.get("mute_as_power"):
            return not player.muted
        return player.powered

    @callback
    def __get_player_volume_level(self, player: Player):
        """Get final/calculated player's volume_level."""
        if not player.available:
            return 0
        player_config = self.mass.config.players[player.player_id]
        # handle group volume
        if player.is_group_player:
            group_volume = 0
            active_players = 0
            for child_player_id in player.group_childs:
                child_player = self._players.get(child_player_id)
                if child_player and child_player.available and child_player.powered:
                    group_volume += child_player.volume_level
                    active_players += 1
            if active_players:
                group_volume = group_volume / active_players
            return group_volume
        # handle hass integration
        # TODO: move into a plugin-like construction
        if self.mass.hass.enabled and player_config.get("hass_volume_entity"):
            hass_state = self.mass.hass.get_state(
                player_config["hass_volume_entity"], attribute="volume_level"
            )
            return int(try_parse_float(hass_state) * 100)
        return player.volume_level
    
    @callback
    def __get_player_state(self, player: Player):
        """Get final/calculated player's state."""
        if not player.available or not player.powered:
            return PlayerState.Off
        # TODO: prefer group player state
        # for group_parent_id in self.group_parents:
        #     group_player = self.mass.player_manager.get_player_sync(group_parent_id)
        #     if group_player and group_player.state != PlayerState.Off:
        #         return group_player.state
        return player.state

    

    
    def __update_player_settings(self):
        """[PROTECTED] update player config settings"""
        player_settings = self.mass.config["player_settings"].get(self.player_id, {})
        # generate config for the player
        config_entries = [  # default config entries for a player
            ("enabled", True, "player_enabled"),
            ("name", "", "player_name"),
            ("mute_as_power", False, "player_mute_power"),
            ("max_sample_rate", 96000, "max_sample_rate"),
            ("volume_normalisation", True, "enable_r128_volume_normalisation"),
            ("target_volume", "-23", "target_volume_lufs"),
            ("fallback_gain_correct", "-12", "fallback_gain_correct"),
            ("crossfade_duration", 0, "crossfade_duration"),
            ("play_power_on", False, "player_power_play"),
        ]
        # append player specific settings
        config_entries += self.mass.player_manager.providers[
            self._prov_id
        ].player_config_entries
        # hass integration
        if self.mass.config["base"].get("homeassistant", {}).get("enabled"):
            # append hass specific config entries
            config_entries += [
                ("hass_power_entity", "", "hass_player_power"),
                ("hass_power_entity_source", "", "hass_player_source"),
                ("hass_volume_entity", "", "hass_player_volume"),
            ]
        # pylint: disable=unused-variable
        for key, def_value, desc in config_entries:
            if not key in player_settings:
                if isinstance(def_value, str) and def_value.startswith("<"):
                    player_settings[key] = None
                else:
                    player_settings[key] = def_value
        # pylint: enable=unused-variable
        self.mass.config["player_settings"][self.player_id] = player_settings
        self.mass.config["player_settings"][self.player_id]["__desc__"] = config_entries

    # async def async_handle_mass_events(self, msg, msg_details=None):
    #     SHOULD BE MOVED TO HOMEASSISTANT PLUGIN
    #     """Listen to some events on event bus"""
    #     if msg == EVENT_HASS_ENTITY_CHANGED:
    #         # handle players with hass integration enabled
    #         player_ids = [player.player_id for player in self.players]
    #         for player_id in player_ids:
    #             player = self._players[player_id]
    #             if msg_details["entity_id"] == player.settings.get(
    #                 "hass_power_entity"
    #             ) or msg_details["entity_id"] == player.settings.get(
    #                 "hass_volume_entity"
    #             ):
    #                 await player.update()

    async def async_get_gain_correct(self, player_id, item_id, id):
        """get gain correction for given player / track combination"""
        player = self._players[player_id]
        if not player.settings["volume_normalisation"]:
            return 0
        target_gain = int(player.settings["target_volume"])
        fallback_gain = int(player.settings["fallback_gain_correct"])
        track_loudness = await self.mass.database.get_track_loudness(item_id, id)
        if track_loudness is None:
            gain_correct = fallback_gain
        else:
            gain_correct = target_gain - track_loudness
        gain_correct = round(gain_correct, 2)
        LOGGER.debug(
            "Loudness level for track %s/%s is %s - calculated replayGain is %s",
            id,
            item_id,
            track_loudness,
            gain_correct,
        )
        return gain_correct


# @property
    # def group_parents(self):
    #     """[PROTECTED] player ids of all groups this player belongs to"""
    #     player_ids = []
    #     for item in self.mass.player_manager.players:
    #         if self.player_id in item.group_childs:
    #             player_ids.append(item.player_id)
    #     return player_ids

    # @property
    # def group_childs(self) -> list:
    #     """
    #         [PROTECTED]
    #         return all child player ids for this group player as list
    #         empty list if this player is not a group player
    #     """
    #     return self._group_childs

    # @group_childs.setter
    # def group_childs(self, group_childs: list):
    #     """[PROTECTED] set group_childs property of this player."""
    #     if group_childs != self._group_childs:
    #         self._group_childs = group_childs
    #         self.mass.loop.create_task(self.update())
    #         for child_player_id in group_childs:
    #             self.mass.loop.create_task(
    #                 self.mass.player_manager.trigger_update(child_player_id)
    #             )

    # def add_group_child(self, child_player_id):
    #     """add player as child to this group player."""
    #     if not child_player_id in self._group_childs:
    #         self._group_childs.append(child_player_id)
    #         self.mass.loop.create_task(self.update())
    #         self.mass.loop.create_task(
    #             self.mass.player_manager.trigger_update(child_player_id)
    #         )

    # def remove_group_child(self, child_player_id):
    #     """remove player as child from this group player."""
    #     if child_player_id in self._group_childs:
    #         self._group_childs.remove(child_player_id)
    #         self.mass.loop.create_task(self.update())
    #         self.mass.loop.create_task(
    #             self.mass.player_manager.trigger_update(child_player_id)
    #         )

    


    # @property
    # def cur_time(self):
    #     """[PROTECTED] cur_time (player's elapsed time) property of this player."""
    #     # prefer group player state
    #     for group_id in self.group_parents:
    #         group_player = self.mass.player_manager.get_player_sync(group_id)
    #         if group_player.state != PlayerState.Off:
    #             return group_player.cur_time
    #     return self._cur_time

    # @cur_time.setter
    # def cur_time(self, cur_time: int):
    #     """[PROTECTED] set cur_time (player's elapsed time) property of this player."""
    #     if cur_time is None:
    #         cur_time = 0
    #     if cur_time != self._cur_time:
    #         self._cur_time = cur_time
    #         self._media_position_updated_at = time.time()
    #         self.mass.loop.create_task(self.update())

    # @property
    # def media_position_updated_at(self):
    #     """[PROTECTED] When was the position of the current playing media valid."""
    #     return self._media_position_updated_at

    # @property
    # def cur_uri(self):
    #     """[PROTECTED] cur_uri (uri loaded in player) property of this player."""
    #     # prefer group player's state
    #     for group_id in self.group_parents:
    #         group_player = self.mass.player_manager.get_player_sync(group_id)
    #         if group_player.state != PlayerState.Off:
    #             return group_player.cur_uri
    #     return self._cur_uri

    # @cur_uri.setter
    # def cur_uri(self, cur_uri: str):
    #     """[PROTECTED] set cur_uri (uri loaded in player) property of this player."""
    #     if cur_uri != self._cur_uri:
    #         self._cur_uri = cur_uri
    #         self.mass.loop.create_task(self.update())


    # @volume_level.setter
    # def volume_level(self, volume_level: int):
    #     """[PROTECTED] set volume_level property of this player."""
    #     volume_level = try_parse_int(volume_level)
    #     if volume_level != self._volume_level:
    #         self._volume_level = volume_level
    #         self.mass.loop.create_task(self.update())
    #         # trigger update on group player
    #         for group_parent_id in self.group_parents:
    #             self.mass.loop.create_task(
    #                 self.mass.player_manager.trigger_update(group_parent_id)
    #             )


    # @muted.setter
    # def muted(self, is_muted: bool):
    #     """[PROTECTED] set muted property of this player."""
    #     is_muted = try_parse_bool(is_muted)
    #     if is_muted != self._muted:
    #         self._muted = is_muted
    #         self.mass.loop.create_task(self.update())

    # @property
    # def queue(self):
    #     """[PROTECTED] player's queue"""
    #     # prefer group player's state
    #     for group_id in self.group_parents:
    #         group_player = self.mass.player_manager.get_player_sync(group_id)
    #         if group_player.state != PlayerState.Off:
    #             return group_player.queue
    #     return self._queue

    # async def update(self, force=False):
    #     """[PROTECTED] signal player updated"""
    #     if not force and (not self.initialized or not self.enabled):
    #         return
    #     # update queue state if player state changes
    #     await self.queue.update_state()
    #     await self.mass.signal_event(EVENT_PLAYER_CHANGED, self.to_dict())

    # @property
    # def settings(self):
    #     """[PROTECTED] get player config settings"""
    #     if self.player_id in self.mass.config["player_settings"]:
    #         return self.mass.config["player_settings"][self.player_id]
    #     else:
    #         self.__update_player_settings()
    #         return self.mass.config["player_settings"][self.player_id]

    # def to_dict(self):
    #     """instance attributes as dict so it can be serialized to json"""
    #     return {
    #         "player_id": self.player_id,
    #         "player_provider": self.player_provider,
    #         "name": self.name,
    #         "is_group": self.is_group,
    #         "state": self.state,
    #         "powered": self.powered,
    #         "cur_time": self.cur_time,
    #         "media_position_updated_at": self.media_position_updated_at,
    #         "cur_uri": self.cur_uri,
    #         "volume_level": self.volume_level,
    #         "muted": self.muted,
    #         "group_parents": self.group_parents,
    #         "group_childs": self.group_childs,
    #         "enabled": self.enabled,
    #         "supports_queue": self.supports_queue,
    #         "supports_gapless": self.supports_gapless,
    #     }

