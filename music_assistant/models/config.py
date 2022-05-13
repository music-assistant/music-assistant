"""Model for the Music Assisant runtime config."""

import os
from dataclasses import dataclass, field
from typing import List, Optional

from databases import DatabaseURL

from music_assistant.helpers.util import get_ip, select_stream_port
from music_assistant.models.enums import ProviderType


@dataclass(frozen=True)
class MusicProviderConfig:
    """Base Model for a MusicProvider config."""

    type: ProviderType
    enabled: bool = True
    username: Optional[str] = None
    password: Optional[str] = None
    path: Optional[str] = None
    # no need to override the id unless you really know what you're doing ;-)
    id: Optional[str] = None

    def __post_init__(self):
        """Call after init."""
        # create a default (hopefully unique enough) id from type + username/path
        if not self.id and self.path:
            prov_id = f"{self.type.value}_"
            prov_id += "".join((x[0] for x in self.path.lower().split(os.sep) if x))
            super().__setattr__("id", prov_id)
            self.__dict__["id"] = prov_id
        elif not self.id and self.username:
            prov_id = f"{self.type.value}_"
            prov_id += (
                self.username.replace(".", "")
                .replace("_", "")
                .lower()
                .split("@")[0][1::2]
            )
            super().__setattr__("id", prov_id)
        elif not self.id:
            self.id = self.type.value


@dataclass(frozen=True)
class MassConfig:
    """Model for the Music Assisant runtime config."""

    database_url: DatabaseURL

    providers: List[MusicProviderConfig] = field(default_factory=list)

    # advanced settings
    max_simultaneous_jobs: int = 5
    stream_port: int = select_stream_port()
    stream_ip: str = get_ip()
