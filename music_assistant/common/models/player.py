"""Model(s) for Player."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from mashumaro import DataClassDictMixin

from .enums import PlayerFeature, PlayerState, PlayerType


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "unknown"
    address: str = "unknown"
    manufacturer: str = "unknown"


@dataclass
class Player(DataClassDictMixin):
    """Representation of a Player within Music Assistant."""

    player_id: str
    provider: str
    type: PlayerType
    name: str
    available: bool
    powered: bool
    device_info: DeviceInfo
    supported_features: tuple[PlayerFeature, ...] = field(default=tuple())

    elapsed_time: float = 0
    elapsed_time_last_updated: float = time.time()
    current_url: str = ""
    state: PlayerState = PlayerState.IDLE

    volume_level: int = 100
    volume_muted: bool = False

    # group_childs: Return list of player group child id's or synced childs.
    # - If this player is a dedicated group player,
    #   returns all child id's of the players in the group.
    # - If this is a syncgroup of players from the same platform (e.g. sonos),
    #   this will return the id's of players synced to this player.
    group_childs: list[str] = field(default_factory=list)

    # active_queue: return player_id of the active queue for this player
    # if the player is grouped and a group is active, this will be set to the group's player_id
    # otherwise it will be set to the own player_id
    active_queue: str = ""

    # can_sync_with: return tuple of player_ids that can be synced to/with this player
    # ususally this is just a list of all player_ids within the playerprovider
    can_sync_with: tuple[str, ...] = field(default=tuple())

    # synced_to: plauyer_id of the player this player is currently sunced to
    # also referred to as "sync master"
    synced_to: str | None = None

    # max_sample_rate: maximum supported sample rate the player supports
    max_sample_rate: int = 96000

    # enabled: if the player is enabled
    # will be set by the player manager based on config
    # a disabled player is hidden in the UI and updates will not be processed
    enabled: bool = True

    @property
    def corrected_elapsed_time(self) -> float:
        """Return the corrected/realtime elapsed time."""
        if self.state == PlayerState.PLAYING:
            return self.elapsed_time + (time.time() - self.elapsed_time_last_updated)
        return self.elapsed_time
