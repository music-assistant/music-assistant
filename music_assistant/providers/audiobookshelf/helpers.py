"""Helpers for Audiobookshelf provider."""

from dataclasses import dataclass, field

from mashumaro.mixins.dict import DataClassDictMixin


@dataclass(kw_only=True)
class LibraryHelper(DataClassDictMixin):
    """Lib name + media items' uuids."""

    name: str
    item_ids: set[str] = field(default_factory=set)


@dataclass(kw_only=True)
class LibrariesHelper(DataClassDictMixin):
    """Helper class to store ABSLibrary name, id and the uuids of its media items.

    Dictionary is lib_id:AbsLibraryWithItemIDs.
    """

    audiobooks: dict[str, LibraryHelper] = field(default_factory=dict)
    podcasts: dict[str, LibraryHelper] = field(default_factory=dict)
