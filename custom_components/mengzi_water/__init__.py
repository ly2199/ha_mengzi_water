"""蒙自城镇供水 (Mengzi Water) 集成入口。"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_COOKIE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .water_api import AuthExpired, Household, MengziWaterApi, MengziWaterError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


class MengziWaterCoordinator(DataUpdateCoordinator[dict[str, Household]]):
    """轮询所有绑定户号数据(基础+欠费+账单统计)。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: MengziWaterApi) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(int(interval), 300)),
        )
        self._api = api
        self.price_standard: dict = {}

    async def _async_update_data(self) -> dict[str, Household]:
        try:
            households = await self._api.fetch_all()
            price = await self._api.fetch_price_standard()
        except AuthExpired as err:
            raise ConfigEntryAuthFailed(f"会话失效，请在集成中重新认证: {err}") from err
        except MengziWaterError as err:
            raise UpdateFailed(str(err)) from err

        self.price_standard = price
        result: dict[str, Household] = {}
        for h in households:
            key = h.customer_no or h.customer_id
            if key:
                result[key] = h
        return result

    @property
    def api(self) -> MengziWaterApi:
        return self._api


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = MengziWaterApi(hass, entry.data.get(CONF_COOKIE, ""))
    coordinator = MengziWaterCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
