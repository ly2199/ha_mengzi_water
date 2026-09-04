"""蒙自城镇供水 (Mengzi Water) 集成入口。"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_COOKIE,
    CONF_OPENID,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .water_api import AuthExpired, Household, MengziWaterApi, MengziWaterError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


class MengziWaterCoordinator(DataUpdateCoordinator[dict[str, Household]]):
    """轮询所有绑定户号数据(基础+欠费+账单统计),并自动续期会话。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: MengziWaterApi) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(int(interval), 300)),
        )
        self._api = api
        self._entry = entry
        self._options_snapshot = dict(entry.options)
        self.price_standard: dict = {}
        self._renewed_in_cycle = False

    @property
    def api(self) -> MengziWaterApi:
        return self._api

    @property
    def open_id(self) -> str:
        return str(self._entry.data.get(CONF_OPENID) or "").strip()

    def options_changed(self) -> bool:
        return dict(self._entry.options) != self._options_snapshot

    def remember_options(self) -> None:
        self._options_snapshot = dict(self._entry.options)

    async def _persist_cookie(self, cookie: str) -> None:
        """把服务端轮换的新 Cookie 写入配置条目(仅数据更新,不触发整体重载)。"""
        old = str(self._entry.data.get(CONF_COOKIE) or "")
        if cookie == old:
            return
        _LOGGER.info("持久化轮换后的会话 Cookie")
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_COOKIE: cookie}
        )

    async def _try_renew_with_openid(self) -> bool:
        """openId 重新登录换取新会话;成功则立即重试拉取。"""
        open_id = self.open_id
        if not open_id or self._renewed_in_cycle:
            return False
        _LOGGER.info("检测到会话失效,尝试用 openId 自动重新登录")
        new_cookie = await self._api.renew_session_with_openid(open_id)
        if not new_cookie:
            return False
        await self._persist_cookie(new_cookie)
        self._renewed_in_cycle = True
        return True

    async def _async_update_data(self) -> dict[str, Household]:
        self._renewed_in_cycle = False
        try:
            households = await self._api.fetch_all()
        except AuthExpired:
            if await self._try_renew_with_openid():
                try:
                    households = await self._api.fetch_all()
                except AuthExpired as err:
                    raise ConfigEntryAuthFailed(
                        f"会话失效且 openId 重新登录后仍失败: {err}"
                    ) from err
            else:
                raise ConfigEntryAuthFailed(
                    "会话失效:请重新抓取 Cookie;如已配置 openId 将自动续期"
                )
        except MengziWaterError as err:
            raise UpdateFailed(str(err)) from err

        try:
            price = await self._api.fetch_price_standard()
        except AuthExpired:
            if await self._try_renew_with_openid():
                try:
                    price = await self._api.fetch_price_standard()
                except AuthExpired as err:
                    raise ConfigEntryAuthFailed(
                        f"会话失效且 openId 重新登录后仍失败: {err}"
                    ) from err
            else:
                raise ConfigEntryAuthFailed("会话失效:请重新抓取 Cookie")
        self.price_standard = price

        result: dict[str, Household] = {}
        for h in households:
            key = h.customer_no or h.customer_id
            if key:
                result[key] = h
        return result


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = MengziWaterApi(
        hass,
        entry.data.get(CONF_COOKIE, ""),
        on_cookie_renewed=_make_cookie_persister(hass, entry),
    )
    coordinator = MengziWaterCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _make_cookie_persister(hass: HomeAssistant, entry: ConfigEntry):
    async def persist(cookie: str) -> None:
        old = str(entry.data.get(CONF_COOKIE) or "")
        if cookie == old:
            return
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_COOKIE: cookie}
        )

    return persist


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项变更才整体重载;Cookie 自动续期(仅数据变更)不重载。"""
    coordinator: MengziWaterCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None and not coordinator.options_changed():
        coordinator.remember_options()
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
