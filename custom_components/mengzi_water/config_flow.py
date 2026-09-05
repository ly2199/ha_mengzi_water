"""Config flow for Mengzi Water (蒙自城镇供水)."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_COOKIE, CONF_NAME, CONF_OPENID, DOMAIN
from .water_api import AuthExpired, MengziWaterApi, MengziWaterError

_LOGGER = logging.getLogger(__name__)

OPEN_ID_HINT = (
    "openId 获取方法:手机微信打开营业厅完成登录后,抓包搜索 openId "
    "(HallEx.Authorize 响应 Data.openId,形如 o 开头约 28 位)。"
    "填了 openId 后,Cookie 可留空 —— 集成会自动登录并保存新会话。"
)

SCHEMA_USER = vol.Schema({
    vol.Optional(CONF_COOKIE, default=""): str,
    vol.Optional(CONF_OPENID, default=""): str,
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


async def _resolve_session(hass: HomeAssistant, cookie_raw: str, open_id: str) -> tuple[str, str]:
    """返回 (最终Cookie, 户号信息)。

    优先用提供的 Cookie;Cookie 无效/为空且给了 openId 时,自动用 openId
    登录换取新会话 Cookie(实测服务端会签发新 Cookie)。
    """
    cookie = _normalize_cookie(cookie_raw) if cookie_raw.strip() else ""
    api = MengziWaterApi(hass, cookie)
    if cookie:
        try:
            info = await api.validate_session()
            return api.cookie, info
        except AuthExpired:
            pass  # Cookie 失效,尝试 openId 登录
        except MengziWaterError as err:
            raise CannotConnect(str(err)) from err

    open_id = open_id.strip()
    if not open_id:
        raise InvalidAuth("Cookie 无效或已过期,且未填写 openId")
    try:
        new_cookie = await api.login_with_openid(open_id)
    except MengziWaterError as err:
        raise CannotConnect(str(err)) from err
    if not new_cookie:
        raise InvalidAuth("openId 登录未取得新会话,请检查 openId 是否正确")
    try:
        info = await api.validate_session()
    except AuthExpired as err:
        raise InvalidAuth("openId 登录后会话仍不可用,请检查 openId") from err
    except MengziWaterError as err:
        raise CannotConnect(str(err)) from err
    return new_cookie, info


class MengziWaterConfigFlow(ConfigFlow, domain=DOMAIN):
    """配置向导:粘贴微信登录后的会话 Cookie。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            open_id = str(user_input.get(CONF_OPENID) or "").strip()
            try:
                cookie, info = await _resolve_session(
                    self.hass, str(user_input.get(CONF_COOKIE) or ""), open_id
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                title = user_input.get(CONF_NAME) or f"蒙自城镇供水 {info}"
                await self.async_set_unique_id(f"{DOMAIN}-{title}")
                self._abort_if_unique_id_configured()
                data = {CONF_COOKIE: cookie}
                if open_id:
                    data[CONF_OPENID] = open_id
                return self.async_create_entry(title=title, data=data)

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
            openid = str(user_input.get(CONF_OPENID) or "").strip() or str(
                entry.data.get(CONF_OPENID) or ""
            ).strip()
            try:
                cookie, _info = await _resolve_session(
                    self.hass, str(user_input.get(CONF_COOKIE) or ""), openid
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                data = {**entry.data, CONF_COOKIE: cookie}
                if openid:
                    data[CONF_OPENID] = openid
                else:
                    data.pop(CONF_OPENID, None)
                self.hass.config_entries.async_update_entry(entry, data=data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema({
            vol.Optional(CONF_COOKIE, default=str(entry.data.get(CONF_COOKIE) or "")): str,
            vol.Optional(CONF_OPENID, default=str(entry.data.get(CONF_OPENID) or "")): str,
        })
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"openid_hint": OPEN_ID_HINT},
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
