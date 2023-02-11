"""Logic to play music from MusicProviders to supported players."""
from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING, Iterator

from music_assistant.common.helpers.util import get_changed_keys
from music_assistant.common.models.enums import (
    ContentType,
    EventType,
    MediaType,
    PlayerState,
    QueueOption,
    RepeatMode,
)
from music_assistant.common.models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    QueueEmpty,
)
from music_assistant.common.models.media_items import MediaItemType, media_from_dict
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import ROOT_LOGGER_NAME
from music_assistant.server.helpers.api import api_command

if TYPE_CHECKING:
    from .players import PlayerController
    from music_assistant.common.models.player import Player

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.players.queue")


class PlayerQueuesController:
    """Controller holding all logic to enqueue music for players."""

    def __init__(self, players: PlayerController) -> None:
        """Initialize class."""
        self.players = players
        self.mass = players.mass
        self._queues: dict[str, PlayerQueue] = {}
        self._queue_items: dict[str, list[QueueItem]] = {}
        self._prev_states: dict[str, dict] = {}

    def __iter__(self) -> Iterator[PlayerQueue]:
        """Iterate over (available) players."""
        return iter(self._queues.values())

    @api_command("players/queue/all")
    def all(self) -> tuple[PlayerQueue]:
        """Return all registered PlayerQueues."""
        return tuple(self._queues.values())

    @api_command("players/queue/get")
    def get(self, queue_id: str) -> PlayerQueue | None:
        """Return PlayerQueue by queue_id or None if not found."""
        return self._queues.get(queue_id)

    @api_command("players/queue/items")
    def items(self, queue_id: str) -> list[QueueItem]:
        """Return all QueueItems for given PlayerQueue."""
        return self._queue_items.get(queue_id, [])

    # Queue commands

    @api_command("players/queue/play_media")
    async def play_media(
        self,
        queue_id: str,
        media: MediaItemType | list[MediaItemType] | str | list[str],
        option: QueueOption = QueueOption.PLAY,
        radio_mode: bool = False,
    ) -> str:
        """
        Play media item(s) on the given queue.

        - media: Media that should be played (MediaItem(s) or uri's).
        - queue_opt: Which enqueue mode to use.
        - radio_mode: Enable radio mode for the given item(s).
        """
        queue = self._queues[queue_id]
        if queue.announcement_in_progress:
            LOGGER.warning("Ignore queue command: An announcement is in progress")
            return

        # a single item or list of items may be provided
        if not isinstance(media, list):
            media = [media]

        # clear queue first if it was finished
        if queue.current_index and queue.current_index >= (
            len(self._queue_items[queue_id]) - 1
        ):
            queue.current_index = None
            self._queue_items[queue_id] = []

        # clear radio source items if needed
        if option not in (QueueOption.ADD, QueueOption.PLAY, QueueOption.NEXT):
            queue.radio_source = []

        tracks: list[MediaItemType] = []
        for item in media:
            # parse provided uri into a MA MediaItem or Basic QueueItem from URL
            if isinstance(item, str):
                try:
                    media_item = await self.mass.music.get_item_by_uri(item)
                except MusicAssistantError as err:
                    # invalid MA uri or item not found error
                    raise MediaNotFoundError(f"Invalid uri: {item}") from err
            elif isinstance(item, dict):
                media_item = media_from_dict(item)
            else:
                media_item = item

            # collect tracks to play
            ctrl = self.mass.music.get_controller(media_item.media_type)
            if radio_mode:
                queue.radio_source.append(media_item)
                # if radio mode enabled, grab the first batch of tracks here
                tracks += await ctrl.dynamic_tracks(
                    item_id=media_item.item_id, provider_domain=media_item.provider
                )
            elif media_item.media_type in (
                MediaType.ARTIST,
                MediaType.ALBUM,
                MediaType.PLAYLIST,
            ):
                tracks += await ctrl.tracks(
                    media_item.item_id, provider_domain=media_item.provider
                )
            else:
                # single track or radio item
                tracks += [media_item]

        # only add valid/available items
        queue_items = [
            QueueItem.from_media_item(queue_id, x) for x in tracks if x and x.available
        ]

        # load the items into the queue
        cur_index = queue.index_in_buffer or queue.current_index or 0
        shuffle = queue.shuffle_enabled and len(queue_items) >= 5

        # handle replace: clear all items and replace with the new items
        if option == QueueOption.REPLACE:
            await self.clear(queue_id)
            await self.load(queue_id, queue_items=queue_items, shuffle=shuffle)
            await self.play_index(queue_id, 0)
        # handle next: add item(s) in the index next to the playing/loaded/buffered index
        elif option == QueueOption.NEXT:
            await self.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=cur_index + 1,
                shuffle=shuffle,
            )
        elif option == QueueOption.REPLACE_NEXT:
            self.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=cur_index + 1,
                keep_remaining=False,
                shuffle=shuffle,
            )
        # handle play: replace current loaded/playing index with new item(s)
        elif option == QueueOption.PLAY:
            self.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=cur_index,
                shuffle=shuffle,
            )
            await self.play_index(queue_id, cur_index)
        # handle add: add/append item(s) to the remaining queue items
        elif option == QueueOption.ADD:
            if queue.shuffle_enabled:
                # shuffle the new items with remaining queue items
                insert_at_index = cur_index + 1
            else:
                # just append at the end
                insert_at_index = len(self._queue_items[queue_id])
            self.load(
                queue_id=queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                shuffle=queue.shuffle_enabled,
            )

    @api_command("players/queue/move_item")
    def move_item(self, queue_id: str, queue_item_id: str, pos_shift: int = 1) -> None:
        """
        Move queue item x up/down the queue.

        - queue_id: id of the queue to process this request.
        - queue_item_id: the item_id of the queueitem that needs to be moved.
        - pos_shift: move item x positions down if positive value
        - pos_shift: move item x positions up if negative value
        - pos_shift:  move item to top of queue as next item if 0
        """
        queue = self._queues[queue_id]
        item_index = self.index_by_id(queue_id, queue_item_id)
        if item_index <= queue.index_in_buffer:
            raise IndexError(f"{item_index} is already played/buffered")

        queue_items = self._queue_items[queue_id]
        queue_items = queue_items.copy()

        if pos_shift == 0 and queue.state == PlayerState.PLAYING:
            new_index = (queue.current_index or 0) + 1
        elif pos_shift == 0:
            new_index = queue.current_index or 0
        else:
            new_index = item_index + pos_shift
        if (new_index < (queue.current_index or 0)) or (new_index > len(queue_items)):
            return
        # move the item in the list
        queue_items.insert(new_index, queue_items.pop(item_index))
        self.update_items(queue_id, queue_items)

    @api_command("players/queue/delete_item")
    def delete_item(self, queue_id: str, item_id_or_index: int | str) -> None:
        """Delete item (by id or index) from the queue."""
        if isinstance(item_id_or_index, str):
            item_index = self.index_by_id(item_id_or_index)
        else:
            item_index = item_id_or_index
        queue = self._queues[queue_id]
        if item_index <= queue.index_in_buffer:
            # ignore request if track already loaded in the buffer
            # the frontend should guard so this is just in case
            LOGGER.warning("delete requested for item already loaded in buffer")
            return
        queue_items = self._queue_items[queue_id]
        queue_items.pop(item_index)
        self.update_items(queue_id, queue_items)

    @api_command("players/queue/clear")
    def clear(self, queue_id: str) -> None:
        """Clear all items in the queue."""
        queue = self._queues[queue_id]
        queue.radio_source = []
        if queue.state not in (PlayerState.IDLE, PlayerState.OFF):
            self.mass.create_task(self.stop())
        self.update_items(queue_id, [])

    @api_command("players/queue/stop")
    async def stop(self, queue_id: str) -> None:
        """
        Handle STOP command for given queue.
            - queue_id: queue_id of the playerqueue to handle the command.
        """
        if self._queues[queue_id].announcement_in_progress:
            LOGGER.warning(
                "Ignore queue command for %s because an announcement is in progress."
            )
            return
        # simply forward the command to underlying player
        await self.players.cmd_stop(queue_id)

    @api_command("players/queue/play")
    async def play(self, queue_id: str) -> None:
        """
        Handle PLAY command for given queue.
            - queue_id: queue_id of the playerqueue to handle the command.
        """
        if self._queues[queue_id].announcement_in_progress:
            LOGGER.warning(
                "Ignore queue command for %s because an announcement is in progress."
            )
            return
        if self._queues[queue_id].state == PlayerState.PAUSED:
            # simply forward the command to underlying player
            await self.players.cmd_play(queue_id)
        else:
            await self.resume(queue_id)

    @api_command("players/queue/pause")
    async def pause(self, queue_id: str) -> None:
        """
        Handle PAUSE command for given queue.
            - queue_id: queue_id of the playerqueue to handle the command.
        """
        if self._queues[queue_id].announcement_in_progress:
            LOGGER.warning(
                "Ignore queue command for %s because an announcement is in progress."
            )
            return
        # simply forward the command to underlying player
        await self.players.cmd_pause(queue_id)

    @api_command("players/queue/play_pause")
    async def play_pause(self, queue_id: str) -> None:
        """
        Toggle play/pause on given playerqueue.
            - queue_id: queue_id of the queue to handle the command.
        """
        if self._queues[queue_id].state == PlayerState.PLAYING:
            await self.pause()
            return
        await self.play(queue_id)

    @api_command("players/queue/next")
    async def next(self, queue_id: str) -> None:
        """
        Handle NEXT TRACK command for given queue.
            - queue_id: queue_id of the queue to handle the command.
        """
        current_index = self._queues[queue_id].current_index
        next_index = self.get_next_index(queue_id, current_index, True)
        if next_index is None:
            return None
        await self.play_index(queue_id, next_index)

    @api_command("players/queue/previous")
    async def previous(self, queue_id: str) -> None:
        """
        Handle PREVIOUS TRACK command for given queue.
            - queue_id: queue_id of the queue to handle the command.
        """
        current_index = self._queues[queue_id].current_index
        if current_index is None:
            return
        await self.play_index(queue_id, max(current_index - 1, 0))

    @api_command("players/queue/skip")
    async def skip(self, queue_id: str, seconds: int = 10) -> None:
        """
        Handle SKIP command for given queue.

            - queue_id: queue_id of the queue to handle the command.
            - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        await self.seek(queue_id, self._queues[queue_id].elapsed_time + seconds)

    @api_command("players/queue/seek")
    async def seek(self, queue_id: str, position: int = 10) -> None:
        """
        Handle SEEK command for given queue.

            - queue_id: queue_id of the queue to handle the command.
            - position: position in seconds to seek to in the current playing item.
        """
        queue = self._queues[queue_id]
        assert queue.current_item, "No item loaded"
        assert queue.current_item.media_item.media_type == MediaType.TRACK
        assert queue.current_item.duration
        assert position < queue.current_item.duration
        await self.play_index(queue_id, queue.current_index, position)

    @api_command("players/queue/resume")
    async def resume(self, queue_id: str) -> None:
        """
        Handle RESUME command for given queue.

            - queue_id: queue_id of the queue to handle the command.
        """
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        resume_item = queue.current_item
        next_item = queue.next_item
        resume_pos = queue.elapsed_time
        if (
            resume_item
            and next_item
            and resume_item.duration
            and resume_pos > (resume_item.duration * 0.9)
        ):
            # track is already played for > 90% - skip to next
            resume_item = next_item
            resume_pos = 0
        elif queue.current_index is None and len(queue_items) > 0:
            # items available in queue but no previous track, start at 0
            resume_item = self.get_item(queue_id, 0)
            resume_pos = 0

        if resume_item is not None:
            resume_pos = resume_pos if resume_pos > 10 else 0
            fade_in = resume_pos > 0
            await self.play_index(queue_id, resume_item.item_id, resume_pos, fade_in)
        else:
            raise QueueEmpty(f"Resume queue requested but queue {queue_id} is empty")

    @api_command("players/queue/play_index")
    async def play_index(
        self,
        queue_id: str,
        index: int | str,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Play item at index (or item_id) X in queue."""
        queue = self._queues[queue_id]
        if queue.announcement_in_progress:
            LOGGER.warning(
                "Ignore queue command for %s because an announcement is in progress."
            )
            return
        queue_item = self.get_item(queue_id, index)
        if queue_item is None:
            raise FileNotFoundError(f"Unknown index/id: {index}")
        queue.current_index = index
        queue.index_in_buffer = index
        # execute the play_media command on the player(s)
        player_prov = self.mass.players.get_player_provider(queue_id)
        await player_prov.cmd_play_media(
            queue_id,
            queue_item=queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
        )

    # Interaction with player

    async def on_player_register(self, player: Player) -> None:
        """Register PlayerQueue for given player/queue id."""
        queue_id = player.player_id
        # try to restore previous state
        if prev_state := await self.mass.cache.get(f"queue.state.{queue_id}"):
            queue = PlayerQueue.from_dict(prev_state)
            prev_items = await self.mass.cache.get(
                f"queue.items.{queue_id}", default=[]
            )
            queue_items = [QueueItem.from_dict(x) for x in prev_items]
        else:
            queue = PlayerQueue(
                queue_id=queue_id,
                active=False,
                display_name=player.display_name,
                available=player.available,
                items=0,
            )
            queue_items = []

        self._queues[queue_id] = queue
        self._queue_items[queue_id] = queue_items
        # always call update to calculate state etc
        self.on_player_update(player)

    def on_player_update(self, player: Player) -> None:
        """Call when a PlayerQueue needs to be updated (e.g. when player updates)."""
        if player.player_id not in self._queues:
            self.mass.create_task(self.on_player_register(player))
            return
        queue_id = player.player_id
        player = self.players.get(queue_id)
        queue = self._queues[queue_id]
        # queue is active when underlying player has the queue_id loaded as stream url
        queue.active = f"/{queue_id}/" in player.current_url
        queue.display_name = player.display_name
        queue.available = player.available
        queue.items = len(self._queue_items[queue_id])

        if queue.active:
            queue.state = player.state
            queue.elapsed_time = int(player.corrected_elapsed_time)
            queue.elapsed_time_last_updated = time.time()
            queue.current_item = self.get_item(queue_id, queue.current_index)
            next_index = self.get_next_index(queue_id, queue.current_index)
            queue.next_item = self.get_item(queue_id, next_index)
        else:
            queue.state = PlayerState.IDLE
            queue.current_item = None
            queue.next_item = None

        # basic throttle: do not send state changed events if queue did not actually change
        prev_state = self._prev_states.get(queue_id, {})
        new_state = self._queues[queue_id].to_dict()
        self._prev_states[queue_id] = new_state
        changed_keys = get_changed_keys(prev_state, new_state)

        if len(changed_keys) == 0:
            return

        if "elapsed_time" in changed_keys:
            self.mass.signal_event(
                EventType.QUEUE_TIME_UPDATED, object_id=queue_id, data=queue
            )
        elif changed_keys != {"elapsed_time", "elapsed_time_last_updated"}:
            self.mass.signal_event(
                EventType.QUEUE_UPDATED, object_id=queue_id, data=queue
            )
        # watch dynamic radio items refill if needed
        if "current_index" in changed_keys:
            fill_index = len(self._queue_items[queue_id]) - 5
            if queue.radio_source and (queue.current_index >= fill_index):
                self.mass.create_task(self._fill_radio_tracks(queue_id))

    def on_player_remove(self, player_id: str) -> None:
        """Call when a player is removed from the registry."""
        self.mass.create_task(self.mass.cache.delete(f"queue.state.{player_id}"))
        self.mass.create_task(self.mass.cache.delete(f"queue.items.{player_id}"))
        self._queues.pop(player_id, None)
        self._queue_items.pop(player_id, None)

    async def resolve_stream(
        self,
        queue_item: QueueItem,
        player_id: str,
        content_type: ContentType = ContentType.WAV,
    ) -> str:
        """
        Resolve the stream URL for the given QueueItem.

        This is called just-in-time by the player implementation to get the URL to the audio.
        - queue_item: the QueueItem that is about to be played (or buffered).
        - player_id: the player_id of the player that will play the stream.
          In case of a multi subscriber stream (e.g. sync/groups),
          call resolve for every child player.
        - content_type: Encode the stream in the given format.
        """

    # Main queue manipulation methods

    def load(
        self,
        queue_id: str,
        queue_items: list[QueueItem],
        insert_at_index: int = 0,
        keep_remaining: bool = True,
        shuffle: bool = False,
    ) -> None:
        """
        Load new items at index.

        - queue_id: id of the queue to process this request.
        - queue_items: a list of QueueItems
        - insert_at_index: insert the item(s) at this index
        - keep_remaining: keep the remaining items after the insert
        - shuffle: (re)shuffle the items after insert index
        """

        # keep previous/played items, append the new ones
        prev_items = self._queue_items[queue_id][:insert_at_index]
        next_items: list[QueueItem] = []

        # if keep_remaining, append the old previous items
        if keep_remaining:
            next_items += queue_items[insert_at_index:]

        # we set the original insert order as attribute so we can un-shuffle
        for index, item in enumerate(next_items):
            item.sort_index += insert_at_index + index
        # (re)shuffle the final batch if needed
        if shuffle:
            next_items = random.sample(next_items, len(next_items))
        self.update_items(queue_id, prev_items + next_items)

    def update_items(self, queue_id: str, queue_items: list[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        self._queue_items[queue_id] = queue_items
        self.signal_update(queue_id, True)

    # Helper methods

    def get_item(
        self, queue_id: str, item_id_or_index: int | str | None
    ) -> QueueItem | None:
        """Get queue item by index or item_id."""
        if item_id_or_index is None:
            return None
        queue_items = self._queue_items[queue_id]
        if isinstance(item_id_or_index, int) and len(queue_items) > item_id_or_index:
            return queue_items[item_id_or_index]
        if isinstance(item_id_or_index, str):
            return next(
                (x for x in queue_items if x.queue_item_id == item_id_or_index), None
            )
        return None

    def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None:
        """Get index by queue_item_id."""
        queue_items = self._queue_items[queue_id]
        for index, item in enumerate(queue_items):
            if item.queue_item_id == queue_item_id:
                return index
        return None

    def get_next_index(
        self, queue_id: str, cur_index: int | None, is_skip: bool = False
    ) -> int:
        """Return the next index for the queue, accounting for repeat settings."""
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        # handle repeat single track
        if queue.repeat_mode == RepeatMode.ONE and not is_skip:
            return cur_index
        # handle repeat all
        if (
            queue.repeat_mode == RepeatMode.ALL
            and queue_items
            and cur_index == (len(queue_items) - 1)
        ):
            return 0
        # simply return the next index. other logic is guarded to detect the index
        # being higher than the number of items to detect end of queue and/or handle repeat.
        if cur_index is None:
            return 0
        next_index = cur_index + 1
        return next_index

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        """Signal state changed of given queue."""
        queue = self._queues[queue_id]
        if items_changed:
            self.mass.signal_event(
                EventType.QUEUE_ITEMS_UPDATED, object_id=queue_id, data=queue
            )
            # save items in cache
            self.mass.create_task(
                self.mass.cache.set(
                    f"queue.items.{queue_id}",
                    [x.to_dict() for x in self._queue_items[queue_id]],
                )
            )

        # always send the base event
        self.mass.signal_event(EventType.QUEUE_UPDATED, object_id=queue_id, data=queue)
        # save state
        self.mass.create_task(
            self.mass.cache.set(
                f"queue.state.{queue_id}",
                queue.to_dict(),
            )
        )

    async def _fill_radio_tracks(self, queue_id: str) -> None:
        """Fill a Queue with (additional) Radio tracks."""
        queue = self._queues[queue_id]
        assert queue.radio_source, "No Radio item(s) loaded/active!"
        tracks: list[MediaItemType] = []
        # grab dynamic tracks for (all) source items
        # shuffle the source items, just in case
        for radio_item in random.sample(queue.radio_source, len(queue.radio_source)):
            ctrl = self.mass.music.get_controller(radio_item.media_type)
            tracks += await ctrl.dynamic_tracks(
                item_id=radio_item.item_id, provider_domain=radio_item.provider
            )
            # make sure we do not grab too much items
            if len(tracks) >= 50:
                break
        # fill queue - filter out unavailable items
        queue_items = [
            QueueItem.from_media_item(queue_id, x) for x in tracks if x.available
        ]
        self.load(
            queue_id,
            queue_items,
            insert_at_index=len(self._queue_items[queue_id]) - 1,
        )
