"""纯协议层(无 Home Assistant 依赖,可独立测试)。

报文格式(实测验证):
    POST https://swp.mzczgs.com/Hall/
    Content-Type: application/x-www-form-urlencoded
    Cookie: Ares.Core.Session.JXSWPHALL=...
    body: {"PostType":"1","Querys":[{QueryType,QueryReturnType,KeyName,FunctionName,Params,PageArgs}]}

单个参数: {"ParamType":"0|2|...","ParamName":"Param","ParamValue":...}
    ParamType: 0=Single(值作为字符串) 2=PageArg(值为分页JSON对象)  3=List 9=Custom
响应: {"ReturnData":{"ReturnType":"1|2|0","ReturnString":"...","<KeyName>":{...}}}
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 查询分组 KeyName(响应按同样 Key 返回)
QRY_SWITCH = "Switch"
QRY_CUSTOMER = "Customer"
QRY_NEEDPAY = "NeedPay"
QRY_BILLS = "Bills"          # 待缴账单
QRY_BASE = "Base"
QRY_RELATIONS = "Relations"
QRY_HIST0 = "HistY0"          # 历史账单:当年
QRY_HIST1 = "HistY1"          # 历史账单:上一年
QRY_ARTICLE = "Article"       # 水价公示文章

RT_SUCCESS = "1"
RT_SESSION_OUT = "2"
RT_ERROR = "0"


def page_args(page_index: int = 1, page_size: int = 100) -> dict:
    return {
        "PageIndex": str(page_index),
        "TotalRecord": "0",
        "TotalPage": "0",
        "RecordCountPerPage": str(page_size),
        "RecordCount": "0",
    }


def single_param(value: Any) -> dict:
    return {"ParamType": "0", "ParamName": "Param", "ParamValue": str(value)}


def page_param(page: dict | None = None) -> dict:
    return {"ParamType": "2", "ParamName": "Param", "ParamValue": page or page_args()}


def build_query(key: str, fun: str, params: list | None = None) -> dict:
    """按 H5 序列化规则构造单个 Query(无额外参数时 Params 为空数组)。"""
    return {
        "QueryType": "2",
        "QueryReturnType": "0",
        "KeyName": key,
        "FunctionName": fun,
        "Params": params or [],
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
class Bill:
    """一张(历史)水费账单。"""

    bill_id: str = ""
    bill_no: str = ""
    period: str = ""              # 账期 YYYY-MM
    usage: float = 0.0            # 本期用水量 m³(AMOUNT)
    money: float = 0.0            # 总金额
    water_money: float = 0.0      # 自来水费
    sewage_charges: float = 0.0   # 污水处理费
    waste_disposal_fee: float = 0.0  # 垃圾处理费
    penalty: float = 0.0          # 滞纳金
    last_record: str = ""         # 上次表码
    current_record: str = ""      # 本次表码
    pay_status: str = ""
    finish_time: str = ""

    def as_attr(self) -> dict:
        return {
            "账期": self.period,
            "用水量(m³)": self.usage,
            "总金额": self.money,
            "水费": self.water_money,
            "污水费": self.sewage_charges,
            "垃圾费": self.waste_disposal_fee,
            "滞纳金": self.penalty,
            "表码": f"{self.last_record}→{self.current_record}",
            "缴费时间": self.finish_time,
        }


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
    # --- 账单统计(自然月/季/年 + 最近一期) ---
    bills: list[Bill] = field(default_factory=list)
    usage_month: float = 0.0
    cost_month: float = 0.0
    usage_quarter: float = 0.0
    cost_quarter: float = 0.0
    usage_year: float = 0.0
    cost_year: float = 0.0
    last_bill: Bill | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def check_return_data(rd: dict) -> None:
    """校验外层 ReturnData;会话失效/错误时抛异常。"""
    ret_type = rd.get("ReturnType")
    if ret_type == RT_SESSION_OUT:
        raise AuthExpired(rd.get("ReturnString") or "会话已失效,请重新抓取 Cookie")
    if ret_type != RT_SUCCESS:
        raise MengziWaterError(rd.get("ReturnString") or f"接口返回异常(ReturnType={ret_type})")


def _f(v: Any) -> float:
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


def parse_bill(row: dict) -> Bill:
    """把历史账单行映射为 Bill(字段名已实测)。"""
    return Bill(
        bill_id=str(row.get("BILL_ID") or ""),
        bill_no=str(row.get("BILL_NO") or ""),
        period=str(row.get("PLAN_FLAG") or "").strip(),
        usage=_f(row.get("AMOUNT")),
        money=_f(row.get("MONEY")),
        water_money=_f(row.get("WATER_MONEY")),
        sewage_charges=_f(row.get("SEWAGE_CHARGES")),
        waste_disposal_fee=_f(row.get("WASTE_DISPOSAL_FEE")),
        penalty=_f(row.get("PENALTY")),
        last_record=str(row.get("LAST_RECORD") or ""),
        current_record=str(row.get("CURRENT_RECORD") or ""),
        pay_status=str(row.get("PAY_STATUS") or ""),
        finish_time=str(row.get("FINISH_TIME") or ""),
    )


def collect_bills(rd: dict) -> list[Bill]:
    """从一次多查询响应里收集两个年份的历史账单。"""
    bills: list[Bill] = []
    for key in (QRY_HIST0, QRY_HIST1):
        node = rd.get(key) or {}
        rows = node.get("Rows") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bills.append(parse_bill(row))
    # 去重(同年查询与空年查询可能重叠)并按账期倒序
    seen: set[str] = set()
    uniq: list[Bill] = []
    for b in bills:
        if b.bill_id and b.bill_id in seen:
            continue
        if b.bill_id:
            seen.add(b.bill_id)
        uniq.append(b)
    uniq.sort(key=lambda b: b.period, reverse=True)
    return uniq


def _period_ym(period: str) -> tuple[int, int] | None:
    m = re.match(r"^(\d{4})[-/]?(\d{1,2})$", str(period).strip())
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None


def compute_stats(h: Household, now: datetime | None = None) -> None:
    """按自然月/季/年聚合用水量(m³)与总费用,并取最近一期账单。"""
    now = now or datetime.now()
    cur_year, cur_month = now.year, now.month
    quarter = (cur_month - 1) // 3 + 1
    q_months = {(cur_year, m) for m in range((quarter - 1) * 3 + 1, quarter * 3 + 1)}
    h.usage_month = h.cost_month = 0.0
    h.usage_quarter = h.cost_quarter = 0.0
    h.usage_year = h.cost_year = 0.0
    for b in h.bills:
        ym = _period_ym(b.period)
        if ym is None:
            continue
        y, m = ym
        if (y, m) == (cur_year, cur_month):
            h.usage_month += b.usage
            h.cost_month += b.money
        if (y, m) in q_months:
            h.usage_quarter += b.usage
            h.cost_quarter += b.money
        if y == cur_year:
            h.usage_year += b.usage
            h.cost_year += b.money
    for key in ("usage_month", "cost_month", "usage_quarter", "cost_quarter", "usage_year", "cost_year"):
        setattr(h, key, round(getattr(h, key), 2))
    h.last_bill = h.bills[0] if h.bills else None


def apply_payload(h: Household, rd: dict) -> None:
    """把一次多查询响应聚合到 Household(客户信息/欠费/待缴/档案)。"""
    h.raw = rd

    cust = (rd.get(QRY_CUSTOMER) or {}).get("Data") or {}
    h.customer_no = str(cust.get("customerNo") or "")
    h.customer_name = str(cust.get("customerName") or "")
    h.address = str(cust.get("Address") or "")
    if not h.label and h.customer_name:
        h.label = h.customer_name

    np = (rd.get(QRY_NEEDPAY) or {}).get("Data") or {}
    if np.get("balance") is not None:
        h.balance = _f(np.get("balance"))
    elif cust.get("accountBalance") is not None:
        h.balance = _f(cust.get("accountBalance"))
    else:
        h.balance = _f(_base_fields(rd).get("ACCOUNT_BALANCE"))
    if np.get("arrearange") is not None:
        h.arrears = _f(np.get("arrearange"))
    elif cust.get("money") is not None:
        h.arrears = _f(cust.get("money"))

    bills_node = rd.get(QRY_BILLS) or {}
    rows = bills_node.get("Rows") or []
    h.pending_bills = len(rows)
    if h.pending_bills and not h.arrears:
        for row in rows:
            if not isinstance(row, dict):
                continue
            for k, v in row.items():
                if isinstance(v, (int, float)) and "MONEY" in str(k).upper():
                    h.arrears += _f(v)

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

    # 历史账单统计
    h.bills = collect_bills(rd)
    compute_stats(h)


# ---------------------------------------------------------------------------
# 文章(水价公示)HTML -> 可读行
# ---------------------------------------------------------------------------

def html_to_lines(html_text: str) -> list[str]:
    """把公众号文章 HTML 转成按行阅读的纯文本(表格按单元格切分)。"""
    if not html_text:
        return []
    text = html_text
    # 去掉脚本/样式
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    # 表格结构转分隔符
    text = re.sub(r"</tr>", "\n", text, flags=re.I)
    text = re.sub(r"</t[dh]>", "|", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    import html as _html

    text = _html.unescape(text)
    lines: list[str] = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\u00a0]+", " ", raw).strip(" |\u00a0 ")
        line = re.sub(r"(\|)+", "|", line).strip("|")
        if line:
            lines.append(line)
    return lines


def parse_article(rd: dict) -> dict:
    """解析 HallEx.GetArticleDetail 响应 → {title, lines, updated}。"""
    node = rd.get(QRY_ARTICLE) or {}
    fields = node.get("Fields") if isinstance(node.get("Fields"), dict) else {}
    if not fields:
        fields = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
    title = str(fields.get("ARTICLE_TITLE") or "水价标准")
    content = str(fields.get("ARTICLE_CONTENT") or "")
    return {"title": title, "lines": html_to_lines(content)}
