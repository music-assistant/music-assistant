"""
HTTP REST API standalone plugin.

Plugin provider for Music Assistant that exposes an additional HTTP port on
9508 (by default) listen to REST requests on the /api endpoint.
"""

from typing import TYPE_CHECKING, cast

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider

from .request_model import LevelSetPoint, ResourceURI, SwitchState, TimeSecondsNumber

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models import ProviderInstanceType


CONF_REST_PORT = "restapi_port"
CONF_REST_IP = "restapi_addr"


async def setup(
    mass: MusicAssistant, manifest: "ProviderManifest", config: "ProviderConfig"
) -> "ProviderInstanceType":
    """Create an instance of the plugin provider."""
    return RestApiPluginProvider(mass, manifest, config)


# ruff: noqa: ARG001
async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
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
            description="Listening port to bind the HTTP API server to: please choose a free port.",
            value=cast(str, values.get(CONF_REST_PORT)) if values else None,
        ),
        ConfigEntry(
            key=CONF_REST_IP,
            type=ConfigEntryType.STRING,
            label="Address",
            required=False,
            default_value="0.0.0.0",
            description="Listening address to bind the HTTP API server to:"
            " use '127.0.0.1' to allow only internal communication",
            value=cast(str, values.get(CONF_REST_IP)) if values else None,
        ),
    )
    return base_entries


class RestApiPluginProvider(PluginProvider):
    """Main class for the plugin provider instance."""

    def __init__(
        self, mass: MusicAssistant, manifest: "ProviderManifest", config: "ProviderConfig"
    ) -> None:
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
"""  # noqa: E501
        tags_metadata = [
            {
                "name": "player",
                "description": "Given a player name, the user can change its state; otherwise, a list of all player names and IDs is available.",  # noqa: E501
            },
            {
                "name": "queues",
                "description": "Given a player name, the user can add and play a media source directly, clear the queue or manage shuffle settings.",  # noqa: E501
            },
        ]
        self.app = FastAPI(
            title="Music Assistant REST API",
            description=description,
            summary="Plugin provider with independent HTTP interface",
            version="1.0.0",
            docs_url="/api/swagger",
            redoc_url=None,
            openapi_url="/api/openapi.json",
            openapi_tags=tags_metadata,
        )
        self.rest_router = APIRouter()
        self.register_routes()
        self.app.include_router(self.rest_router, prefix="/api/v1")
        self.server = uvicorn.Server(
            config=uvicorn.Config(
                self.app,
                host=cast(str, self.config.get_value(CONF_REST_IP)),
                port=cast(int, self.config.get_value(CONF_REST_PORT)),
                log_level="info",
                log_config=None,
            )
        )

    def register_routes(self) -> None:  # noqa: PLR0915
        """Set up the API endpoint URIs with their methods."""

        @self.rest_router.get(
            "/players",
            tags=["player"],
            summary="Retrieve a list of player IDs and their names",
        )
        def list_players() -> list[dict[str, str]]:
            players = self.mass.players.all()
            if len(players) == 0:
                raise HTTPException(status_code=404, detail="No player was found")
            return [{"player_id": player.player_id, "name": player.name} for player in players]

        @self.rest_router.post(
            "/player/{player_name}/play",
            tags=["player"],
            summary="Set the status of a specific player to 'play'",
        )
        async def play(player_name: str) -> dict[str, str]:
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_play(player.player_id)
                return {"status": "playing", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to play on player '{player_name}': {e}")
                raise HTTPException(status_code=500, detail=f"Failed to execute play: {e}") from e

        @self.rest_router.post(
            "/player/{player_name}/pause",
            tags=["player"],
            summary="Set the status of a specific player to 'pause'",
        )
        async def pause(player_name: str) -> dict[str, str]:
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_pause(player.player_id)
                return {"status": "paused", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to pause on player '{player_name}': {e}")
                raise HTTPException(status_code=500, detail=f"Failed to execute pause: {e}") from e

        @self.rest_router.post(
            "/player/{player_name}/stop",
            tags=["player"],
            summary="Set the status of a specific player to 'stop'",
        )
        async def stop(player_name: str) -> dict[str, str]:
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_stop(player.player_id)
                return {"status": "stopped", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to stop on player '{player_name}': {e}")
                raise HTTPException(status_code=500, detail=f"Failed to execute stop: {e}") from e

        @self.rest_router.post(
            "/player/{player_name}/seek",
            tags=["player"],
            summary="Seek the source of a specific player for the specified seconds from start",
        )
        async def seek(player_name: str, body: TimeSecondsNumber) -> dict[str, str]:
            position = body.seconds
            if position < 0:
                raise HTTPException(
                    status_code=400, detail="The number of seconds must be positive!"
                )
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_seek(player.player_id, position)
                return {"status": "seeked", "player_name": player_name, "position": str(position)}
            except Exception as e:
                self.logger.exception(f"Failed to seek on player '{player_name}': {e}")
                raise HTTPException(status_code=500, detail=f"Failed to execute seek: {e}") from e

        @self.rest_router.post(
            "/player/{player_name}/volume",
            tags=["player"],
            description="Set the volume of a specific player from 0 to 100",
        )
        async def volume(player_name: str, body: LevelSetPoint) -> dict[str, str]:
            volume = body.level
            if not 0 <= volume <= 100:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid 'volume'. Expected an integer between 0 and 100.",
                )
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_volume_set(player.player_id, volume)
                return {"volume": str(volume), "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to set volume on player '{player_name}': {e}")
                raise HTTPException(status_code=500, detail=f"Failed to execute volume: {e}") from e

        @self.rest_router.post(
            "/player/{player_name}/power",
            tags=["player"],
            description="Set the power state of a specific player to either 'true' or 'false'",
        )
        async def power(player_name: str, body: SwitchState) -> dict[str, str]:
            state = body.state
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            try:
                await self.mass.players.cmd_power(player.player_id, state)
                return {"power": str(state), "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed power on player '{player_name}': {e}")
                raise HTTPException(status_code=500, detail=f"Failed to execute power: {e}") from e

        @self.rest_router.post(
            "/queues/{player_name}/play_media",
            tags=["queues"],
            description="Set the queue for a specific player to the media source provided",
        )
        async def play_media(player_name: str, body: ResourceURI) -> dict[str, str]:
            media_uri = body.uri
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            active_queue = self.mass.player_queues.get(player.player_id)
            if active_queue is None:
                raise HTTPException(status_code=404, detail="Active queue not found for the player")
            try:
                await self.mass.player_queues.play_media(active_queue.queue_id, media_uri)
                return {"status": "enqueued", "player_name": player_name}
            except Exception as e:
                self.logger.exception(
                    f"Failed to enqueue '{media_uri}' on player '{player_name}': {e}"
                )
                raise HTTPException(
                    status_code=500, detail=f"Failed to execute play_media: {e}"
                ) from e

        @self.rest_router.post(
            "/queues/{player_name}/clear",
            tags=["queues"],
            description="Clear the media queue for a specific player",
        )
        def clear(player_name: str) -> dict[str, str]:
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            try:
                active_queue = self.mass.player_queues.get(player.player_id)
                if active_queue is None:
                    raise HTTPException(
                        status_code=404, detail="Active queue not found for the player"
                    )
                self.mass.player_queues.clear(active_queue.queue_id)
                return {"status": "cleared", "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed to clear queue on player '{player_name}': {e}")
                raise HTTPException(status_code=500, detail=f"Failed to execute clear: {e}") from e

        @self.rest_router.post(
            "/queues/{player_name}/shuffle",
            tags=["queues"],
            description="Set the shuffle mode for a media queue of a specific player to either 'true' or 'false'",  # noqa: E501
        )
        def shuffle(player_name: str, body: SwitchState) -> dict[str, str]:
            state = body.state
            player = self.mass.players.get_by_name(player_name)
            if not player:
                raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found")
            active_queue = self.mass.player_queues.get(player.player_id)
            if active_queue is None:
                raise HTTPException(status_code=404, detail="Active queue not found for the player")
            try:
                self.mass.player_queues.set_shuffle(active_queue.queue_id, state)
                return {"shuffle": str(state), "player_name": player_name}
            except Exception as e:
                self.logger.exception(f"Failed shuffle on player '{player_name}': {e}")
                raise HTTPException(
                    status_code=500, detail=f"Failed to execute shuffle: {e}"
                ) from e

    async def loaded_in_mass(self) -> None:
        """Start the API server when the plugin is loaded."""
        self.mass.create_task(self.server.serve())
        self.logger.info("Custom API server running")

    async def unload(self, is_removed: bool = False) -> None:
        """Stop the API server when the plugin is unloaded."""
        if hasattr(self, "server"):
            await self.server.shutdown()
