"""蒙自供水网上营业厅异步客户端(只读轮询)。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_BASE_URL, ARTICLE_ID
from .protocol import (
    AuthExpired,
    Household,
    MengziWaterError,
    apply_payload,
    build_payload,
    build_query,
    check_return_data,
    extract_relations,
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


class MengziWaterApi:
    """与蒙自城镇供水网上营业厅交互的只读客户端。"""

    def __init__(self, hass, session_cookie: str, timeout: int = 25) -> None:
        self._hass = hass
        self._session = async_get_clientsession(hass)
        self._cookie = session_cookie.strip()
        self._timeout = timeout

    # ------------------------------------------------------------------
    async def _post(self, queries: list[dict]) -> dict:
        payload = build_payload(queries)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": self._cookie,
            "Origin": "https://swp.mzczgs.com",
            "Referer": "https://swp.mzczgs.com/Hall/",
            "User-Agent": ("Mozilla/5.0 (Linux; Android 16; wv) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Mobile MicroMessenger/8.0.77"),
        }
        try:
            resp = await self._session.post(
                API_BASE_URL, data=payload, headers=headers, timeout=self._timeout
            )
            text = await resp.text()
        except Exception as err:  # noqa: BLE001
            raise MengziWaterError(f"网络请求失败: {err}") from err

        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise MengziWaterError(f"响应解析失败(HTTP {resp.status})") from err

        rd = data.get("ReturnData") or {}
        check_return_data(rd)
        return rd

    # ------------------------------------------------------------------
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
