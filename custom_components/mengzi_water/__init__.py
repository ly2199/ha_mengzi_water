"""蒙自城镇供水 (Mengzi Water) 集成入口。"""
from __future__ import annotations

import asyncio
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
    KEEPALIVE_INTERVAL,
)
from .water_api import AuthExpired, Household, MengziWaterApi, MengziWaterError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


class MengziWaterCoordinator(DataUpdateCoordinator[dict[str, Household]]):
    """轮询所有绑定户号数据;openId 会话保活由独立常驻任务负责,不依赖轮询周期。"""

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
        self._keepalive_task: asyncio.Task | None = None

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

    # ------------------------------------------------------------------
    # 会话保活(独立常驻任务)
    # ------------------------------------------------------------------
    def start_keepalive(self) -> None:
        """启动保活循环:立即报到一次,之后每 KEEPALIVE_INTERVAL 一次。"""
        if self._keepalive_task is not None or not self.open_id:
            return
        self._keepalive_task = self.hass.asyncio.create_task(self._keepalive_loop())
        _LOGGER.info("openId 会话保活任务已启动(每 %s 秒报到一次)", KEEPALIVE_INTERVAL)

    async def _keepalive_loop(self) -> None:
        while True:
            try:
                await self._keepalive()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("会话保活异常: %s", err)
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _keepalive(self) -> bool:
        """用 openId 调登录接口刷新服务端活跃状态,并把新会话持久化。"""
        open_id = self.open_id
        if not open_id:
            return False
        ok = await self._api.renew_session_with_openid(open_id)
        if ok:
            # 服务端会签发新会话 Cookie:立即写入配置,重启后仍有效
            await self._persist_cookie(self._api.cookie)
            _LOGGER.debug("会话保活成功")
        else:
            _LOGGER.warning("会话保活失败(openId 无效或网络异常),将在下个周期重试")
        return ok

    async def _persist_cookie(self, cookie: str) -> None:
        """把服务端轮换的新 Cookie 写入配置条目(仅数据更新,不触发整体重载)。"""
        old = str(self._entry.data.get(CONF_COOKIE) or "")
        if cookie == old:
            return
        _LOGGER.info("持久化轮换后的会话 Cookie")
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_COOKIE: cookie}
        )

    def cancel_keepalive(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    # ------------------------------------------------------------------
    # 数据轮询
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Household]:
        try:
            households = await self._api.fetch_all()
        except AuthExpired:
            # 拉取确认为会话失效:立即保活一次再重试
            if await self._keepalive():
                try:
                    households = await self._api.fetch_all()
                except AuthExpired as err:
                    raise ConfigEntryAuthFailed(
                        f"会话失效且 openId 保活后仍失败: {err}"
                    ) from err
            else:
                raise ConfigEntryAuthFailed(
                    "会话失效:请检查 openId 是否正确;未配置 openId 时请重新抓取 Cookie"
                )
        except MengziWaterError as err:
            raise UpdateFailed(str(err)) from err

        try:
            price = await self._api.fetch_price_standard()
        except AuthExpired:
            if await self._keepalive():
                try:
                    price = await self._api.fetch_price_standard()
                except AuthExpired as err:
                    raise ConfigEntryAuthFailed(
                        f"会话失效且 openId 保活后仍失败: {err}"
                    ) from err
            else:
                raise ConfigEntryAuthFailed("会话失效:请重新抓取 Cookie 或检查 openId")
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
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        # 重启时存储的 Cookie 可能已失效:有 openId 就先保活换新会话再试一次
        if coordinator.open_id and await coordinator._keepalive():
            await coordinator.async_config_entry_first_refresh()
        else:
            raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    coordinator.start_keepalive()
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
    coordinator: MengziWaterCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        coordinator.cancel_keepalive()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
