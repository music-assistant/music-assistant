"""Tidal music provider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import base64
import pickle
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from tidalapi import Album as TidalAlbum
from tidalapi import Artist as TidalArtist
from tidalapi import Config as TidalConfig
from tidalapi import Playlist as TidalPlaylist
from tidalapi import Session as TidalSession
from tidalapi import Track as TidalTrack
from tidalapi import exceptions as tidal_exceptions

from music_assistant.constants import CACHE_CATEGORY_DEFAULT, CACHE_CATEGORY_MEDIA_INFO
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.tags import AudioTags, async_parse_tags
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider

from .helpers import (
    DEFAULT_LIMIT,
    add_playlist_tracks,
    create_playlist,
    get_album,
    get_album_tracks,
    get_artist,
    get_artist_albums,
    get_artist_toptracks,
    get_library_albums,
    get_library_artists,
    get_library_playlists,
    get_library_tracks,
    get_playlist,
    get_playlist_tracks,
    get_similar_tracks,
    get_stream,
    get_track,
    get_track_lyrics,
    get_tracks_by_isrc,
    library_items_add_remove,
    remove_playlist_tracks,
    search,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from tidalapi.media import Lyrics as TidalLyrics
    from tidalapi.media import Stream as TidalStream

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

TOKEN_TYPE = "Bearer"

# Actions
CONF_ACTION_START_PKCE_LOGIN = "start_pkce_login"
CONF_ACTION_COMPLETE_PKCE_LOGIN = "auth"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# Intermediate steps
CONF_TEMP_SESSION = "temp_session"
CONF_OOPS_URL = "oops_url"

# Config keys
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_EXPIRY_TIME = "expiry_time"
CONF_QUALITY = "quality"

# Labels
LABEL_START_PKCE_LOGIN = "start_pkce_login_label"
LABEL_OOPS_URL = "oops_url_label"
LABEL_COMPLETE_PKCE_LOGIN = "complete_pkce_login_label"

BROWSE_URL = "https://tidal.com/browse"
RESOURCES_URL = "https://resources.tidal.com/images"

_R = TypeVar("_R")
_P = ParamSpec("_P")


class TidalQualityEnum(StrEnum):
    """Enum for Tidal Quality."""

    HIGH_LOSSLESS = "LOSSLESS"  # "High - 16bit, 44.1kHz"
    HI_RES = "HI_RES_LOSSLESS"  # "Max - Up to 24bit, 192kHz"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return TidalProvider(mass, manifest, config)


async def tidal_auth_url(auth_helper: AuthenticationHelper, quality: str) -> str:
    """Generate the Tidal authentication URL."""

    def inner() -> str:
        config = TidalConfig(quality=quality, item_limit=10000, alac=False)
        session = TidalSession(config=config)
        url = session.pkce_login_url()
        # Schedule auth_helper.send_url to run in event loop
        auth_helper.mass.loop.call_soon_threadsafe(auth_helper.send_url, url)
        session_bytes = pickle.dumps(session)
        base64_bytes = base64.b64encode(session_bytes)
        return base64_bytes.decode("utf-8")

    return await asyncio.to_thread(inner)


async def tidal_pkce_login(base64_session: str, url: str) -> TidalSession:
    """Async wrapper around the tidalapi Session function."""

    def inner() -> TidalSession:
        base64_bytes = base64_session.encode("utf-8")
        message_bytes = base64.b64decode(base64_bytes)
        session = pickle.loads(message_bytes)  # noqa: S301
        token = session.pkce_get_auth_token(url_redirect=url)
        session.process_auth_token(token)
        return session

    return await asyncio.to_thread(inner)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    assert values is not None

    if action == CONF_ACTION_START_PKCE_LOGIN:
        async with AuthenticationHelper(mass, cast(str, values["session_id"])) as auth_helper:
            quality = str(values.get(CONF_QUALITY))
            base64_session = await tidal_auth_url(auth_helper, quality)
            values[CONF_TEMP_SESSION] = base64_session
            # Tidal is (ab)using the AuthenticationHelper just to send the user to an URL
            # there is no actual oauth callback happening, instead the user is redirected
            # to a non-existent page and needs to copy the URL from the browser and paste it
            # we simply wait here to allow the user to start the auth
            await asyncio.sleep(15)

    if action == CONF_ACTION_COMPLETE_PKCE_LOGIN:
        quality = str(values.get(CONF_QUALITY))
        pkce_url = str(values.get(CONF_OOPS_URL))
        base64_session = str(values.get(CONF_TEMP_SESSION))
        tidal_session = await tidal_pkce_login(base64_session, pkce_url)
        if not tidal_session.check_login():
            msg = "Authentication to Tidal failed"
            raise LoginFailed(msg)
        # set the retrieved token on the values object to pass along
        values[CONF_AUTH_TOKEN] = tidal_session.access_token
        values[CONF_REFRESH_TOKEN] = tidal_session.refresh_token
        values[CONF_EXPIRY_TIME] = tidal_session.expiry_time.isoformat()
        values[CONF_USER_ID] = str(tidal_session.user.id)
        values[CONF_TEMP_SESSION] = ""

    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_AUTH_TOKEN] = None

    if values.get(CONF_AUTH_TOKEN):
        auth_entries: tuple[ConfigEntry, ...] = (
            ConfigEntry(
                key="label_ok",
                type=ConfigEntryType.LABEL,
                label="You are authenticated with Tidal",
            ),
            ConfigEntry(
                key=CONF_ACTION_CLEAR_AUTH,
                type=ConfigEntryType.ACTION,
                label="Reset authentication",
                description="Reset the authentication for Tidal",
                action=CONF_ACTION_CLEAR_AUTH,
                value=None,
            ),
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                label=CONF_QUALITY,
                required=True,
                hidden=True,
                value=cast(str, values.get(CONF_QUALITY) or TidalQualityEnum.HI_RES.value),
                default_value=cast(str, values.get(CONF_QUALITY) or TidalQualityEnum.HI_RES.value),
            ),
        )
    else:
        auth_entries = (
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                label="Quality setting for Tidal:",
                required=True,
                description="HIGH_LOSSLESS = 16bit 44.1kHz, HI_RES = Up to 24bit 192kHz",
                options=[ConfigValueOption(x.value, x.name) for x in TidalQualityEnum],
                default_value=TidalQualityEnum.HI_RES.value,
                value=cast(str, values.get(CONF_QUALITY)) if values else None,
            ),
            ConfigEntry(
                key=LABEL_START_PKCE_LOGIN,
                type=ConfigEntryType.LABEL,
                label="The button below will redirect you to Tidal.com to authenticate.\n\n"
                " After authenticating, you will be redirected to a page that prominently displays"
                " 'Oops' at the top. That is normal, you need to copy that URL from the "
                "address bar and come back here",
                hidden=action == CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_ACTION_START_PKCE_LOGIN,
                type=ConfigEntryType.ACTION,
                label="Starts the auth process via PKCE on Tidal.com",
                description="This button will redirect you to Tidal.com to authenticate."
                " After authenticating, you will be redirected to a page that prominently displays"
                " 'Oops' at the top.",
                action=CONF_ACTION_START_PKCE_LOGIN,
                depends_on=CONF_QUALITY,
                action_label="Starts the auth process via PKCE on Tidal.com",
                value=cast(str, values.get(CONF_TEMP_SESSION)) if values else None,
                hidden=action == CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_TEMP_SESSION,
                type=ConfigEntryType.STRING,
                label="Temporary session for Tidal",
                hidden=True,
                required=False,
                value=cast(str, values.get(CONF_TEMP_SESSION)) if values else None,
            ),
            ConfigEntry(
                key=LABEL_OOPS_URL,
                type=ConfigEntryType.LABEL,
                label="Copy the URL from the 'Oops' page that you were previously redirected to"
                " and paste it in the field below",
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_OOPS_URL,
                type=ConfigEntryType.STRING,
                label="Oops URL from Tidal redirect",
                description="This field should be filled manually by you after authenticating on"
                " Tidal.com and being redirected to a page that prominently displays"
                " 'Oops' at the top.",
                depends_on=CONF_ACTION_START_PKCE_LOGIN,
                value=cast(str, values.get(CONF_OOPS_URL)) if values else None,
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=LABEL_COMPLETE_PKCE_LOGIN,
                type=ConfigEntryType.LABEL,
                label="After pasting the URL in the field above, click the button below to complete"
                " the process.",
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
            ConfigEntry(
                key=CONF_ACTION_COMPLETE_PKCE_LOGIN,
                type=ConfigEntryType.ACTION,
                label="Complete the auth process via PKCE on Tidal.com",
                description="Click this after adding the 'Oops' URL above, this will complete the"
                " authentication process.",
                action=CONF_ACTION_COMPLETE_PKCE_LOGIN,
                depends_on=CONF_OOPS_URL,
                action_label="Complete the auth process via PKCE on Tidal.com",
                value=None,
                hidden=action != CONF_ACTION_START_PKCE_LOGIN,
            ),
        )

    # return the collected config entries
    return (
        *auth_entries,
        ConfigEntry(
            key=CONF_AUTH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Authentication token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            hidden=True,
            value=cast(str, values.get(CONF_AUTH_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Refresh token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            hidden=True,
            value=cast(str, values.get(CONF_REFRESH_TOKEN)) if values else None,
        ),
        ConfigEntry(
            key=CONF_EXPIRY_TIME,
            type=ConfigEntryType.STRING,
            label="Expiry time of auth token for Tidal",
            hidden=True,
            value=cast(str, values.get(CONF_EXPIRY_TIME)) if values else None,
        ),
        ConfigEntry(
            key=CONF_USER_ID,
            type=ConfigEntryType.STRING,
            label="Your Tidal User ID",
            description="This is your unique Tidal user ID.",
            hidden=True,
            value=cast(str, values.get(CONF_USER_ID)) if values else None,
        ),
    )


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    _tidal_session: TidalSession | None = None
    _tidal_user_id: str
    # rate limiter needs to be specified on provider-level,
    # so make it an class attribute
    throttler = ThrottlerManager(rate_limit=1, period=2)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._tidal_user_id = str(self.config.get_value(CONF_USER_ID))
        await self._get_tidal_session()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
            ProviderFeature.SEARCH,
            ProviderFeature.LIBRARY_ARTISTS_EDIT,
            ProviderFeature.LIBRARY_ALBUMS_EDIT,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            ProviderFeature.PLAYLIST_CREATE,
            ProviderFeature.SIMILAR_TRACKS,
            ProviderFeature.BROWSE,
            ProviderFeature.PLAYLIST_TRACKS_EDIT,
        }

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        parsed_results = SearchResults()
        media_types = [
            x
            for x in media_types
            if x in (MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST)
        ]
        if not media_types:
            return parsed_results

        tidal_session = await self._get_tidal_session()
        search_query = search_query.replace("'", "")
        results = await search(tidal_session, search_query, media_types, limit)

        if results["artists"]:
            parsed_results.artists = [self._parse_artist(artist) for artist in results["artists"]]
        if results["albums"]:
            parsed_results.albums = [self._parse_album(album) for album in results["albums"]]
        if results["playlists"]:
            parsed_results.playlists = [
                self._parse_playlist(playlist) for playlist in results["playlists"]
            ]
        if results["tracks"]:
            parsed_results.tracks = [self._parse_track(track) for track in results["tracks"]]
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        tidal_session = await self._get_tidal_session()
        artist: TidalArtist  # satisfy the type checker
        async for artist in self._iter_items(
            get_library_artists, tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT
        ):
            yield self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        tidal_session = await self._get_tidal_session()
        album: TidalAlbum  # satisfy the type checker
        async for album in self._iter_items(
            get_library_albums, tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT
        ):
            yield self._parse_album(album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        tidal_session = await self._get_tidal_session()
        track: TidalTrack  # satisfy the type checker
        async for track in self._iter_items(
            get_library_tracks, tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT
        ):
            yield self._parse_track(track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        tidal_session = await self._get_tidal_session()
        playlist: TidalPlaylist  # satisfy the type checker
        async for playlist in self._iter_items(
            get_library_playlists, tidal_session, self._tidal_user_id
        ):
            yield self._parse_playlist(playlist)

    @throttle_with_retries
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        tidal_session = await self._get_tidal_session()
        tracks_obj = await get_album_tracks(tidal_session, prov_album_id)
        return [self._parse_track(track_obj=track_obj) for track_obj in tracks_obj]

    @throttle_with_retries
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        tidal_session = await self._get_tidal_session()
        artist_albums_obj = await get_artist_albums(tidal_session, prov_artist_id)
        return [self._parse_album(album) for album in artist_albums_obj]

    @throttle_with_retries
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        tidal_session = await self._get_tidal_session()
        try:
            artist_toptracks_obj = await get_artist_toptracks(tidal_session, prov_artist_id)
            return [self._parse_track(track) for track in artist_toptracks_obj]
        except tidal_exceptions.ObjectNotFound as err:
            self.logger.warning(f"Failed to get toptracks for artist {prov_artist_id}: {err}")
            return []

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        tidal_session = await self._get_tidal_session()
        result: list[Track] = []
        page_size = 200
        offset = page * page_size
        track_obj: TidalTrack  # satisfy the type checker
        tidal_tracks = await get_playlist_tracks(
            tidal_session, prov_playlist_id, limit=page_size, offset=offset
        )
        for index, track_obj in enumerate(tidal_tracks, 1):
            track = self._parse_track(track_obj=track_obj)
            track.position = offset + index
            result.append(track)
        return result

    @throttle_with_retries
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks for given track id."""
        tidal_session = await self._get_tidal_session()
        similar_tracks_obj = await get_similar_tracks(tidal_session, prov_track_id, limit)
        return [self._parse_track(track) for track in similar_tracks_obj]

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library."""
        tidal_session = await self._get_tidal_session()
        return await library_items_add_remove(
            tidal_session,
            str(self._tidal_user_id),
            item.item_id,
            item.media_type,
            add=True,
        )

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        tidal_session = await self._get_tidal_session()
        return await library_items_add_remove(
            tidal_session,
            str(self._tidal_user_id),
            prov_item_id,
            media_type,
            add=False,
        )

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        tidal_session = await self._get_tidal_session()
        await add_playlist_tracks(tidal_session, prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        tidal_session = await self._get_tidal_session()
        prov_track_ids: list[str] = []
        # Get tracks by position
        for pos in positions_to_remove:
            tracks = await get_playlist_tracks(
                tidal_session, prov_playlist_id, limit=1, offset=pos - 1
            )
            if tracks and len(tracks) > 0:
                prov_track_ids.append(str(tracks[0].id))

        if prov_track_ids:
            await remove_playlist_tracks(tidal_session, prov_playlist_id, prov_track_ids)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        tidal_session = await self._get_tidal_session()
        playlist_obj = await create_playlist(
            session=tidal_session,
            user_id=str(self._tidal_user_id),
            title=name,
            description="",
        )
        return self._parse_playlist(playlist_obj)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        tidal_session = await self._get_tidal_session()
        # make sure a valid track is requested.
        # Try direct track lookup first with exception handling
        try:
            track = await get_track(tidal_session, item_id)
        except MediaNotFoundError:
            # Fallback to ISRC lookup
            self.logger.info(
                """Track %s not found, attempting fallback by ISRC.
                It's likely that this track has a new ID upstream in Tidal's WebApp.""",
                item_id,
            )
            track = await self._get_track_by_isrc(item_id, tidal_session)
            if not track:
                raise MediaNotFoundError(f"Track {item_id} not found")

        stream: TidalStream = await get_stream(track)
        manifest = stream.get_stream_manifest()

        url = (
            # for mpeg-dash streams we just pass the complete base64 manifest
            f"data:application/dash+xml;base64,{manifest.manifest}"
            if stream.is_mpd
            # as far as I can oversee a BTS stream is just a single URL
            else manifest.urls[0]
        )

        return StreamDetails(
            item_id=track.id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(manifest.codecs),
                sample_rate=manifest.sample_rate,
                bit_depth=stream.bit_depth,
                channels=2,
            ),
            stream_type=StreamType.HTTP,
            duration=track.duration,
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    @throttle_with_retries
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details for given artist id."""
        tidal_session = await self._get_tidal_session()
        try:
            artist_obj = await get_artist(tidal_session, prov_artist_id)
            return self._parse_artist(artist_obj)
        except tidal_exceptions.ObjectNotFound as err:
            raise MediaNotFoundError from err

    @throttle_with_retries
    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details for given album id."""
        tidal_session = await self._get_tidal_session()
        try:
            album_obj = await get_album(tidal_session, prov_album_id)
            return self._parse_album(album_obj)
        except tidal_exceptions.ObjectNotFound as err:
            raise MediaNotFoundError from err

    @throttle_with_retries
    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details for given track id."""
        tidal_session = await self._get_tidal_session()
        track_obj = await get_track(tidal_session, prov_track_id)
        try:
            track = self._parse_track(track_obj)
            # get some extra details for the full track info
            with suppress(tidal_exceptions.MetadataNotAvailable, AttributeError):
                lyrics: TidalLyrics = await get_track_lyrics(tidal_session, prov_track_id)
                if lyrics and hasattr(lyrics, "text"):
                    track.metadata.lyrics = lyrics.text
            return track
        except tidal_exceptions.ObjectNotFound as err:
            raise MediaNotFoundError from err

    @throttle_with_retries
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details for given playlist id."""
        tidal_session = await self._get_tidal_session()
        playlist_obj = await get_playlist(tidal_session, prov_playlist_id)
        return self._parse_playlist(playlist_obj)

    def get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        """Create a generic item mapping."""
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.lookup_key,
            name=name,
        )

    async def _get_tidal_session(self) -> TidalSession:
        """Ensure the current token is valid and return a tidal session."""
        if (
            self._tidal_session
            and self._tidal_session.access_token
            and datetime.fromisoformat(str(self.config.get_value(CONF_EXPIRY_TIME)))
            > (datetime.now() + timedelta(days=1))
        ):
            return self._tidal_session

        try:
            self._tidal_session = await self._load_tidal_session(
                token_type="Bearer",
                quality=str(self.config.get_value(CONF_QUALITY)),
                access_token=str(self.config.get_value(CONF_AUTH_TOKEN)),
                refresh_token=str(self.config.get_value(CONF_REFRESH_TOKEN)),
                expiry_time=datetime.fromisoformat(str(self.config.get_value(CONF_EXPIRY_TIME))),
            )
        except Exception as err:
            if "401 Client Error: Unauthorized" in str(err):
                err_msg = "Credentials expired, you need to re-setup"
                # clear stored creds
                self.update_config_value(CONF_AUTH_TOKEN, None)
                self.update_config_value(CONF_REFRESH_TOKEN, None)
                # if we're already loaded and the login got invalid, we need to unload
                if self.available:
                    self.unload_with_error(err_msg)
                raise LoginFailed(err_msg)
            raise

        self.update_config_value(
            CONF_AUTH_TOKEN,
            self._tidal_session.access_token,
            encrypted=True,
        )
        self.update_config_value(
            CONF_REFRESH_TOKEN,
            self._tidal_session.refresh_token,
            encrypted=True,
        )
        self.update_config_value(
            CONF_EXPIRY_TIME,
            self._tidal_session.expiry_time.isoformat(),
        )
        return self._tidal_session

    async def _load_tidal_session(
        self,
        token_type: str,
        quality: str,
        access_token: str,
        refresh_token: str,
        expiry_time: datetime | None = None,
    ) -> TidalSession:
        """Load the tidalapi Session."""

        def inner() -> TidalSession:
            config = TidalConfig(quality=quality, item_limit=10000, alac=False)
            session = TidalSession(config=config)
            session.load_oauth_session(
                token_type=token_type,
                access_token=access_token,
                refresh_token=refresh_token,
                expiry_time=expiry_time,
                is_pkce=True,
            )
            return session

        return await asyncio.to_thread(inner)

    async def _get_track_by_isrc(
        self, item_id: str, tidal_session: TidalSession
    ) -> TidalTrack | None:
        """Get track by ISRC from library item, with caching."""
        # Try to get from cache first
        cache_key = f"isrc_map_{item_id}"
        cached_track_id = await self.mass.cache.get(
            cache_key, category=CACHE_CATEGORY_DEFAULT, base_key=self.lookup_key
        )

        if cached_track_id:
            self.logger.debug(
                "Using cached track id",
            )
            try:
                return await get_track(tidal_session, str(cached_track_id))
            except MediaNotFoundError:
                # Track no longer exists, invalidate cache
                await self.mass.cache.delete(
                    cache_key, category=CACHE_CATEGORY_DEFAULT, base_key=self.lookup_key
                )

        # Lookup by ISRC if no cache or cached track not found
        library_track = await self.mass.music.tracks.get_library_item_by_prov_id(
            item_id, self.instance_id
        )
        if not library_track:
            return None

        isrc = next(
            (
                id_value
                for id_type, id_value in library_track.external_ids
                if id_type == ExternalID.ISRC
            ),
            None,
        )
        if not isrc:
            return None

        self.logger.debug("Attempting track lookup by ISRC: %s", isrc)
        tracks: list[TidalTrack] = await get_tracks_by_isrc(tidal_session, isrc)
        if not tracks:
            return None

        # Cache the mapping for future use
        await self.mass.cache.set(
            cache_key, tracks[0].id, category=CACHE_CATEGORY_DEFAULT, base_key=self.lookup_key
        )

        return tracks[0]

    # Parsers

    def _parse_artist(self, artist_obj: TidalArtist) -> Artist:
        """Parse tidal artist object to generic layout."""
        artist_id = artist_obj.id
        artist = Artist(
            item_id=str(artist_id),
            provider=self.lookup_key,
            name=artist_obj.name,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    # NOTE: don't use the /browse endpoint as it's
                    # not working for musicbrainz lookups
                    url=f"https://tidal.com/artist/{artist_id}",
                )
            },
        )
        # metadata
        if artist_obj.picture:
            picture_id = artist_obj.picture.replace("-", "/")
            image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )

        return artist

    def _parse_album(self, album_obj: TidalAlbum) -> Album:
        """Parse tidal album object to generic layout."""
        name = album_obj.name
        version = album_obj.version or ""
        album_id = album_obj.id
        album = Album(
            item_id=str(album_id),
            provider=self.lookup_key,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.FLAC,
                    ),
                    url=f"https://tidal.com/album/{album_id}",
                    available=album_obj.available,
                )
            },
        )
        various_artist_album: bool = False
        for artist_obj in album_obj.artists:
            if artist_obj.name == "Various Artists":
                various_artist_album = True
            album.artists.append(self._parse_artist(artist_obj))

        if album_obj.type == "COMPILATION" or various_artist_album:
            album.album_type = AlbumType.COMPILATION
        elif album_obj.type == "ALBUM":
            album.album_type = AlbumType.ALBUM
        elif album_obj.type == "EP":
            album.album_type = AlbumType.EP
        elif album_obj.type == "SINGLE":
            album.album_type = AlbumType.SINGLE

        album.year = int(album_obj.year)
        # metadata
        if album_obj.universal_product_number:
            album.external_ids.add((ExternalID.BARCODE, album_obj.universal_product_number))
        album.metadata.copyright = album_obj.copyright
        album.metadata.explicit = album_obj.explicit
        album.metadata.popularity = album_obj.popularity
        if album_obj.cover:
            picture_id = album_obj.cover.replace("-", "/")
            image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )

        return album

    def _parse_track(
        self,
        track_obj: TidalTrack,
    ) -> Track:
        """Parse tidal track object to generic layout."""
        version = track_obj.version or ""
        track_id = str(track_obj.id)
        track = Track(
            item_id=str(track_id),
            provider=self.lookup_key,
            name=track_obj.name,
            version=version,
            duration=track_obj.duration,
            provider_mappings={
                ProviderMapping(
                    item_id=str(track_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.FLAC,
                        bit_depth=24 if track_obj.is_hi_res_lossless else 16,
                    ),
                    url=f"https://tidal.com/track/{track_id}",
                    available=track_obj.available,
                )
            },
            disc_number=track_obj.volume_num or 0,
            track_number=track_obj.track_num or 0,
        )
        if track_obj.isrc:
            track.external_ids.add((ExternalID.ISRC, track_obj.isrc))
        track.artists = UniqueList()
        for track_artist in track_obj.artists:
            artist = self._parse_artist(track_artist)
            track.artists.append(artist)
        # metadata
        track.metadata.explicit = track_obj.explicit
        track.metadata.popularity = track_obj.popularity
        track.metadata.copyright = track_obj.copyright
        if track_obj.album:
            # Here we use an ItemMapping as Tidal returns
            # minimal data when getting an Album from a Track
            track.album = self.get_item_mapping(
                media_type=MediaType.ALBUM,
                key=str(track_obj.album.id),
                name=track_obj.album.name,
            )
            if track_obj.album.cover:
                picture_id = track_obj.album.cover.replace("-", "/")
                image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
                track.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=image_url,
                            provider=self.lookup_key,
                            remotely_accessible=True,
                        )
                    ]
                )
        return track

    def _parse_playlist(self, playlist_obj: TidalPlaylist) -> Playlist:
        """Parse tidal playlist object to generic layout."""
        playlist_id = playlist_obj.id
        creator_id = playlist_obj.creator.id if playlist_obj.creator else None
        creator_name = playlist_obj.creator.name if playlist_obj.creator else "Tidal"
        is_editable = bool(creator_id and str(creator_id) == self._tidal_user_id)
        playlist = Playlist(
            item_id=str(playlist_id),
            provider=self.instance_id if is_editable else self.lookup_key,
            name=playlist_obj.name,
            owner=creator_name,
            provider_mappings={
                ProviderMapping(
                    item_id=str(playlist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{BROWSE_URL}/playlist/{playlist_id}",
                )
            },
            is_editable=is_editable,
        )
        # metadata
        playlist.cache_checksum = str(playlist_obj.last_updated)
        playlist.metadata.popularity = playlist_obj.popularity
        if picture := (playlist_obj.square_picture or playlist_obj.picture):
            picture_id = picture.replace("-", "/")
            image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )

        return playlist

    async def _iter_items(
        self,
        func: Callable[_P, list[_R]] | Callable[_P, Awaitable[list[_R]]],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> AsyncGenerator[_R, None]:
        """Yield all items from a larger listing."""
        offset = 0
        while True:
            if asyncio.iscoroutinefunction(func):
                chunk = await func(*args, **kwargs, offset=offset)  # type: ignore[arg-type]
            else:
                chunk = await asyncio.to_thread(func, *args, **kwargs, offset=offset)  # type: ignore[arg-type]
            offset += len(chunk)
            for item in chunk:
                yield item
            if len(chunk) < DEFAULT_LIMIT:
                break

    async def _get_media_info(
        self, item_id: str, url: str, force_refresh: bool = False
    ) -> AudioTags:
        """Retrieve (cached) mediainfo for track."""
        cache_category = CACHE_CATEGORY_MEDIA_INFO
        cache_base_key = self.lookup_key
        # do we have some cached info for this url ?
        cached_info = await self.mass.cache.get(
            item_id, category=cache_category, base_key=cache_base_key
        )
        if cached_info and not force_refresh:
            media_info = AudioTags.parse(cached_info)
        else:
            # parse info with ffprobe (and store in cache)
            media_info = await async_parse_tags(url)
            await self.mass.cache.set(
                item_id,
                media_info.raw,
                category=cache_category,
                base_key=cache_base_key,
            )
        return media_info
