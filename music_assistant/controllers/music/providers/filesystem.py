"""Filesystem musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
import os
import urllib.parse
from contextlib import asynccontextmanager
from time import time
from typing import Generator, List, Optional, Tuple
from uuid import uuid4

import aiofiles
import xmltodict
from aiofiles.threadpool.binary import AsyncFileIO
from tinytag.tinytag import TinyTag

from music_assistant.helpers.util import parse_title_and_version, try_parse_int
from music_assistant.models.enums import ProviderType
from music_assistant.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    MediaQuality,
    MediaType,
    Playlist,
    StreamDetails,
    StreamType,
    Track,
)
from music_assistant.models.provider import MusicProvider


def scantree(path: str) -> Generator[os.DirEntry]:
    """Recursively yield DirEntry objects for given directory."""
    for entry in os.scandir(path):
        if entry.is_dir(follow_symlinks=False):
            yield from scantree(entry.path)  # see below for Python 2.x
        else:
            yield entry


def split_items(org_str: str, splitters: Tuple[str] = None) -> Tuple[str]:
    """Split up a tags string by common splitter."""
    if isinstance(org_str, list):
        return org_str
    if splitters is None:
        splitters = ("/", ";", ",")
    if org_str is None:
        return tuple()
    for splitter in splitters:
        if splitter in org_str:
            return tuple((x.strip() for x in org_str.split(splitter)))
    return (org_str,)


FALLBACK_ARTIST = "Various Artists"
ARTIST_SPLITTERS = (";", ",", "Featuring", " Feat. ", " Feat ", "feat.", " & ")


class FileSystemProvider(MusicProvider):
    """
    Implementation of a musicprovider for local files.

    Assumes files are stored on disk in format <artist>/<album>/<track.ext>
    Reads ID3 tags from file and falls back to parsing filename
    Supports m3u files only for playlists
    Supports having URI's from streaming providers within m3u playlist
    """

    _attr_name = "Filesystem"
    _attr_type = ProviderType.FILESYSTEM_LOCAL
    _attr_supported_mediatypes = [
        MediaType.TRACK,
        MediaType.PLAYLIST,
        MediaType.ARTIST,
        MediaType.ALBUM,
    ]

    def __init__(self, *args, **kwargs) -> None:
        """Initialize MusicProvider."""
        super().__init__(*args, **kwargs)
        self._cache_built = asyncio.Event()

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""

        if not os.path.isdir(self.config.path):
            raise MediaNotFoundError(
                f"Music Directory {self.config.path} does not exist"
            )

        return True

    async def search(self, *args, **kwargs) -> List[MediaItemType]:
        """Perform search on musicprovider."""
        # items for the filesystem provider are already returned by the database
        return []

    async def sync_library(self) -> None:
        """Run library sync for this provider."""
        last_save = 0
        cache_key = f"{self.id}.checksums"
        checksums = await self.mass.cache.get(cache_key)
        if checksums is None:
            checksums = {}
        # find all music files in the music directory and all subfolders
        # we work bottom down, as-in we derive all info from the tracks
        for entry in scantree(self.config.path):

            # mtime is used as file checksum
            checksum = str(entry.stat().st_mtime)
            if checksum == checksums.get(entry.path):
                continue

            try:
                if track := await self._parse_track(entry.path, checksum):
                    # add/update track to db
                    track.in_library = True
                    await self.mass.music.tracks.add_db_item(track)
                    # process album
                    if track.album:
                        track.album.in_library = True
                        await self.mass.music.albums.add_db_item(track.album)
                        # process (album)artist
                        if track.album.artist:
                            track.album.artist.in_library = True
                            await self.mass.music.artists.add_db_item(
                                track.album.artist
                            )
                elif playlist := await self._parse_playlist(entry.path, checksum):
                    # add/update] playlist to db
                    playlist.in_library = True
                    await self.mass.music.playlists.add_db_item(playlist)
            except Exception:  # pylint: disable=broad-except
                # we don't want the whole sync to crash on one file so we catch all exceptions here
                self.logger.exception("Error processing %s", entry.path)

            # save current checksum cache every 5 mins for large listings
            checksums[entry.path] = checksum
            if (time() - last_save) > 60:
                await self.mass.cache.set(cache_key, checksums)
                last_save = time()

        await self.mass.cache.set(cache_key, checksums)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        itempath = await self.get_filepath(prov_artist_id)
        return await self._parse_artist(itempath)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        itempath = await self.get_filepath(prov_album_id)
        return await self._parse_album(itempath)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        itempath = await self.get_filepath(prov_track_id)
        return await self._parse_track(itempath)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        itempath = await self.get_filepath(prov_playlist_id)
        return await self._parse_playlist(itempath)

    async def get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        itempath = await self.get_filepath(prov_album_id)
        if not self.exists(itempath):
            raise MediaNotFoundError(f"Album path does not exist: {itempath}")
        result = []
        for entry in scantree(self.config.path):
            # mtime is used as file checksum
            checksum = str(entry.stat().st_mtime)
            if track := await self._parse_track(entry.path, checksum):
                result.append(track)
        return result

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get playlist tracks for given playlist id."""
        result = []
        itempath = await self.get_filepath(prov_playlist_id)
        if not self.exists(itempath):
            raise MediaNotFoundError(f"playlist path does not exist: {itempath}")
        index = 0
        async with self.open_file(itempath, "r") as _file:
            for line in await _file.readlines():
                line = urllib.parse.unquote(line.strip())
                if line and not line.startswith("#"):
                    if track := await self._parse_track_from_uri(line):
                        track.position = index
                        result.append(track)
                        index += 1
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of albums for the given artist."""
        itempath = await self.get_filepath(prov_artist_id)
        if not self.exists(itempath):
            raise MediaNotFoundError(f"Artist path does not exist: {itempath}")
        result = []
        for entry in os.scandir(itempath):
            if entry.is_dir(follow_symlinks=False):
                if album := await self._parse_album(entry.path):
                    result.append(album)
        return result

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of all tracks as we have no clue about preference."""
        itempath = await self.get_filepath(prov_artist_id)
        if not self.exists(itempath):
            raise MediaNotFoundError(f"Artist path does not exist: {itempath}")
        result = []
        for entry in scantree(self.config.path):
            # mtime is used as file checksum
            checksum = str(entry.stat().st_mtime)
            if track := await self._parse_track(entry.path, checksum):
                result.append(track)
        return result

    async def library_add(self, *args, **kwargs) -> bool:
        """Add item to provider's library. Return true on succes."""
        # already handled by database

    async def library_remove(self, *args, **kwargs) -> bool:
        """Remove item from provider's library. Return true on succes."""
        # already handled by database
        # TODO: do we want to process deletions here ?

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Add track(s) to playlist."""
        itempath = await self.get_filepath(prov_playlist_id)
        if not self.exists(itempath):
            raise MediaNotFoundError(f"Playlist path does not exist: {itempath}")
        async with self.open_file(itempath, "a") as _file:
            for uri in prov_track_ids:
                await _file.writeline(uri)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Remove track(s) from playlist."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        itempath = await self.get_filepath(item_id)
        if not self.exists(itempath):
            raise MediaNotFoundError(f"Track path does not exist: {itempath}")

        def parse_tag():
            return TinyTag.get(itempath)

        tags = await self.mass.loop.run_in_executor(None, parse_tag)

        return StreamDetails(
            type=StreamType.FILE,
            provider=self.type,
            item_id=item_id,
            content_type=ContentType(itempath.split(".")[-1]),
            path=itempath,
            sample_rate=tags.samplerate or 44100,
            bit_depth=16,  # TODO: parse bitdepth
        )

    async def _parse_track(
        self, track_path: str, checksum: Optional[str] = None
    ) -> Track | None:
        """Try to parse a track from a filename by reading its tags."""
        track_item_id = await self._get_item_id(track_path)
        track_path_base = track_path.split(self.config.path)[-1]

        if not self.exists(track_path):
            raise MediaNotFoundError(f"Track path does not exist: {track_path}")

        # reading file/tags is slow so we keep a cache and checksum
        checksum = checksum or self._get_checksum(track_path)
        cache_key = f"{self.id}_tracks_{track_item_id}"
        if cache := await self.mass.cache.get(cache_key, checksum):
            return Track.from_dict(cache)

        if not TinyTag.is_supported(track_path):
            return None

        # parse ID3 tags with TinyTag
        def parse_tags():
            return TinyTag.get(track_path, image=True, ignore_errors=True)

        tags = await self.mass.loop.run_in_executor(None, parse_tags)

        # prefer title from tag, fallback to filename
        if tags.title:
            track_title = tags.title
        else:

            ext = track_path_base.split(".")[-1]
            track_title = track_path_base.replace(f".{ext}", "").replace("_", " ")
            self.logger.warning(
                "%s is missing ID3 tags, use filename as fallback", track_path_base
            )

        name, version = parse_title_and_version(track_title)
        track = Track(
            item_id=track_item_id, provider=self.type, name=name, version=version
        )

        # work out if we have an artist/album/track.ext structure
        track_parts = track_path_base.rsplit(os.sep)
        if track_parts[0] == "":
            track_parts = track_parts[1:]
        if len(track_parts) == 3:
            album_path = os.path.dirname(track_path)
            artist_path = os.path.dirname(album_path)
            album_artist = await self._parse_artist(artist_path, True)
            track.album = await self._parse_album(album_path, album_artist, True)

        if track.album is None and tags.album:
            if tags.albumartist:
                album_path = f"{tags.albumartist}/{tags.album}"
            else:
                album_path = tags.album
            album_id = await self._get_item_id(album_path)
            album_name, album_version = parse_title_and_version(tags.album)
            track.album = Album(
                item_id=album_id,
                provider=self.type,
                name=album_name,
                version=album_version,
                year=try_parse_int(tags.year) if tags.year else None,
                provider_ids={
                    MediaItemProviderId(album_id, self.type, self.id, url=album_path)
                },
            )
            if tags.albumartist:
                artist_id = await self._get_item_id(tags.albumartist)
                track.album.artist = Artist(
                    item_id=artist_id,
                    provider=self.type,
                    name=tags.albumartist,
                    provider_ids={
                        MediaItemProviderId(
                            artist_id, self.type, self.id, url=tags.albumartist
                        )
                    },
                )

            # try to guess the album type
            if name.lower() == album_name.lower():
                track.album.album_type = AlbumType.SINGLE
            elif track.album.artist and track.album.artist not in (
                x.name for x in track.artists
            ):
                track.album.album_type = AlbumType.COMPILATION
            else:
                track.album.album_type = AlbumType.ALBUM

        # Parse track artist(s) from artist string using common splitters used in ID3 tags
        # NOTE: do not use a '/' or '&' to prevent artists like AC/DC become messed up
        track_artists_str = tags.artist or FALLBACK_ARTIST
        track.artists = [
            await self._parse_artist(item)
            for item in split_items(track_artists_str, ARTIST_SPLITTERS)
        ]

        # Check if track has embedded metadata
        img = await self.mass.loop.run_in_executor(None, tags.get_image)
        if not track.metadata.images and img:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            track.metadata.images = [MediaItemImage(ImageType.THUMB, track_path, True)]
            if track.album and not track.album.metadata.images:
                track.album.metadata.images = track.metadata.images

        # parse other info
        track.duration = tags.duration
        track.metadata.genres = set(split_items(tags.genre))
        track.disc_number = try_parse_int(tags.disc)
        track.track_number = try_parse_int(tags.track)
        track.isrc = tags.extra.get("isrc", "")
        if "copyright" in tags.extra:
            track.metadata.copyright = tags.extra["copyright"]
        if "lyrics" in tags.extra:
            track.metadata.lyrics = tags.extra["lyrics"]
        # store last modified time as checksum
        track.metadata.checksum = checksum

        quality_details = ""
        if track_path.endswith(".flac"):
            # TODO: get bit depth
            quality = MediaQuality.FLAC_LOSSLESS
            if tags.samplerate > 192000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_4
            elif tags.samplerate > 96000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_3
            elif tags.samplerate > 48000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_2
            quality_details = f"{tags.samplerate / 1000} Khz"
        elif track_path.endswith(".ogg"):
            quality = MediaQuality.LOSSY_OGG
            quality_details = f"{tags.bitrate} kbps"
        elif track_path.endswith(".m4a"):
            quality = MediaQuality.LOSSY_AAC
            quality_details = f"{tags.bitrate} kbps"
        else:
            quality = MediaQuality.LOSSY_MP3
            quality_details = f"{tags.bitrate} kbps"
        track.add_provider_id(
            MediaItemProviderId(
                item_id=track_item_id,
                prov_type=self.type,
                prov_id=self.id,
                quality=quality,
                details=quality_details,
                url=track_path,
            )
        )
        await self.mass.cache.set(cache_key, track.to_dict(), checksum, 86400 * 365 * 5)
        return track

    async def _parse_artist(self, artist_path: str, skip_cache=False) -> Artist | None:
        """Lookup metadata in Artist folder."""
        artist_item_id = await self._get_item_id(artist_path)
        name = artist_path.split(os.sep)[-1]

        cache_key = f"{self.id}.artist.{artist_item_id}"
        if not skip_cache:
            if cache := await self.mass.cache.get(cache_key):
                return Artist.from_dict(cache)

        artist = Artist(
            artist_item_id,
            self.type,
            name,
            provider_ids={
                MediaItemProviderId(artist_item_id, self.type, self.id, url=artist_path)
            },
        )

        if not self.exists(artist_path):
            # return basic object if there is no path on disk
            # happens if disk structure does not conform
            return artist

        nfo_file = os.path.join(artist_path, "artist.nfo")
        if self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            async with self.open_file(nfo_file, "r") as _file:
                data = await _file.read()
            info = await self.mass.loop.run_in_executor(None, xmltodict.parse, data)
            info = info["artist"]
            artist.name = info.get("title", info.get("name", name))
            if sort_name := info.get("sortname"):
                artist.sort_name = sort_name
            if musicbrainz_id := info.get("musicbrainzartistid"):
                artist.musicbrainz_id = musicbrainz_id
            if descripton := info.get("biography"):
                artist.metadata.description = descripton
            if genre := info.get("genre"):
                artist.metadata.genres = set(split_items(genre))
            if not artist.musicbrainz_id:
                for uid in info.get("uniqueid", []):
                    if uid["@type"] == "MusicBrainzArtist":
                        artist.musicbrainz_id = uid["#text"]
        # find local images
        images = []
        for _filename in os.listdir(artist_path):
            ext = _filename.split(".")[-1]
            if ext not in ("jpg", "png"):
                continue
            _filepath = os.path.join(artist_path, _filename)
            for img_type in ImageType:
                if img_type.value in _filepath:
                    images.append(MediaItemImage(img_type, _filepath, True))
                elif _filename == "folder.jpg":
                    images.append(MediaItemImage(ImageType.THUMB, _filepath, True))
        if images:
            artist.metadata.images = images

        await self.mass.cache.set(cache_key, artist.to_dict())
        return artist

    async def _parse_album(
        self, album_path: str, artist: Optional[Artist] = None, skip_cache=False
    ) -> Album | None:
        """Lookup metadata in Album folder."""
        album_item_id = await self._get_item_id(album_path)
        name = album_path.split(os.sep)[-1]

        cache_key = f"{self.id}.album.{album_item_id}"
        if not skip_cache:
            if cache := await self.mass.cache.get(cache_key):
                return Album.from_dict(cache)

        album = Album(
            album_item_id,
            self.type,
            name,
            artist=artist,
            provider_ids={
                MediaItemProviderId(album_item_id, self.type, self.id, url=album_path)
            },
        )

        if not self.exists(album_path):
            # return basic object if there is no path on disk
            # happens if disk structure does not conform
            return artist

        nfo_file = os.path.join(album_path, "album.nfo")
        if self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            async with self.open_file(nfo_file) as _file:
                data = await _file.read()
            info = await self.mass.loop.run_in_executor(None, xmltodict.parse, data)
            info = info["album"]
            album.name = info.get("title", info.get("name", name))
            if sort_name := info.get("sortname"):
                album.sort_name = sort_name
            if musicbrainz_id := info.get("musicbrainzreleasegroupid"):
                album.musicbrainz_id = musicbrainz_id
            if description := info.get("review"):
                album.metadata.description = description
            if year := info.get("label"):
                album.year = int(year)
            if genre := info.get("genre"):
                album.metadata.genres = set(split_items(genre))
            for uid in info.get("uniqueid", []):
                if uid["@type"] == "MusicBrainzReleaseGroup":
                    if not album.musicbrainz_id:
                        album.musicbrainz_id = uid["#text"]
                if uid["@type"] == "MusicBrainzAlbumArtist":
                    if album.artist and not album.artist.musicbrainz_id:
                        album.artist.musicbrainz_id = uid["#text"]
        # parse name/version
        album.name, album.version = parse_title_and_version(album.name)
        # find local images
        images = []
        for _filename in os.listdir(album_path):
            ext = _filename.split(".")[-1]
            if ext not in ("jpg", "png"):
                continue
            _filepath = os.path.join(album_path, _filename)
            for img_type in ImageType:
                if img_type.value in _filepath:
                    images.append(MediaItemImage(img_type, _filepath, True))
                elif _filename == "folder.jpg":
                    images.append(MediaItemImage(ImageType.THUMB, _filepath, True))
        if images:
            album.metadata.images = images

        await self.mass.cache.set(cache_key, album.to_dict())
        return album

    async def _parse_playlist(
        self, playlist_path: str, checksum: Optional[str] = None
    ) -> Playlist | None:
        """Parse playlist from file."""
        prov_item_id = await self._get_item_id(playlist_path)
        checksum = checksum or self._get_checksum(playlist_path)

        if not playlist_path.endswith(".m3u"):
            return None

        if not self.exists(playlist_path):
            raise MediaNotFoundError(f"Playlist path does not exist: {playlist_path}")

        name = prov_item_id.split(os.sep)[-1].replace(".m3u", "")

        playlist = Playlist(prov_item_id, provider=self.type, name=name)
        playlist.is_editable = True
        playlist.add_provider_id(
            MediaItemProviderId(
                item_id=prov_item_id,
                prov_type=self.type,
                prov_id=self.id,
                url=playlist_path,
            )
        )
        playlist.owner = self._attr_name
        playlist.metadata.checksum = checksum
        return playlist

    async def _parse_track_from_uri(self, uri):
        """Try to parse a track from an uri found in playlist."""
        if "://" in uri:
            # track is uri from external provider?
            try:
                return await self.mass.music.get_item_by_uri(uri)
            except MusicAssistantError as err:
                self.logger.warning(
                    "Could not parse uri %s to track: %s", uri, str(err)
                )
                return None
        # try to treat uri as filename
        try:
            return await self.get_track(uri)
        except MediaNotFoundError:
            return None

    @staticmethod
    def exists(file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        return os.path.isfile(file_path) or os.path.isdir(file_path)

    @asynccontextmanager
    @staticmethod
    async def open_file(filename: str, mode="rb") -> AsyncFileIO:
        """Return (async) handle to given file."""
        # remote file locations should return a tempfile here ?
        async with aiofiles.open(filename, mode) as _file:
            yield _file

    async def get_filepath(self, item_id: str) -> str | None:
        """Get full filepath on disk for item_id."""
        prov_mapping = await self.mass.music.get_provider_mapping(
            provider_id=self.id, provider_item_id=item_id, return_key="url"
        )
        if prov_mapping is not None:
            return prov_mapping
        return None

    async def _get_item_id(self, filename: str) -> str:
        """Return item_id for given filename (or create a new one)."""
        prov_mapping = await self.mass.music.get_provider_mapping(
            provider_id=self.id, url=filename, return_key="prov_item_id"
        )
        if prov_mapping is not None:
            return prov_mapping
        # generate new unique id
        return uuid4().hex

    @staticmethod
    def _get_checksum(filename: str) -> str:
        """Get checksum for file."""
        # use last modified time as checksum
        return str(os.path.getmtime(filename))
