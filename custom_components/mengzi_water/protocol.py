"""纯协议层(无 Home Assistant 依赖,可独立测试)。

报文格式(实测验证):
    POST https://swp.mzczgs.com/Hall/
    Content-Type: application/x-www-form-urlencoded
    Cookie: Ares.Core.Session.JXSWPHALL=...
    body: {"PostType":"1","Querys":[{QueryType,QueryReturnType,KeyName,FunctionName,Params,PageArgs}]}

响应: {"ReturnData":{"ReturnType":"1|2|0","ReturnString":"...","<KeyName>":{...}}}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 查询分组 KeyName(响应按同样 Key 返回)
QRY_SWITCH = "Switch"
QRY_CUSTOMER = "Customer"
QRY_NEEDPAY = "NeedPay"
QRY_BILLS = "Bills"
QRY_BASE = "Base"
QRY_RELATIONS = "Relations"

RT_SUCCESS = "1"
RT_SESSION_OUT = "2"
RT_ERROR = "0"


def page_args(page_index: int = 1, page_size: int = 10) -> dict:
    return {
        "PageIndex": str(page_index),
        "TotalRecord": "0",
        "TotalPage": "0",
        "RecordCountPerPage": str(page_size),
        "RecordCount": "0",
    }


def build_query(key: str, fun: str, params: list | None = None) -> dict:
    """按 H5 序列化规则构造单个 Query。普通参数一律 Single 字符串。"""
    return {
        "QueryType": "2",
        "QueryReturnType": "0",
        "KeyName": key,
        "FunctionName": fun,
        "Params": [
            {"Type": "0", "ParamName": "", "ParamValue": str(v)} for v in (params or [])
        ],
        "PageArgs": page_args(),
    }


def build_payload(queries: list[dict]) -> str:
    import json

    return json.dumps({"PostType": "1", "Querys": queries})


class MengziWaterError(Exception):
    """API 错误基类。"""


class AuthExpired(MengziWaterError):
    """会话失效(ReturnType=2)。"""


@dataclass
class Household:
    """一个绑定户号及其聚合数据。"""

    customer_id: str = ""
    label: str = ""
    customer_no: str = ""
    customer_name: str = ""
    address: str = ""
    balance: float = 0.0
    arrears: float = 0.0
    pending_bills: int = 0
    last_payment_time: str = ""
    stop_status: str = ""
    book_name: str = ""
    water_use_type: str = ""
    meter_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def check_return_data(rd: dict) -> None:
    """校验外层 ReturnData;会话失效/错误时抛异常。"""
    ret_type = rd.get("ReturnType")
    if ret_type == RT_SESSION_OUT:
        raise AuthExpired(rd.get("ReturnString") or "会话已失效,请重新抓取 Cookie")
    if ret_type != RT_SUCCESS:
        raise MengziWaterError(rd.get("ReturnString") or f"接口返回异常(ReturnType={ret_type})")


def _fmt_amount(v: Any) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _base_fields(rd: dict) -> dict:
    base = rd.get(QRY_BASE) or {}
    fields = base.get("Fields") if isinstance(base.get("Fields"), dict) else {}
    if fields:
        return fields
    return {k: v for k, v in base.items() if not isinstance(v, (dict, list))}


def apply_payload(h: Household, rd: dict) -> None:
    """把一次多查询响应聚合到 Household。"""
    h.raw = rd

    cust = (rd.get(QRY_CUSTOMER) or {}).get("Data") or {}
    h.customer_no = str(cust.get("customerNo") or "")
    h.customer_name = str(cust.get("customerName") or "")
    h.address = str(cust.get("Address") or "")
    if not h.label and h.customer_name:
        h.label = h.customer_name

    # 预存/待缴:欠费页汇总优先,其次首页客户信息,最后客户档案
    np = (rd.get(QRY_NEEDPAY) or {}).get("Data") or {}
    if np.get("balance") is not None:
        h.balance = _fmt_amount(np.get("balance"))
    elif cust.get("accountBalance") is not None:
        h.balance = _fmt_amount(cust.get("accountBalance"))
    else:
        h.balance = _fmt_amount(_base_fields(rd).get("ACCOUNT_BALANCE"))

    if np.get("arrearange") is not None:
        h.arrears = _fmt_amount(np.get("arrearange"))
    elif cust.get("money") is not None:
        h.arrears = _fmt_amount(cust.get("money"))

    # 待缴账单行(金额/用量字段名以服务端实测为准,这里做通用兜底)
    bills = rd.get(QRY_BILLS) or {}
    rows = bills.get("Rows") or []
    h.pending_bills = len(rows)
    if h.pending_bills and not h.arrears:
        for row in rows:
            if not isinstance(row, dict):
                continue
            for k, v in row.items():
                if isinstance(v, (int, float)) and "MONEY" in str(k).upper():
                    h.arrears += _fmt_amount(v)

    fields = _base_fields(rd)
    h.last_payment_time = str(fields.get("LAST_PAYMENT_TIME") or "")
    stop_text = str(cust.get("stopStatus") or "")
    if not stop_text or stop_text in ("", "None", "null"):
        try:
            stop_text = "停水" if int(fields.get("IS_STOP") or 0) else "正常"
        except (TypeError, ValueError):
            stop_text = "正常"
    h.stop_status = stop_text
    h.book_name = str(fields.get("BOOK_NAME") or "")
    h.water_use_type = str(fields.get("WATER_USE_TYPE") or "")
    try:
        h.meter_count = int(fields.get("WATER_METERS") or 0)
    except (TypeError, ValueError):
        h.meter_count = 0


def extract_relations(rd: dict) -> list[Household]:
    """从 Relations 响应取出绑定户号列表。"""
    rel = ((rd.get(QRY_RELATIONS) or {}).get("Data") or {}).get("List") or {}
    rows = rel.get("Rows") or []
    out: list[Household] = []
    for row in rows:
        cid = str(row.get("CUSTOMER_ID") or "")
        if not cid:
            continue
        out.append(Household(
            customer_id=cid,
            label=str(row.get("CUSTOMER_XX") or row.get("CUSTOMER_NAME") or cid),
        ))
    return out
