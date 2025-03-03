"""Tidal music provider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar, cast

from aiohttp import ClientResponse
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
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
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
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

from music_assistant.constants import CACHE_CATEGORY_DEFAULT
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    throttle_with_retries,
)
from music_assistant.models.music_provider import MusicProvider

from .auth_manager import ManualAuthenticationHelper, TidalAuthManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

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

CONF_AUTH_DATA = "auth_data"

# Labels
LABEL_START_PKCE_LOGIN = "start_pkce_login_label"
LABEL_OOPS_URL = "oops_url_label"
LABEL_COMPLETE_PKCE_LOGIN = "complete_pkce_login_label"

BROWSE_URL = "https://tidal.com/browse"
RESOURCES_URL = "https://resources.tidal.com/images"

DEFAULT_LIMIT = 50

T = TypeVar("T")


class TidalQualityEnum(StrEnum):
    """Enum for Tidal Quality."""

    HIGH_LOSSLESS = "LOSSLESS"  # "High - 16bit, 44.1kHz"
    HI_RES = "HI_RES_LOSSLESS"  # "Max - Up to 24bit, 192kHz"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return TidalProvider(mass, manifest, config)


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
        async with ManualAuthenticationHelper(mass, cast(str, values["session_id"])) as auth_helper:
            quality = str(values.get(CONF_QUALITY))
            base64_session = await TidalAuthManager.generate_auth_url(auth_helper, quality)
            values[CONF_TEMP_SESSION] = base64_session
            # Tidal is using the ManualAuthenticationHelper just to send the user to an URL
            # there is no actual oauth callback happening, instead the user is redirected
            # to a non-existent page and needs to copy the URL from the browser and paste it
            # we simply wait here to allow the user to start the auth
            await asyncio.sleep(15)

    if action == CONF_ACTION_COMPLETE_PKCE_LOGIN:
        quality = str(values.get(CONF_QUALITY))
        pkce_url = str(values.get(CONF_OOPS_URL))
        base64_session = str(values.get(CONF_TEMP_SESSION))
        auth_data = await TidalAuthManager.process_pkce_login(
            mass.http_session, base64_session, pkce_url
        )
        # set the retrieved token on the values object to pass along
        values[CONF_AUTH_DATA] = json.dumps(auth_data)
        values[CONF_TEMP_SESSION] = ""

    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_AUTH_DATA] = None

    if values.get(CONF_AUTH_DATA):
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

    # return the auth_data config entry
    return (
        *auth_entries,
        ConfigEntry(
            key=CONF_AUTH_DATA,
            type=ConfigEntryType.SECURE_STRING,
            label="Authentication data for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            hidden=True,
            value=values.get(CONF_AUTH_DATA) if values else None,
        ),
    )


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    BASE_URL: str = "https://api.tidal.com/v1"
    OPEN_API_URL: str = "https://openapi.tidal.com/v2/"

    throttler = ThrottlerManager(rate_limit=1, period=2)

    #
    # INITIALIZATION & SETUP
    #

    def __init__(self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig):
        """Initialize Tidal provider."""
        super().__init__(mass, manifest, config)
        self.auth = TidalAuthManager(
            http_session=mass.http_session,
            config_updater=self._update_auth_config,
            logger=self.logger,
        )

    def _update_auth_config(self, auth_info: dict[str, Any]) -> None:
        """Update auth config with new auth info."""
        self.update_config_value(CONF_AUTH_DATA, json.dumps(auth_info), encrypted=True)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Load auth info from config
        auth_data = cast(str, self.config.get_value(CONF_AUTH_DATA))
        if not auth_data:
            raise LoginFailed("Missing authentication data")

        # Initialize auth manager
        if not await self.auth.initialize(auth_data):
            raise LoginFailed("Failed to authenticate with Tidal")

        # Get user information from sessions API
        api_result = await self._get_data("sessions")
        user_info = self._extract_data(api_result)
        await self.auth.update_user_info(user_info)

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

    #
    # API REQUEST HELPERS & DECORATORS
    #

    @staticmethod
    def prepare_api_request(method: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        """Prepare API requests with authentication and common parameters."""

        @functools.wraps(method)
        async def wrapper(self: TidalProvider, endpoint: str, **kwargs: Any) -> T:
            # Ensure we have a valid token through auth manager
            if not await self.auth.ensure_valid_token():
                raise LoginFailed("Failed to authenticate with Tidal")

            # Add required parameters to every request
            params = kwargs.pop("params", {}) or {}

            # Add session ID and country code if available
            if self.auth.session_id:
                params["sessionId"] = self.auth.session_id

            if self.auth.country_code:
                params["countryCode"] = self.auth.country_code

            kwargs["params"] = params

            # Prepare headers
            headers = kwargs.pop("headers", {}) or {}
            headers["Authorization"] = f"Bearer {self.auth.access_token}"

            # Add locale headers
            locale = self.mass.metadata.locale.replace("_", "-")
            language = locale.split("-")[0]
            headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"
            kwargs["headers"] = headers

            return await method(self, endpoint, **kwargs)

        return wrapper

    @staticmethod
    def handle_item_errors(
        item_type: str,
    ) -> Callable[[Callable[..., Coroutine[Any, Any, T]]], Callable[..., Coroutine[Any, Any, T]]]:
        """Handle standard error patterns in item getters."""

        def decorator(
            method: Callable[..., Coroutine[Any, Any, T]],
        ) -> Callable[..., Coroutine[Any, Any, T]]:
            @functools.wraps(method)
            async def wrapper(self: TidalProvider, item_id: str, *args: Any, **kwargs: Any) -> T:
                try:
                    return await method(self, item_id, *args, **kwargs)
                except ResourceTemporarilyUnavailable:
                    raise
                except Exception as err:
                    raise MediaNotFoundError(f"{item_type} {item_id} not found") from err

            return wrapper

        return decorator

    #
    # CORE API METHODS
    #

    @throttle_with_retries
    @prepare_api_request
    async def _get_data(
        self, endpoint: str, **kwargs: Any
    ) -> dict[str, Any] | tuple[dict[str, Any], str]:
        """Get data from Tidal API using mass.http_session."""
        # Check if we want to return the ETag
        return_etag = kwargs.pop("return_etag", False)

        base_url = kwargs.pop("base_url", self.BASE_URL)
        url = f"{base_url}/{endpoint}"

        self.logger.debug("Making request to Tidal API: %s", endpoint)

        async with self.mass.http_session.get(url, **kwargs) as response:
            return await self._handle_response(response, return_etag)

    @prepare_api_request
    async def _post_data(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        as_form: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Post data to Tidal API using mass.http_session."""
        url = f"{self.BASE_URL}/{endpoint}"

        if as_form:
            # Set content type for form data
            headers = kwargs.get("headers", {})
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            kwargs["headers"] = headers
            # Use data parameter for form-encoded data
            async with self.mass.http_session.post(url, data=data, **kwargs) as response:
                return cast(
                    dict[str, Any],
                    await self._handle_response(response, return_etag=False),
                )
        else:
            # Use json parameter for JSON data (default)
            async with self.mass.http_session.post(url, json=data, **kwargs) as response:
                return cast(
                    dict[str, Any],
                    await self._handle_response(response, return_etag=False),
                )

    @prepare_api_request
    async def _delete_data(
        self, endpoint: str, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Delete data from Tidal API using mass.http_session."""
        url = f"{self.BASE_URL}/{endpoint}"
        self.logger.debug("Making DELETE request to Tidal API: %s", endpoint)

        # For DELETE requests with a body, we need to use json parameter
        async with self.mass.http_session.delete(url, json=data, **kwargs) as response:
            return cast(dict[str, Any], await self._handle_response(response, return_etag=False))

    async def _handle_response(
        self, response: ClientResponse, return_etag: bool = False
    ) -> dict[str, Any] | tuple[dict[str, Any], str]:
        """Handle API response and common error conditions."""
        # Handle error responses
        if response.status == 401:
            # Authentication error is handled by the calling method (which will retry)
            raise LoginFailed("Authentication failed")

        if response.status == 404:
            raise MediaNotFoundError(f"Item not found: {response.url}")

        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", 30))
            raise ResourceTemporarilyUnavailable(
                "Tidal Rate limit reached", backoff_time=retry_after
            )

        if response.status == 412:
            text = await response.text()
            self.logger.error("Precondition failed: %s", text)
            raise ResourceTemporarilyUnavailable(
                "Resource changed while updating, please try again"
            )

        if response.status >= 400:
            text = await response.text()
            self.logger.error("API error: %s - %s", response.status, text)
            raise ResourceTemporarilyUnavailable("API error")

        # Parse successful response
        try:
            # Check if there's content to parse
            if (
                response.content_length == 0
                or not response.content_type
                or response.content_type == ""
            ):
                # Empty response, return success indicator
                data = {"success": True}
            else:
                data = await response.json()

            # Return with etag if requested
            if return_etag:
                etag = response.headers.get("ETag", "")
                return data, etag
            return data
        except json.JSONDecodeError as err:
            self.logger.error("Failed to parse JSON response: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to parse response") from err
        except (TypeError, ValueError, KeyError) as err:
            self.logger.error("Invalid response format: %s", err)
            raise ResourceTemporarilyUnavailable("Invalid response format") from err

    async def _paginate_api(
        self,
        endpoint: str,
        item_key: str = "items",
        nested_key: str | None = None,
        limit: int = DEFAULT_LIMIT,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Paginate through all items from a Tidal API endpoint."""
        offset = 0
        while True:
            # Get a batch of items
            params = {"limit": limit, "offset": offset}
            if "params" in kwargs:
                params.update(kwargs.pop("params"))

            api_result = await self._get_data(endpoint, params=params, **kwargs)
            response = self._extract_data(api_result)

            # Extract items from response
            items = response.get(item_key, [])
            if not items:
                break

            # Process each item in the batch
            for item in items:
                if nested_key and nested_key in item and item[nested_key]:
                    yield item[nested_key]
                else:
                    yield item

            # Update offset for next batch
            offset += len(items)

            # Stop if we've received fewer items than the limit
            if len(items) < limit:
                break

    def _extract_data(
        self, api_result: dict[str, Any] | tuple[dict[str, Any], str]
    ) -> dict[str, Any]:
        """Extract data from API result that might be tuple of (data, etag)."""
        return api_result[0] if isinstance(api_result, tuple) else api_result

    def _extract_data_and_etag(
        self, api_result: dict[str, Any] | tuple[dict[str, Any], str]
    ) -> tuple[dict[str, Any], str | None]:
        """Extract both data and etag from API result."""
        if isinstance(api_result, tuple):
            return api_result
        return api_result, None

    #
    # SEARCH & DISCOVERY
    #

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

        api_result = await self._get_data(
            "search",
            params={
                "query": search_query.replace("'", ""),
                "limit": limit,
                "types": ",".join(media_types),
            },
        )

        # Handle potential tuple return (data, etag)
        results = self._extract_data(api_result)

        if results["artists"]:
            parsed_results.artists = [
                self._parse_artist(artist) for artist in results["artists"]["items"]
            ]
        if results["albums"]:
            parsed_results.albums = [
                self._parse_album(album) for album in results["albums"]["items"]
            ]
        if results["playlists"]:
            parsed_results.playlists = [
                self._parse_playlist(playlist) for playlist in results["playlists"]["items"]
            ]
        if results["tracks"]:
            parsed_results.tracks = [
                self._parse_track(track) for track in results["tracks"]["items"]
            ]
        return parsed_results

    @handle_item_errors("Track")
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks for given track id."""
        api_result = await self._get_data(f"tracks/{prov_track_id}/radio", params={"limit": limit})
        similar_tracks = self._extract_data(api_result)
        return [self._parse_track(track_obj) for track_obj in similar_tracks.get("items", [])]

    #
    # ITEM RETRIEVAL METHODS
    #

    @handle_item_errors("Artist")
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details for given artist id."""
        api_result = await self._get_data(f"artists/{prov_artist_id}")
        artist_obj = self._extract_data(api_result)
        return self._parse_artist(artist_obj)

    @handle_item_errors("Album")
    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details for given album id."""
        api_result = await self._get_data(f"albums/{prov_album_id}")
        album_obj = self._extract_data(api_result)
        return self._parse_album(album_obj)

    @handle_item_errors("Track")
    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details for given track id."""
        api_result = await self._get_data(f"tracks/{prov_track_id}")
        track_obj = self._extract_data(api_result)
        track = self._parse_track(track_obj)
        # Get additional details like lyrics if needed
        with suppress(MediaNotFoundError):
            api_result = await self._get_data(f"tracks/{prov_track_id}/lyrics")
            lyrics_data = self._extract_data(api_result)

            if lyrics_data and "text" in lyrics_data:
                track.metadata.lyrics = lyrics_data["text"]

        return track

    @handle_item_errors("Playlist")
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details for given playlist id."""
        api_result = await self._get_data(f"playlists/{prov_playlist_id}")
        playlist_obj = self._extract_data(api_result)
        return self._parse_playlist(playlist_obj)

    @handle_item_errors("Track")
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        api_result = await self._get_data(f"albums/{prov_album_id}/tracks")
        album_tracks = self._extract_data(api_result)
        return [self._parse_track(track_obj) for track_obj in album_tracks.get("items", [])]

    @handle_item_errors("Album")
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        api_result = await self._get_data(f"artists/{prov_artist_id}/albums")
        artist_albums = self._extract_data(api_result)
        return [self._parse_album(album_obj) for album_obj in artist_albums.get("items", [])]

    @handle_item_errors("Track")
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        api_result = await self._get_data(
            f"artists/{prov_artist_id}/toptracks", params={"limit": 10, "offset": 0}
        )
        artist_top_tracks = self._extract_data(api_result)
        return [self._parse_track(track_obj) for track_obj in artist_top_tracks.get("items", [])]

    @handle_item_errors("Playlist")
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        page_size = 200
        offset = page * page_size
        api_result = await self._get_data(
            f"playlists/{prov_playlist_id}/tracks",
            params={"limit": page_size, "offset": offset},
        )
        tidal_tracks = self._extract_data(api_result)
        for index, track_obj in enumerate(tidal_tracks.get("items", []), 1):
            track = self._parse_track(track_obj=track_obj)
            track.position = offset + index
            result.append(track)
        return result

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # Try direct track lookup first with exception handling
        try:
            track = await self.get_track(item_id)
        except MediaNotFoundError:
            self.logger.info(
                "Track %s not found, attempting fallback by ISRC lookup",
                item_id,
            )
            track_result = await self._get_track_by_isrc(item_id)
            if not track_result:
                raise MediaNotFoundError(f"Track {item_id} not found")
            track = track_result

        quality = self.config.get_value(CONF_QUALITY)

        # Request stream manifest
        async with self.throttler.bypass():
            api_result = await self._get_data(
                f"tracks/{item_id}/playbackinfopostpaywall",
                params={
                    "playbackmode": "STREAM",
                    "audioquality": quality,
                    "assetpresentation": "FULL",
                },
            )
        stream_data = self._extract_data(api_result)

        # Extract streaming information
        manifest_type = stream_data.get("manifestMimeType", "")
        is_mpd = "dash+xml" in manifest_type

        if is_mpd:
            url = f"data:application/dash+xml;base64,{stream_data['manifest']}"
        else:
            # For non-MPD streams, use the direct URL
            url = stream_data.get("urls", [None])[0]
            if not url:
                raise MediaNotFoundError(f"No stream URL for track {item_id}")

        # Determine audio format info
        codec = stream_data.get("codec", "")
        content_type = ContentType.try_parse(codec)
        bit_depth = 24 if "HI_RES_LOSSLESS" in stream_data.get("audioMode", "") else 16
        sample_rate = stream_data.get("sampleRate", 44100)

        return StreamDetails(
            item_id=track.item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=content_type,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
                channels=2,
            ),
            stream_type=StreamType.HTTP,
            duration=track.duration,
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_track_by_isrc(self, item_id: str) -> Track | None:
        """Get track by ISRC from library item, with caching."""
        # Try to get from cache first
        cache_key = f"isrc_map_{item_id}"
        cached_track_id = await self.mass.cache.get(
            cache_key, category=CACHE_CATEGORY_DEFAULT, base_key=self.lookup_key
        )

        if cached_track_id:
            self.logger.debug("Using cached track id")
            try:
                api_result = await self._get_data(f"tracks/{cached_track_id}")
                track_data = self._extract_data(api_result)
                return self._parse_track(track_data)
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

        # Get tracks by ISRC using direct API
        api_result = await self._get_data(
            "tracks",
            params={
                "filter[isrc]": isrc,
            },
            base_url=self.OPEN_API_URL,
        )
        tracks_data = self._extract_data(api_result)

        if not tracks_data and not tracks_data.get("data"):
            return None

        track_data = tracks_data["data"][0]
        track_id = str(track_data["id"])

        # Cache the mapping for future use
        await self.mass.cache.set(
            cache_key,
            track_id,
            category=CACHE_CATEGORY_DEFAULT,
            base_key=self.lookup_key,
        )

        return await self.get_track(track_id)

    def get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        """Create a generic item mapping."""
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.lookup_key,
            name=name,
        )

    #
    # LIBRARY MANAGEMENT
    #

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        user_id = self.auth.user_id
        path = f"users/{user_id}/favorites/artists"

        async for artist_item in self._paginate_api(path, nested_key="item"):
            if artist_item and artist_item.get("id"):
                yield self._parse_artist(artist_item)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        user_id = self.auth.user_id
        path = f"users/{user_id}/favorites/albums"

        async for album_item in self._paginate_api(path, nested_key="item"):
            if album_item and album_item.get("id"):
                yield self._parse_album(album_item)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        user_id = self.auth.user_id
        path = f"users/{user_id}/favorites/tracks"

        async for track_item in self._paginate_api(path, nested_key="item"):
            if track_item and track_item.get("id"):
                yield self._parse_track(track_item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        user_id = self.auth.user_id
        path = f"users/{user_id}/playlistsAndFavoritePlaylists"

        async for playlist_item in self._paginate_api(path, nested_key="playlist"):
            if playlist_item and playlist_item.get("uuid"):
                yield self._parse_playlist(playlist_item)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library."""
        endpoint = None
        data = {}

        if item.media_type == MediaType.ARTIST:
            endpoint = "favorites/artists"
            data = {"artistId": item.item_id}
        elif item.media_type == MediaType.ALBUM:
            endpoint = "favorites/albums"
            data = {"albumId": item.item_id}
        elif item.media_type == MediaType.TRACK:
            endpoint = "favorites/tracks"
            data = {"trackId": item.item_id}
        elif item.media_type == MediaType.PLAYLIST:
            endpoint = "favorites/playlists"
            data = {"playlistId": item.item_id}
        else:
            return False

        endpoint = f"users/{self.auth.user_id}/{endpoint}"

        await self._post_data(endpoint, data=data, as_form=True)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        endpoint = None

        if media_type == MediaType.ARTIST:
            endpoint = f"favorites/artists/{prov_item_id}"
        elif media_type == MediaType.ALBUM:
            endpoint = f"favorites/albums/{prov_item_id}"
        elif media_type == MediaType.TRACK:
            endpoint = f"favorites/tracks/{prov_item_id}"
        elif media_type == MediaType.PLAYLIST:
            endpoint = f"favorites/playlists/{prov_item_id}"
        else:
            return False

        endpoint = f"users/{self.auth.user_id}/{endpoint}"

        try:
            await self._delete_data(endpoint)
            return True
        except Exception:
            # Log but don't raise - just return False to indicate failure
            self.logger.warning("Failed to remove %s:%s library", media_type, prov_item_id)
            return False

    #
    # PLAYLIST MANAGEMENT
    #

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        # Create playlist using form-encoded data
        data = {"title": name, "description": ""}

        try:
            playlist_obj = await self._post_data(
                f"users/{self.auth.user_id}/playlists", data=data, as_form=True
            )

            return self._parse_playlist(playlist_obj)
        except Exception as err:
            self.logger.error("Failed to create playlist: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to create playlist") from err

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        try:
            # Get playlist details first with ETag
            api_result = await self._get_data(f"playlists/{prov_playlist_id}", return_etag=True)
            playlist_obj, etag = self._extract_data_and_etag(api_result)

            # Send using form-encoded data like the synchronous library
            data = {
                "onArtifactNotFound": "SKIP",
                "trackIds": ",".join(map(str, prov_track_ids)),
                "toIndex": playlist_obj["numberOfTracks"],
                "onDupes": "SKIP",
            }

            # Force using form data instead of JSON and include ETag
            headers = {"If-None-Match": etag} if etag else {}
            await self._post_data(
                f"playlists/{prov_playlist_id}/items",
                data=data,
                as_form=True,
                headers=headers,
            )

        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        # Get playlist with ETag first
        api_result = await self._get_data(f"playlists/{prov_playlist_id}", return_etag=True)
        _, etag = self._extract_data_and_etag(api_result)

        # Format positions as string in URL path
        # Tidal can use directly indices in path, not track IDs in the body
        position_string = ",".join([str(pos - 1) for pos in positions_to_remove])

        # Use DELETE with If-None-Match header
        # Tidal uses this incorrectly, but it's required
        headers = {"If-None-Match": etag} if etag else {}

        # Make a direct DELETE request to the endpoint with positions in the URL path
        await self._delete_data(
            f"playlists/{prov_playlist_id}/items/{position_string}", headers=headers
        )

    #
    # ITEM PARSERS
    #

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse tidal artist object to generic layout."""
        artist_id = str(artist_obj["id"])
        artist = Artist(
            item_id=artist_id,
            provider=self.lookup_key,
            name=artist_obj["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    # NOTE: don't use the /browse endpoint as it's
                    # not working for musicbrainz lookups
                    url=f"https://tidal.com/artist/{artist_id}",
                )
            },
        )
        # metadata
        if artist_obj["picture"]:
            picture_id = artist_obj["picture"].replace("-", "/")
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

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse tidal album object to generic layout."""
        name = album_obj["title"]
        version = album_obj["version"] or ""
        album_id = str(album_obj["id"])
        album = Album(
            item_id=album_id,
            provider=self.lookup_key,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.FLAC,
                    ),
                    url=f"https://tidal.com/album/{album_id}",
                    available=album_obj["streamReady"],
                )
            },
        )
        various_artist_album: bool = False
        for artist_obj in album_obj["artists"]:
            if artist_obj["name"] == "Various Artists":
                various_artist_album = True
            album.artists.append(self._parse_artist(artist_obj))

        if album_obj["type"] == "COMPILATION" or various_artist_album:
            album.album_type = AlbumType.COMPILATION
        elif album_obj["type"] == "ALBUM":
            album.album_type = AlbumType.ALBUM
        elif album_obj["type"] == "EP":
            album.album_type = AlbumType.EP
        elif album_obj["type"] == "SINGLE":
            album.album_type = AlbumType.SINGLE

        album.year = int(album_obj["releaseDate"].split("-")[0])
        # metadata
        if album_obj["upc"]:
            album.external_ids.add((ExternalID.BARCODE, album_obj["upc"]))
        album.metadata.copyright = album_obj["copyright"]
        album.metadata.explicit = album_obj["explicit"]
        album.metadata.popularity = album_obj["popularity"]
        if album_obj["cover"]:
            picture_id = album_obj["cover"].replace("-", "/")
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
        track_obj: dict[str, Any],
    ) -> Track:
        """Parse tidal track object to generic layout."""
        version = track_obj["version"] or ""
        track_id = str(track_obj["id"])
        hi_res_lossless = "HI_RES_LOSSLESS" in track_obj["mediaMetadata"]["tags"]
        track = Track(
            item_id=track_id,
            provider=self.lookup_key,
            name=track_obj["title"],
            version=version,
            duration=track_obj["duration"],
            provider_mappings={
                ProviderMapping(
                    item_id=str(track_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.FLAC,
                        bit_depth=24 if hi_res_lossless else 16,
                    ),
                    url=f"https://tidal.com/track/{track_id}",
                    available=track_obj["streamReady"],
                )
            },
            disc_number=track_obj["volumeNumber"] or 0,
            track_number=track_obj["trackNumber"] or 0,
        )
        if track_obj["isrc"]:
            track.external_ids.add((ExternalID.ISRC, track_obj["isrc"]))
        track.artists = UniqueList()
        for track_artist in track_obj["artists"]:
            artist = self._parse_artist(track_artist)
            track.artists.append(artist)
        # metadata
        track.metadata.explicit = track_obj["explicit"]
        track.metadata.popularity = track_obj["popularity"]
        track.metadata.copyright = track_obj["copyright"]
        if track_obj["album"]:
            # Here we use an ItemMapping as Tidal returns
            # minimal data when getting an Album from a Track
            track.album = self.get_item_mapping(
                media_type=MediaType.ALBUM,
                key=str(track_obj["album"]["id"]),
                name=track_obj["album"]["title"],
            )
            if track_obj["album"]["cover"]:
                picture_id = track_obj["album"]["cover"].replace("-", "/")
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

    def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        """Parse tidal playlist object to generic layout."""
        playlist_id = str(playlist_obj["uuid"])
        creator_id = None
        if playlist_obj["creator"]:
            creator_id = playlist_obj["creator"]["id"]
        is_editable = bool(creator_id and str(creator_id) == str(self.auth.user_id))
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id if is_editable else self.lookup_key,
            name=playlist_obj["title"],
            owner=creator_id or "Tidal",
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{BROWSE_URL}/playlist/{playlist_id}",
                )
            },
            is_editable=is_editable,
        )
        # metadata
        playlist.cache_checksum = str(playlist_obj["lastUpdated"])
        playlist.metadata.popularity = playlist_obj["popularity"]
        if picture := (playlist_obj["squareImage"] or playlist_obj["image"]):
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
