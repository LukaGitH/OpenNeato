"""Dashboard-compatible map cameras for OpenNeato."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, HISTORY_POLL_INTERVAL
from .coordinator import latest_completed_session
from .entity import OpenNeatoEntity
from .replay import build_replay_session, render_replay_map


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up dashboard-compatible map cameras."""
    data = hass.data[DOMAIN][entry.entry_id]
    common = {
        "coordinator": data["coordinator"], "serial": data["serial"],
        "model": data["model"], "sw_version": data["sw_version"],
        "fw_version": data["fw_version"], "host": data["host"],
    }
    async_add_entities([
        OpenNeatoLidarCamera(mapper=data.get("mapper"), **common),
        OpenNeatoReplayCamera(api=data["api"], **common),
    ])


class OpenNeatoLidarCamera(OpenNeatoEntity, Camera):
    """Camera showing the mapper's sparse wall plan."""

    _attr_name = "LIDAR map"
    _attr_translation_key = "lidar_map"
    _attr_content_type = "image/png"
    _attr_frame_interval = 2.0

    def __init__(self, coordinator, serial: str, mapper, **kwargs) -> None:
        OpenNeatoEntity.__init__(self, coordinator, serial, **kwargs)
        Camera.__init__(self)
        self._mapper = mapper
        self._attr_unique_id = f"{serial}_lidar_map"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "map_source": "live" if getattr(self._mapper, "live_revision", 0) else "saved",
            "live_refresh_seconds": 2,
        }

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        if self._mapper is None:
            return None
        rendered = await self._mapper.async_render()
        return rendered[0] if rendered else None


class OpenNeatoReplayCamera(OpenNeatoEntity, Camera):
    """Camera showing the robot's recorded path and cleaned coverage."""

    _attr_name = "Cleaning replay"
    _attr_translation_key = "motion_map"
    _attr_content_type = "image/png"
    _attr_frame_interval = float(HISTORY_POLL_INTERVAL)

    def __init__(self, coordinator, api, serial: str, **kwargs) -> None:
        OpenNeatoEntity.__init__(self, coordinator, serial, **kwargs)
        Camera.__init__(self)
        self._api = api
        self._attr_unique_id = f"{serial}_motion_map"
        self._image: bytes | None = None
        self._session_name: str | None = None
        self._recording = False
        self._busy = False
        self._unsub_timer = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "map_source": "recording" if self._recording else "history",
            "live_refresh_seconds": HISTORY_POLL_INTERVAL if self._recording else None,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sync_polling()
        self.hass.async_create_task(self._async_refresh())

    async def async_will_remove_from_hass(self) -> None:
        self._stop_polling()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_polling()
        super()._handle_coordinator_update()

    @callback
    def _sync_polling(self) -> None:
        state = ((self.coordinator.data or {}).get("state") or {}).get("uiState", "")
        active = "CLEANINGRUNNING" in state or "MANUALCLEANING" in state
        if active and self._unsub_timer is None:
            self._unsub_timer = async_track_time_interval(
                self.hass, self._async_refresh, timedelta(seconds=HISTORY_POLL_INTERVAL)
            )
            self.hass.async_create_task(self._async_refresh())
        elif not active:
            self._stop_polling()
            latest = latest_completed_session((self.coordinator.data or {}).get("history"))
            if latest and latest.get("name") != self._session_name:
                self.hass.async_create_task(self._async_refresh())

    @callback
    def _stop_polling(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    async def _async_refresh(self, _now=None) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            history = await self._api.get_history()
            recording = next(
                (item for item in history if isinstance(item, dict) and item.get("recording")), None
            )
            selected = recording or latest_completed_session(history)
            if not selected or not selected.get("name"):
                return
            name = selected["name"]
            raw = await self._api.get_history_session(name)
            parsed = await self.hass.async_add_executor_job(build_replay_session, raw, name)
            image = await self.hass.async_add_executor_job(render_replay_map, parsed, bool(recording))
            if image is None:
                return
            self._image = image
            self._session_name = name
            self._recording = bool(recording)
            self.async_write_ha_state()
        finally:
            self._busy = False

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._image
