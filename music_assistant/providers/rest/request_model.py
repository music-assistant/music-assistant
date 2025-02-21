"""Definitions for REST API types."""

from pydantic import BaseModel, Field


class LevelSetPoint(BaseModel):  # noqa: D101
    level: int = Field(..., description="Integer level between 0 and 100", example=15)  # type: ignore [call-overload]


class TimeSecondsNumber(BaseModel):  # noqa: D101
    seconds: int = Field(..., description="Number of seconds from the start", example=120)  # type: ignore[call-overload]


class SwitchState(BaseModel):  # noqa: D101
    state: bool = Field(
        ...,
        description="Either 'true' or 'false', representing the ON and OFF states respectively",
        example=True,
    )  # type: ignore[call-overload]


class ResourceURI(BaseModel):  # noqa: D101
    uri: str = Field(
        ...,
        description="String representing the path of the target resource",
        example="library://playlist/6",
    )  # type: ignore[call-overload]
