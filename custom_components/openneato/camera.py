"""Compatibility camera entities for Home Assistant dashboards.

The replay card is the richer interface, but camera entities remain useful:
Home Assistant can add them to a dashboard directly from the device page and
many existing vacuum cards accept a camera entity.  Both cameras expose the
new mapper's live, two-second LIDAR preview while cleaning and its persisted
plan after docking.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import OpenNeatoEntity


async def async_setup_entry(
    hass,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up dashboard-compatible map cameras."""
    data = hass.data[DOMAIN][entry.entry_id]
    common = {
        "coordinator": data["coordinator"],
        "serial": data["serial"],
        "model": data["model"],
        "sw_version": data["sw_version"],
        "fw_version": data["fw_version"],
        "host": data["host"],
        "mapper": data.get("mapper"),
    }
    async_add_entities(
        [
            OpenNeatoLidarCamera(**common),
            OpenNeatoReplayCamera(**common),
        ]
    )


class _OpenNeatoMapCamera(OpenNeatoEntity, Camera):
    """Base camera serving the mapper's current PNG."""

    _attr_content_type = "image/png"
    _attr_frame_interval = 2.0

    def __init__(
        self,
        coordinator,
        serial: str,
        mapper,
        model: str | None = None,
        sw_version: str | None = None,
        fw_version: str | None = None,
        host: str | None = None,
    ) -> None:
        OpenNeatoEntity.__init__(
            self,
            coordinator,
            serial,
            model=model,
            sw_version=sw_version,
            fw_version=fw_version,
            host=host,
        )
        Camera.__init__(self)
        self._mapper = mapper

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Report whether this is the active run or a saved plan."""
        return {
            "map_source": "live" if getattr(self._mapper, "live_revision", 0) else "saved",
            "live_refresh_seconds": 2,
        }

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the current live or persisted LIDAR map."""
        if self._mapper is None:
            return None
        rendered = await self._mapper.async_render()
        return rendered[0] if rendered else None


class OpenNeatoLidarCamera(_OpenNeatoMapCamera):
    """LIDAR map camera retained for existing dashboards."""

    _attr_name = "LIDAR map"
    _attr_translation_key = "lidar_map"

    def __init__(self, *args, serial: str, **kwargs) -> None:
        super().__init__(*args, serial=serial, **kwargs)
        self._attr_unique_id = f"{serial}_lidar_map"


class OpenNeatoReplayCamera(_OpenNeatoMapCamera):
    """Cleaning replay compatibility camera using the live map preview."""

    _attr_name = "Cleaning replay"
    _attr_translation_key = "motion_map"

    def __init__(self, *args, serial: str, **kwargs) -> None:
        super().__init__(*args, serial=serial, **kwargs)
        self._attr_unique_id = f"{serial}_motion_map"
