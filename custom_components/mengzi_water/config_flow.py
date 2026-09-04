"""Config flow for Mengzi Water (蒙自城镇供水)."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_COOKIE, DOMAIN
from .water_api import AuthExpired, MengziWaterApi, MengziWaterError

_LOGGER = logging.getLogger(__name__)

SCHEMA_USER = vol.Schema({
    vol.Required(CONF_COOKIE): str,
    vol.Optional(CONF_NAME, default=""): str,
})


def _normalize_cookie(raw: str) -> str:
    """容忍用户粘贴整段 Cookie / 带引号 / 带空格的情况。"""
    value = raw.strip()
    for line in value.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        name, val = line.split("=", 1)
        if name.strip() == "Ares.Core.Session.JXSWPHALL" or name.strip().lower().startswith("ares.core"):
            return f"{name.strip()}={val.strip()}"
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if "=" not in value:
        return f"Ares.Core.Session.JXSWPHALL={value}"
    return value


async def _validate(hass: HomeAssistant, cookie: str) -> str:
    api = MengziWaterApi(hass, cookie)
    try:
        return await api.validate_session()
    except AuthExpired as err:
        raise InvalidAuth("会话已失效或已过期") from err
    except MengziWaterError as err:
        raise CannotConnect(str(err)) from err


class MengziWaterConfigFlow(ConfigFlow, domain=DOMAIN):
    """配置向导:粘贴微信登录后的会话 Cookie。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cookie = _normalize_cookie(user_input[CONF_COOKIE])
            try:
                info = await _validate(self.hass, cookie)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                title = user_input.get(CONF_NAME) or f"蒙自城镇供水 {info}"
                await self.async_set_unique_id(f"{DOMAIN}-{title}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data={CONF_COOKIE: cookie})

        return self.async_show_form(
            step_id="user",
            data_schema=SCHEMA_USER,
            errors=errors,
            description_placeholders={},
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            cookie = _normalize_cookie(user_input[CONF_COOKIE])
            try:
                await _validate(self.hass, cookie)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_COOKIE: cookie}
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema({vol.Required(CONF_COOKIE): str})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MengziWaterOptionsFlow(config_entry)


class MengziWaterOptionsFlow(OptionsFlow):
    """选项:轮询间隔。"""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL

        if user_input is not None:
            interval = max(int(user_input[CONF_SCAN_INTERVAL]), MIN_SCAN_INTERVAL)
            return self.async_create_entry(title="", data={CONF_SCAN_INTERVAL: interval})

        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    int, vol.Range(min=MIN_SCAN_INTERVAL)
                ),
            }),
        )


class InvalidAuth(HomeAssistantError):
    """Cookie 无效。"""


class CannotConnect(HomeAssistantError):
    """连接失败。"""
