"""蒙自供水网上营业厅异步客户端(只读轮询,支持会话自动续期)。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_BASE_URL, ARTICLE_ID, COOKIE_NAME
from .protocol import (
    AuthExpired,
    Household,
    MengziWaterError,
    RT_SESSION_OUT,
    RT_SUCCESS,
    apply_payload,
    build_payload,
    build_query,
    extract_relations,
    extract_session_cookie,
    page_param,
    parse_article,
    single_param,
    QRY_ARTICLE,
    QRY_BILLS,
    QRY_BASE,
    QRY_CUSTOMER,
    QRY_HIST0,
    QRY_HIST1,
    QRY_NEEDPAY,
    QRY_RELATIONS,
    QRY_SWITCH,
)

_LOGGER = logging.getLogger(__name__)

__all__ = ["MengziWaterApi", "MengziWaterError", "AuthExpired", "Household"]

_COOKIE_PREFIX = COOKIE_NAME + "="


class MengziWaterApi:
    """与蒙自城镇供水网上营业厅交互的只读客户端。"""

    def __init__(
        self,
        hass,
        session_cookie: str,
        timeout: int = 25,
        on_cookie_renewed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._hass = hass
        self._session = async_get_clientsession(hass)
        self._cookie = session_cookie.strip()
        self._timeout = timeout
        self.on_cookie_renewed = on_cookie_renewed

    def set_cookie(self, cookie: str) -> None:
        """更新内存中的会话 Cookie。"""
        if cookie:
            self._cookie = cookie.strip()

    async def _absorb_set_cookie(self, resp) -> None:
        """服务端回发 Set-Cookie 时(滑动续期),吸收并持久化。"""
        try:
            values = resp.headers.getall("Set-Cookie", [])
        except Exception:  # noqa: BLE001
            return
        new_cookie = extract_session_cookie(values)
        if not new_cookie or new_cookie == self._cookie:
            return
        old = self._cookie
        self._cookie = new_cookie
        _LOGGER.info("会话 Cookie 已被服务端轮换,自动吸收续期")
        if self.on_cookie_renewed is not None:
            try:
                await self.on_cookie_renewed(new_cookie)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("持久化新会话 Cookie 失败(将沿用内存值): %s", err)
        elif not old:
            pass

    # ------------------------------------------------------------------
    async def _post(self, queries: list[dict], use_cookie: bool = True,
                    _attempts: int = 3) -> dict:
        """POST 并校验响应。

        服务端偶发短暂返回“会话失效/空响应”(Cookie 仍有效),先重试;
        连续失败才抛 AuthExpired。每次成功响应吸收 Set-Cookie 轮换。
        """
        import asyncio

        payload = build_payload(queries)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://swp.mzczgs.com",
            "Referer": "https://swp.mzczgs.com/Hall/",
            "User-Agent": ("Mozilla/5.0 (Linux; Android 16; wv) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Mobile MicroMessenger/8.0.77"),
        }
        if use_cookie and self._cookie:
            headers["Cookie"] = self._cookie
        last_err: Exception | None = None
        for attempt in range(1, _attempts + 1):
            try:
                resp = await self._session.post(
                    API_BASE_URL, data=payload, headers=headers, timeout=self._timeout
                )
                text = await resp.text()
                data = json.loads(text)
                rd = data.get("ReturnData") or {}
            except json.JSONDecodeError:
                last_err = MengziWaterError(f"响应解析失败(HTTP {getattr(resp, 'status', '?')})")
            except Exception as err:  # noqa: BLE001
                last_err = MengziWaterError(f"网络请求失败: {err}")
            else:
                await self._absorb_set_cookie(resp)
                ret = rd.get("ReturnType")
                if ret == RT_SUCCESS:
                    return rd
                msg = rd.get("ReturnString") or f"接口返回异常(ReturnType={ret})"
                if ret == RT_SESSION_OUT:
                    if attempt < _attempts:
                        _LOGGER.warning(
                            "供水服务端第 %s 次返回会话失效(可能为瞬时误报),稍后重试: %s",
                            attempt, msg,
                        )
                    else:
                        raise AuthExpired(msg)
                else:
                    last_err = MengziWaterError(msg)
            if attempt < _attempts:
                await asyncio.sleep(1.0)
        raise last_err or MengziWaterError("未知请求错误")

    # ------------------------------------------------------------------
    async def renew_session_with_openid(self, open_id: str) -> bool:
        """用微信 openId 调登录接口刷新服务端活跃状态。

        实测:会话 Cookie 是固定值,服务端按“最近一次登录/活跃”判定会话是否有效,
        微信端每次打开营业厅都会自动调用本接口。HA 定期调用即可等效保活,
        无需微信在线。返回 True 表示登录接口成功(无论是否回发新 Cookie)。"""
        try:
            resp = await self._session.post(
                API_BASE_URL,
                data=build_payload([build_query("Login", "HallEx.Login",
                                                 [single_param(open_id)])]),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://swp.mzczgs.com",
                    "Referer": "https://swp.mzczgs.com/Hall/",
                    "User-Agent": ("Mozilla/5.0 (Linux; Android 16; wv) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Mobile MicroMessenger/8.0.77"),
                },
                timeout=self._timeout,
            )
            text = await resp.text()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("openId 保活请求失败: %s", err)
            return False

        new_cookie = extract_session_cookie(resp.headers.getall("Set-Cookie", []))
        if new_cookie:
            self._cookie = new_cookie
            _LOGGER.info("openId 保活成功且服务端轮换了会话 Cookie")
        try:
            data = json.loads(text)
            rd = data.get("ReturnData") or {}
        except json.JSONDecodeError:
            _LOGGER.warning("openId 保活响应无法解析")
            return False
        if rd.get("ReturnType") == RT_SUCCESS:
            _LOGGER.debug("openId 保活成功(ReturnType=1)")
            return True
        _LOGGER.warning("openId 保活未成功: ReturnType=%s %s",
                        rd.get("ReturnType"), rd.get("ReturnString"))
        return False

    async def validate_session(self) -> str:
        """校验会话,返回默认户号显示信息(配置向导用)。"""
        rd = await self._post([build_query(QRY_CUSTOMER, "Hall.GetCustomerInfo")])
        data = (rd.get(QRY_CUSTOMER) or {}).get("Data") or {}
        no = str(data.get("customerNo") or "")
        name = str(data.get("customerName") or "")
        if no:
            return f"{name}(户号 {no})"
        return "会话有效"

    async def fetch_price_standard(self) -> dict:
        """拉取水价公示文章(失败返回空结构,不影响主流程)。"""
        try:
            rd = await self._post([
                build_query(QRY_ARTICLE, "HallEx.GetArticleDetail",
                            [single_param(ARTICLE_ID)])
            ])
            return parse_article(rd)
        except AuthExpired:
            raise
        except MengziWaterError as err:
            _LOGGER.warning("拉取水价公示失败: %s", err)
            return {"title": "水价公示", "lines": []}

    async def fetch_all(self) -> list[Household]:
        """拉取全部绑定户号:基础信息 + 欠费 + 当年/上一年历史账单。"""
        rd = await self._post([build_query(QRY_RELATIONS, "Hall.GetRelations")])
        households = extract_relations(rd)
        if not households:
            rd = await self._post([build_query(QRY_CUSTOMER, "Hall.GetCustomerInfo")])
            data = (rd.get(QRY_CUSTOMER) or {}).get("Data") or {}
            if data.get("customerNo"):
                h = Household(
                    customer_id=str(data.get("customerID") or ""),
                    label="默认户号",
                )
                apply_payload(h, rd)
                return [h]
            return []

        now = datetime.now()
        years = [str(now.year), str(now.year - 1)]
        result: list[Household] = []
        for h in households:
            try:
                rd = await self._post([
                    build_query(QRY_SWITCH, "Hall.SwitchCustom",
                                [single_param(h.customer_id)]),
                    build_query(QRY_CUSTOMER, "Hall.GetCustomerInfo"),
                    build_query(QRY_NEEDPAY, "Hall.GetNeedPayInfo"),
                    build_query(QRY_BILLS, "Hall.GetNoPayBillRecordList"),
                    build_query(QRY_BASE, "Hall.GetCustomerBaseInfo"),
                    build_query(QRY_HIST0, "Hall.GetBillRecordListByPage",
                                [page_param(), single_param(years[0])]),
                    build_query(QRY_HIST1, "Hall.GetBillRecordListByPage",
                                [page_param(), single_param(years[1])]),
                ])
                apply_payload(h, rd)
            except AuthExpired:
                raise
            except MengziWaterError as err:
                _LOGGER.warning("拉取户号 %s 失败: %s", h.customer_id[:8], err)
                continue
            result.append(h)
        return result
