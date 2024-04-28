"""Apple Music musicprovider support for MusicAssistant."""

from __future__ import annotations

import base64
import json
import os
from typing import TYPE_CHECKING, Any

import aiofiles
import m3u8
from pywidevine import PSSH, Cdm, Device, DeviceTypes
from pywidevine.license_protocol_pb2 import WidevinePsshData

from music_assistant.common.helpers.json import json_loads
from music_assistant.common.models.config_entries import (ConfigEntry,
                                                          ConfigValueType)
from music_assistant.common.models.enums import (ConfigEntryType, ExternalID,
                                                 ProviderFeature, StreamType)
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.common.models.media_items import (Album, AlbumType,
                                                       Artist, AudioFormat,
                                                       ContentType, ImageType,
                                                       MediaItemImage,
                                                       MediaItemType,
                                                       MediaType, Playlist,
                                                       ProviderMapping,
                                                       SearchResults, Track)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import CONF_PASSWORD
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    # ProviderFeature.LIBRARY_ARTISTS_EDIT,
    # ProviderFeature.LIBRARY_ALBUMS_EDIT,
    # ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    # ProviderFeature.LIBRARY_TRACKS_EDIT,
    # ProviderFeature.PLAYLIST_TRACKS_EDIT,
    # ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    # ProviderFeature.SIMILAR_TRACKS,
)

DEVELOPER_TOKEN = "TODO: Parse from appvars"
DECRYPT_CLIENT_ID_FILENAME = "client_id.bin"
DECRYPT_PRIVATE_KEY_FILENAME = "private_key.pem"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = AppleMusicProvider(mass, manifest, config)
    await prov.handle_async_init()
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
    return (
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Music user token",
            required=True,
        ),
    )


class AppleMusicProvider(MusicProvider):
    """Implementation of an Apple Music MusicProvider."""

    _music_user_token: str | None = None
    _storefront: str | None = None
    _decrypt_client_id: bytes | None = None
    _decrypt_private_key: bytes | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._music_user_token = self.config.get_value(CONF_PASSWORD)
        self._storefront = await self._get_user_storefront()
        base_path = os.path.join(os.path.dirname(__file__), "bin")
        async with aiofiles.open(
            os.path.join(base_path, DECRYPT_CLIENT_ID_FILENAME), "rb"
        ) as _file:
            self._decrypt_client_id = await _file.read()
        async with aiofiles.open(
            os.path.join(base_path, DECRYPT_PRIVATE_KEY_FILENAME), "rb"
        ) as _file:
            self._decrypt_private_key = await _file.read()

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.LIBRARY_ARTISTS_EDIT,
            ProviderFeature.LIBRARY_ALBUMS_EDIT,
            ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.PLAYLIST_TRACKS_EDIT,
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
            # ProviderFeature.SIMILAR_TRACKS,
        )

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        return SearchResults()

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from spotify."""
        endpoint = "me/library/artists"
        for item in await self._get_all_items(endpoint, include="catalog", extend="editorialNotes"):
            if item and item["id"]:
                yield self._parse_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        endpoint = "me/library/albums"
        for item in await self._get_all_items(endpoint, include="catalog"):
            if item and item["id"]:
                yield self._parse_album(item)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        endpoint = "me/library/songs"
        for item in await self._get_all_items(endpoint, include="artists,albums,catalog"):
            if item and item["id"]:
                yield self._parse_track(item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        endpoint = "me/library/playlists"
        for item in await self._get_all_items(endpoint):
            if item and item["id"]:
                yield self._parse_playlist(item)

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        endpoint = f"catalog/{self._storefront}/artists/{prov_artist_id}"
        response = await self._get_data(endpoint, extend="editorialNotes")
        return self._parse_artist(response["data"][0])

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        endpoint = f"catalog/{self._storefront}/albums/{prov_album_id}"
        response = await self._get_data(endpoint)
        return self._parse_album(response["data"][0])

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        endpoint = f"catalog/{self._storefront}/songs/{prov_track_id}"
        response = await self._get_data(endpoint, include="artists")
        return self._parse_track(response["data"][0])

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        endpoint = f"catalog/{self._storefront}/playlists/{prov_playlist_id}"
        response = await self._get_data(endpoint)
        return self._parse_playlist(response["data"][0])

    async def get_album_tracks(self, prov_album_id) -> list[Track]:
        """Get all album tracks for given album id."""
        endpoint = f"catalog/{self._storefront}/albums/{prov_album_id}/tracks"
        response = await self._get_data(endpoint, include="artists")
        return [self._parse_track(track) for track in response["data"] if track["id"]]

    async def get_playlist_tracks(self, prov_playlist_id) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given playlist id."""
        if self._is_catalog_id(prov_playlist_id):
            endpoint = f"catalog/{self._storefront}/playlists/{prov_playlist_id}/tracks"
        else:
            endpoint = f"me/library/playlists/{prov_playlist_id}/tracks"
        count = 1
        for track in await self._get_all_items(endpoint, include="artists,catalog"):
            if track and track["id"]:
                parsed_track = self._parse_track(track)
                parsed_track.position = count
                yield parsed_track
                count += 1

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of all albums for the given artist."""
        endpoint = f"catalog/{self._storefront}/artists/{prov_artist_id}/albums"
        response = await self._get_data(endpoint)
        return [self._parse_album(album) for album in response["data"] if album["id"]]

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        endpoint = f"catalog/{self._storefront}/artists/{prov_artist_id}/view/top-songs"
        response = await self._get_data(endpoint)
        return [self._parse_track(track) for track in response["data"] if track["id"]]

    async def library_add(self, item: MediaItemType):
        """Add item to library."""
        raise NotImplementedError("Not implemented!")

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        raise NotImplementedError("Not implemented!")

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]):
        """Add track(s) to playlist."""
        raise NotImplementedError("Not implemented!")

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        raise NotImplementedError("Not implemented!")

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        raise NotImplementedError("Not implemented!")

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        stream_metadata = await self._fetch_song_stream_metadata(item_id)
        license_url = stream_metadata["hls-key-server-url"]
        stream_url, uri = self._parse_stream_url_and_uri(stream_metadata["assets"])
        key_id = base64.b64decode(uri.split(",")[1])
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC,
            ),
            stream_type=StreamType.ENCRYPTED_HTTP,
            path=stream_url,
            decryption_key=await self._get_decryption_key(license_url, key_id, uri, item_id),
        )

    def _parse_artist(self, artist_obj):
        """Parse spotify artist object to generic layout."""
        relationships = artist_obj.get("relationships", {})
        if artist_obj.get("type") == "library-artists" and relationships["catalog"]["data"] != []:
            artist_id = relationships["catalog"]["data"][0]["id"]
            attributes = relationships["catalog"]["data"][0]["attributes"]
        elif "attributes" in artist_obj:
            artist_id = artist_obj["id"]
            attributes = artist_obj["attributes"]
        else:
            artist_id = artist_obj["id"]
            attributes = {}
        artist = Artist(
            item_id=artist_id,
            name=attributes.get("name"),
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=attributes.get("url"),
                )
            },
        )
        if artwork := attributes.get("artwork"):
            artist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork["url"].format(w=artwork["width"], h=artwork["height"]),
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        if genres := attributes.get("genreNames"):
            artist.metadata.genres = set(genres)
        if notes := attributes.get("editorialNotes"):
            artist.metadata.description = notes.get("standard")
        return artist

    def _parse_album(self, album_obj: dict):
        """Parse spotify album object to generic layout."""
        relationships = album_obj.get("relationships", {})
        response_type = album_obj.get("type")
        if response_type == "library-albums" and relationships["catalog"]["data"] != []:
            album_id = relationships.get("catalog", {})["data"][0]["id"]
            attributes = relationships.get("catalog", {})["data"][0]["attributes"]
        elif "attributes" in album_obj:
            album_id = album_obj["id"]
            attributes = album_obj["attributes"]
        else:
            album_id = album_obj["id"]
            attributes = {}
        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=attributes.get("name"),
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.AAC),
                    url=attributes.get("url"),
                    available=attributes.get("playParams", {}).get("id") is not None,
                )
            },
        )
        if artists := relationships.get("artists"):
            album.artists = [self._parse_artist(artist) for artist in artists["data"]]
        if release_date := attributes.get("releaseDate"):
            album.year = int(release_date.split("-")[0])
        if genres := attributes.get("genreNames"):
            album.metadata.genres = set(genres)
        if artwork := attributes.get("artwork"):
            album.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork["url"].format(w=artwork["width"], h=artwork["height"]),
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        if album_copyright := attributes.get("copyright"):
            album.metadata.copyright = album_copyright
        if record_label := attributes.get("recordLabel"):
            album.metadata.label = record_label
        if upc := attributes.get("upc"):
            album.external_ids.add((ExternalID.BARCODE, "0" + upc))
        if notes := attributes.get("editorialNotes"):
            album.metadata.description = notes.get("standard")
        if content_rating := attributes.get("contentRating"):
            album.metadata.explicit = content_rating == "explicit"
        album_type = AlbumType.ALBUM
        if attributes.get("isSingle"):
            album_type = AlbumType.SINGLE
        elif attributes.get("isCompilation"):
            album_type = AlbumType.COMPILATION
        album.album_type = album_type
        return album

    def _parse_track(
        self,
        track_obj: dict[str, Any],
    ) -> Track:
        """Parse track object to generic layout."""
        relationships = track_obj.get("relationships", {})
        if track_obj.get("type") == "library-songs" and relationships["catalog"]["data"] != []:
            track_id = relationships.get("catalog", {})["data"][0]["id"]
            attributes = relationships.get("catalog", {})["data"][0]["attributes"]
        elif "attributes" in track_obj:
            track_id = track_obj["id"]
            attributes = track_obj["attributes"]
        else:
            track_id = track_obj["id"]
            attributes = {}
        track = Track(
            item_id=track_id,
            provider=self.domain,
            name=attributes.get("name"),
            duration=attributes.get("durationInMillis", 0) / 1000,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.AAC),
                    url=attributes.get("url"),
                    available=attributes.get("playParams", {}).get("id") is not None,
                )
            },
        )
        if disc_number := attributes.get("discNumber"):
            track.disc_number = disc_number
        if track_number := attributes.get("trackNumber"):
            track.track_number = track_number
        if artists := relationships.get("artists"):
            track.artists = [self._parse_artist(artist) for artist in artists["data"]]
        if albums := relationships.get("albums"):
            track.album = self._parse_album(albums["data"][0])
        if artwork := attributes.get("artwork"):
            track.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork["url"].format(w=artwork["width"], h=artwork["height"]),
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        if genres := attributes.get("genreNames"):
            track.metadata.genres = set(genres)
        if composers := attributes.get("composerName"):
            track.metadata.performers = set(composers.split(", "))
        if isrc := attributes.get("isrc"):
            track.external_ids.add((ExternalID.ISRC, isrc))
        return track

    def _parse_playlist(self, playlist_obj):
        """Parse spotify playlist object to generic layout."""
        attributes = playlist_obj["attributes"]
        playlist = Playlist(
            item_id=playlist_obj["id"],
            provider=self.domain,
            name=attributes["name"],
            owner=attributes.get("curatorName", "me"),
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=attributes.get("url"),
                )
            },
        )
        if artwork := attributes.get("artwork"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork["url"].format(w=artwork["width"], h=artwork["height"]),
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        if description := attributes.get("description"):
            playlist.metadata.description = description.get("standard")
        playlist.is_editable = attributes.get("canEdit", False)
        if checksum := attributes.get("lastModifiedDate"):
            playlist.metadata.cache_checksum = checksum
        return playlist

    async def _get_all_items(self, endpoint, key="data", **kwargs) -> list[dict]:
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        all_items = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            offset += limit
            if not result or key not in result or not result[key]:
                break
            all_items += result[key]
            if len(result[key]) < limit:
                break
        return all_items

    async def _get_data(self, endpoint, **kwargs) -> dict[str, Any]:
        """Get data from api."""
        url = f"https://api.music.apple.com/v1/{endpoint}"
        headers = {"Authorization": f"Bearer {DEVELOPER_TOKEN}"}
        headers["Music-User-Token"] = self._music_user_token
        async with (
            self.mass.http_session.get(
                url, headers=headers, params=kwargs, ssl=True, timeout=120
            ) as response,
        ):
            # handle 404 not found, convert to MediaNotFoundError
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            response.raise_for_status()
            return await response.json(loads=json_loads)

    async def _delete_data(self, endpoint, data=None, **kwargs) -> str:
        """Delete data from api."""
        raise NotImplementedError("Not implemented!")

    async def _put_data(self, endpoint, data=None, **kwargs) -> str:
        """Put data on api."""
        raise NotImplementedError("Not implemented!")

    async def _post_data(self, endpoint, data=None, **kwargs) -> str:
        """Post data on api."""
        raise NotImplementedError("Not implemented!")

    async def _get_user_storefront(self) -> str:
        """Get the user's storefront."""
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        result = await self._get_data("me/storefront", l=language)
        return result["data"][0]["id"]

    def _is_catalog_id(self, catalog_id: str) -> bool:
        """Check if input is a catalog id, or a library id."""
        return catalog_id.isnumeric()

    async def _fetch_song_stream_metadata(self, song_id: str) -> str:
        """Get the stream URL for a song from Apple Music."""
        playback_url = "https://play.music.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
        data = {
            "salableAdamId": song_id,
        }
        async with self.mass.http_session.post(
            playback_url, headers=self._get_decryption_headers(), json=data, ssl=False
        ) as response:
            response.raise_for_status()
            content = await response.json(loads=json_loads)
            return content["songList"][0]

    def _parse_stream_url_and_uri(self, stream_assets: list[dict]) -> str:
        """Parse the Stream URL and Key URI from the song."""
        ctrp256_urls = [asset["URL"] for asset in stream_assets if asset["flavor"] == "28:ctrp256"]
        if len(ctrp256_urls) == 0:
            raise MediaNotFoundError("No ctrp256 URL found for song.")
        m3u8_playlist = m3u8.load(ctrp256_urls[0])
        track_url = m3u8_playlist.base_uri + m3u8_playlist.segments[0].uri
        uri = m3u8_playlist.keys[0].uri
        return (track_url, uri)

    def _get_decryption_headers(self):
        """Get headers for decryption requests."""
        return {
            "authorization": f"Bearer {DEVELOPER_TOKEN}",
            "media-user-token": self._music_user_token,
            "connection": "keep-alive",
            "accept": "application/json",
            "origin": "https://music.apple.com",
            "referer": "https://music.apple.com/",
            "accept-encoding": "gzip, deflate, br",
            "content-type": "application/json;charset=utf-8",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
                " Chrome/110.0.0.0 Safari/537.36"
            ),
        }

    async def _get_decryption_key(
        self, license_url: str, key_id: str, uri: str, item_id: str
    ) -> str:
        """Get the decryption key for a song."""
        pssh = self._get_pssh(key_id)
        device = Device(
            client_id=self._decrypt_client_id,
            private_key=self._decrypt_private_key,
            type_=DeviceTypes.ANDROID,
            security_level=3,
            flags={},
        )
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, pssh)
        track_license = await self._get_license(challenge, license_url, uri, item_id)
        cdm.parse_license(session_id, track_license)
        key = [key for key in cdm.get_keys(session_id) if key.type == "CONTENT"][0]
        if not key:
            raise MediaNotFoundError("Unable to get decryption key for song %s.".format())
        cdm.close(session_id)
        return key.key.hex()

    def _get_pssh(self, key_id: bytes) -> PSSH:
        """Get the PSSH for a song."""
        pssh_data = WidevinePsshData()
        pssh_data.algorithm = 1
        pssh_data.key_ids.append(key_id)
        init_data = base64.b64encode(pssh_data.SerializeToString()).decode("utf-8")
        return PSSH.new(system_id=PSSH.SystemId.Widevine, init_data=init_data)

    async def _get_license(self, challenge: bytes, license_url: str, uri: str, item_id: str) -> str:
        """Get the license for a song based on the challenge."""
        challenge_b64 = base64.b64encode(challenge).decode("utf-8")
        data = {
            "challenge": challenge_b64,
            "key-system": "com.widevine.alpha",
            "uri": uri,
            "adamId": item_id,
            "isLibrary": False,
            "user-initiated": True,
        }
        async with self.mass.http_session.post(
            license_url, data=json.dumps(data), headers=self._get_decryption_headers(), ssl=False
        ) as response:
            response.raise_for_status()
            content = await response.json(loads=json_loads)
            track_license = content.get("license")
            if not track_license:
                raise MediaNotFoundError("No license found for song %s.".format())
            return track_license
