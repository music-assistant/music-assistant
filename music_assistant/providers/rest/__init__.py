"""
HTTP REST API standalone plugin.

Plugin provider for Music Assistant that exposes an additional HTTP port on
9508 (by default) listen to REST requests on the /api endpoint.
"""

from typing import TYPE_CHECKING, cast

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Request
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models import ProviderInstanceType


CONF_REST_PORT = "restapi_port"


async def setup(mass: MusicAssistant,
                manifest: "ProviderManifest",
                config: "ProviderConfig") -> "ProviderInstanceType":
    """Create an instance of the plugin provider."""
    return RestApiPluginProvider(mass, manifest, config)


async def get_config_entries(mass: MusicAssistant,
                             instance_id: str | None = None,
                             action: str | None = None,
                             values: dict[str, ConfigValueType] | None = None
                             ) -> tuple[ConfigEntry, ...]:
    """Return config options required for setting up this provider."""
    base_entries: tuple[ConfigEntry, ...]
    base_entries = (
        ConfigEntry(
            key=CONF_REST_PORT,
            type=ConfigEntryType.INTEGER,
            label="Port",
            required=False,
            default_value=9508,
            description="Listening port to bind the HTTP API server to:"
            " please choose a free port.",
            value=cast(str, values.get(CONF_REST_PORT)) if values else None,
        ),
    )
    return base_entries


class RestApiPluginProvider(PluginProvider):
    """Main class for the plugin provider instance."""

    def __init__(self,
                 mass: MusicAssistant,
                 manifest: "ProviderManifest",
                 config: "ProviderConfig") -> None:
        """Instance constructor; sets up also the external HTTP server."""
        super().__init__(mass, manifest, config)
        description = """
This service lets you interact with applicative elements of the main service instance exposed through an HTTP web server

## Read methods

You can get a list of players and their IDs.

## Write methods

The following interactions are implemented

* Manage a player's state (including power and volume, and seek).
* Change a player's queue contents and shuffle settings.
* Clear a player's queue
"""
        tags_metadata = [
            {"name": "player",
             "description": "Given a player name, the user can change its state; otherwise, a list of all player names and IDs is available.",},
            {"name": "queues",
             "description": "Given a player name, the user can add and play a media source directly, clear the queue or manage shuffle settings."}
        ]
        self.app = FastAPI(
            title="Music Assistant REST API",
            description=description,
            summary="Plugin provider with independent HTTP interface",
            version="1.0.0",
            docs_url="/api/v1/swagger",
            redoc_url=None,
            openapi_url="/api/v1/openapi.json",
            openapi_tags=tags_metadata,
        )
        self.rest_router = APIRouter()
        self.register_routes()
        self.app.include_router(self.rest_router, prefix="/api/v1")
        self.server = uvicorn.Server(
            config=uvicorn.Config(
                self.app,
                host="0.0.0.0",
                port=cast(int, self.config.get_value(CONF_REST_PORT)),
                log_level="info",
                log_config=None
            )
        )

    def register_routes(self) -> None:
        """Set up the API endpoint URIs with their methods."""

        @self.rest_router.get("/players", tags=["player"])
        def list_players() -> list[dict[str, str]]:
            players = self.mass.players.all()
            if len(players) == 0:
                raise HTTPException(status_code=404,
                                    detail="No player was found")
            return [{"player_id": player.player_id, "name": player.name} for player in players]

        @self.rest_router.post("/player/{player_name}/play", tags=["player"])
        async def play(player_name: str) -> dict[str, str]:
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_play(player.player_id)
                return {"status": "playing", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to play on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute play: {e}") from e

        @self.rest_router.post("/player/{player_name}/pause", tags=["player"])
        async def pause(player_name: str) -> dict[str, str]:
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_pause(player.player_id)
                return {"status": "paused", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to pause on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute pause: {e}") from e

        @self.rest_router.post("/player/{player_name}/stop", tags=["player"])
        async def stop(player_name: str) -> dict[str, str]:
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_stop(player.player_id)
                return {"status": "stopped", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to stop on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute stop: {e}") from e

        @self.rest_router.post("/player/{player_name}/seek", tags=["player"])
        async def seek(player_name: str, request: Request) -> dict[str, str]:
            payload = await request.json()
            position = payload.get("position")
            if position is None:
                raise HTTPException(status_code=400,
                                    detail="Missing 'position' in request")
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_seek(player.player_id, position)
                return {"status": "seeked",
                        "player_name": player_name,
                        "position": position}
            except Exception as e:
                self.logger.exception(f"Failed to seek on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute seek: {e}") from e

        @self.rest_router.post("/player/{player_name}/volume", tags=["player"])
        async def volume(player_name: str, request: Request) -> dict[str, str]:
            payload = await request.json()
            volume = payload.get("volume")
            if volume is not None and not 0 <= volume <= 100:
                raise HTTPException(status_code=400,
                                    detail="Invalid 'volume'."
                                    " Expected an integer between 1 and 100.")
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_volume_set(player.player_id, volume)
                return {"volume": str(volume), "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to set volume on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute volume: {e}") from e

        @self.rest_router.post("/player/{player_name}/power", tags=["player"])
        async def power(player_name: str, request: Request) -> dict[str, str]:
            payload = await request.json()
            state = payload.get("state")
            if state not in ("on", "off"):
                raise HTTPException(status_code=400,
                                    detail="Invalid 'state'. Expected 'on' or 'off'.")
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_power(player.player_id, state == "on")
                return {"status": f"power_{state}", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed power on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute power: {e}") from e

        @self.rest_router.post("/queues/{player_name}/play_media", tags=["queues"])
        async def play_media(player_name: str, request: Request) -> dict[str, str]:
            try:
                payload = await request.json()
                media_uri = payload.get("media_uri")
            except Exception as e:
                raise HTTPException(status_code=400,
                                    detail=f"Unable to red the POST contents: {e}") from e
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                active_queue = self.mass.player_queues.get(player.player_id)
                await self.mass.player_queues.play_media(active_queue.queue_id, media_uri)
                return {"status": "enqueued", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to enqueue '{media_uri}' on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute play_media: {e}") from e

        @self.rest_router.post("/queues/{player_name}/clear", tags=["queues"])
        async def clear(player_name: str) -> dict[str, str]:
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                active_queue = self.mass.player_queues.get(player.player_id)
                await self.mass.player_queues.clear(active_queue.queue_id)
                return {"status": "cleared", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to clear queue on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute clear: {e}") from e

        @self.rest_router.post("/queues/{player_name}/shuffle", tags=["queues"])
        async def shuffle(player_name: str, request: Request) -> dict[str, str]:
            payload = await request.json()
            state = payload.get("state")
            if state not in ("on", "off"):
                raise HTTPException(status_code=400,
                                    detail="Invalid 'state'. Expected 'on' or 'off'.")
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404,
                                    detail=f"Player '{player_name}' not found")
            try:
                active_queue = self.mass.player_queues.get(player.player_id)
                self.mass.player_queues.set_shuffle(active_queue.queue_id, state == "on")
                return {"status": f"shuffle_{state}", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed shuffle on player '{player_name}': {e}")
                raise HTTPException(status_code=500,
                                    detail=f"Failed to execute shuffle: {e}") from e

    async def loaded_in_mass(self) -> None:
        """Start the API server when the plugin is loaded."""
        self.mass.create_task(self.server.serve())
        self.logger.info("Custom API server running")

    async def unload(self, is_removed: bool = False) -> None:
        """Stop the API server when the plugin is unloaded."""
        if hasattr(self, "server"):
            await self.server.shutdown()
