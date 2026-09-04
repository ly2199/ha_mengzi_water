"""蒙自供水网上营业厅异步客户端(只读轮询)。"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_BASE_URL
from .protocol import (
    AuthExpired,
    Household,
    MengziWaterError,
    apply_payload,
    build_payload,
    build_query,
    check_return_data,
    extract_relations,
    QRY_BILLS,
    QRY_BASE,
    QRY_CUSTOMER,
    QRY_NEEDPAY,
    QRY_RELATIONS,
    QRY_SWITCH,
)

_LOGGER = logging.getLogger(__name__)

__all__ = ["MengziWaterApi", "MengziWaterError", "AuthExpired", "Household"]


class MengziWaterApi:
    """与蒙自城镇供水网上营业厅交互的只读客户端。"""

    def __init__(self, hass, session_cookie: str, timeout: int = 20) -> None:
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

        import json

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

    async def fetch_all(self) -> list[Household]:
        """拉取全部绑定户号。每户号一次 RPC(切换+客户信息+欠费汇总+待缴账单+档案)。"""
        rd = await self._post([build_query(QRY_RELATIONS, "Hall.GetRelations")])
        households = extract_relations(rd)
        if not households:
            # 关系列表为空时保底:取当前默认户号
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

        result: list[Household] = []
        for h in households:
            try:
                rd = await self._post([
                    build_query(QRY_SWITCH, "Hall.SwitchCustom", [h.customer_id]),
                    build_query(QRY_CUSTOMER, "Hall.GetCustomerInfo"),
                    build_query(QRY_NEEDPAY, "Hall.GetNeedPayInfo"),
                    build_query(QRY_BILLS, "Hall.GetNoPayBillRecordList"),
                    build_query(QRY_BASE, "Hall.GetCustomerBaseInfo"),
                ])
                apply_payload(h, rd)
            except AuthExpired:
                raise
            except MengziWaterError as err:
                _LOGGER.warning("拉取户号 %s 失败: %s", h.customer_id[:8], err)
                continue
            result.append(h)
        return result
