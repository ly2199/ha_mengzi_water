"""纯协议层(无 Home Assistant 依赖,可独立测试)。

报文格式(实测验证):
    POST https://swp.mzczgs.com/Hall/
    Content-Type: application/x-www-form-urlencoded
    Cookie: Ares.Core.Session.JXSWPHALL=...
    body: {"PostType":"1","Querys":[{QueryType,QueryReturnType,KeyName,FunctionName,Params,PageArgs}]}

单个参数: {"ParamType":"0|2|...","ParamName":"Param","ParamValue":...}
    ParamType: 0=Single(值作为字符串) 2=PageArg(值为分页JSON对象)
响应: {"ReturnData":{"ReturnType":"1|2|0","ReturnString":"...","<KeyName>":{...}}}

抄表/账单规则(实测):每月 1~4 日月头抄表,账单期=抄表月,但用量窗口
实际覆盖上一自然月(如账期 2026-09、9/1 抄表、表码 96→101 对应 8 月用量)。
因此统计一律按「实际使用月 = 抄表月-1(抄表日在 15 日及以前)」归集。
费用结构(实测):水费=用量×自来水单价(一档 3.30);污水费=用量×1.10;
垃圾处理费=10 元/户/月(定额);滞纳金(违约金)按账单给出。
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


# ---------------------------------------------------------------------------
# 报文构造
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

def _f(v: Any) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    m = month + delta
    while m < 1:
        m += 12
        year -= 1
    while m > 12:
        m -= 12
        year += 1
    return year, m


def bill_actual_ym(period: str, read_date: str) -> tuple[int, int] | None:
    """账单账期 + 抄表日 → 实际使用月(抄表日≤15 → 抄表月-1)。"""
    m = re.match(r"^(\d{4})[-/]?(\d{1,2})", str(period).strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if read_date:
        try:
            rd = datetime.strptime(str(read_date)[:10], "%Y-%m-%d")
        except ValueError:
            rd = None
        if rd:
            return _shift_month(rd.year, rd.month, -1 if rd.day <= 15 else 0)
    return (y, mo)


@dataclass
class Bill:
    """一张(历史)水费账单。"""

    bill_id: str = ""
    bill_no: str = ""
    period: str = ""                 # 账期 YYYY-MM(抄表月)
    read_date: str = ""              # 本次抄表日
    last_read_date: str = ""         # 上次抄表日
    actual_ym: tuple[int, int] | None = None   # 实际使用月
    usage: float = 0.0               # 本期用量 m³(AMOUNT)
    money: float = 0.0               # 总金额
    water_money: float = 0.0         # 自来水费
    sewage_charges: float = 0.0      # 污水处理费
    waste_disposal_fee: float = 0.0  # 垃圾处理费(定额)
    penalty: float = 0.0             # 违约金(滞纳金)
    last_record: str = ""            # 上次表码
    current_record: str = ""         # 本次表码
    pay_status: str = ""
    finish_time: str = ""

    @property
    def unit_water_price(self) -> float:
        """本期自来水折合单价(元/m³),用于核对阶梯。"""
        return round(self.water_money / self.usage, 2) if self.usage else 0.0

    @property
    def avg_total_price(self) -> float:
        """总费用(含定额垃圾费)折合每吨价格。"""
        return round(self.money / self.usage, 2) if self.usage else 0.0

    def as_attr(self, tier_text: str = "") -> dict:
        out: dict[str, Any] = {
            "账期": self.period,
            "实际使用月": f"{self.actual_ym[0]}-{self.actual_ym[1]:02d}" if self.actual_ym else self.period,
            "抄表日": self.read_date or "",
            "上次抄表日": self.last_read_date or "",
            "用水量(m³)": self.usage,
            "总金额": self.money,
            "自来水费": self.water_money,
            "污水处理费": self.sewage_charges,
            "垃圾处理费": self.waste_disposal_fee,
            "违约金": self.penalty,
            "折合自来水单价": self.unit_water_price,
            "折合到户均价(含定额费)": self.avg_total_price,
            "表码": f"{self.last_record} → {self.current_record}",
        }
        if tier_text:
            out["适用阶梯"] = tier_text
        if self.finish_time:
            out["缴费时间"] = self.finish_time
        return out


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
    read_user_name: str = ""         # 抄表员
    # --- 账单(按实际使用月归集) ---
    bills: list[Bill] = field(default_factory=list)
    usage_month: float = 0.0
    cost_month: float = 0.0
    usage_quarter: float = 0.0
    cost_quarter: float = 0.0
    usage_year: float = 0.0
    cost_year: float = 0.0
    # 费用分解: 自来水/污水/垃圾/违约金 (元), 按 本季/本年
    q_water: float = 0.0
    q_sewage: float = 0.0
    q_waste: float = 0.0
    q_penalty: float = 0.0
    y_water: float = 0.0
    y_sewage: float = 0.0
    y_waste: float = 0.0
    y_penalty: float = 0.0
    last_bill: Bill | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def check_return_data(rd: dict) -> None:
    ret_type = rd.get("ReturnType")
    if ret_type == RT_SESSION_OUT:
        raise AuthExpired(rd.get("ReturnString") or "会话已失效,请重新抓取 Cookie")
    if ret_type != RT_SUCCESS:
        raise MengziWaterError(rd.get("ReturnString") or f"接口返回异常(ReturnType={ret_type})")


def _base_fields(rd: dict) -> dict:
    base = rd.get(QRY_BASE) or {}
    fields = base.get("Fields") if isinstance(base.get("Fields"), dict) else {}
    if fields:
        return fields
    return {k: v for k, v in base.items() if not isinstance(v, (dict, list))}


def extract_relations(rd: dict) -> list[Household]:
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
    b = Bill(
        bill_id=str(row.get("BILL_ID") or ""),
        bill_no=str(row.get("BILL_NO") or ""),
        period=str(row.get("PLAN_FLAG") or "").strip(),
        read_date=str(row.get("READ_DATE") or "")[:10],
        last_read_date=str(row.get("LAST_READ_TIME") or "")[:10],
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
    b.actual_ym = bill_actual_ym(b.period, b.read_date)
    return b


def collect_bills(rd: dict) -> list[Bill]:
    bills: list[Bill] = []
    for key in (QRY_HIST0, QRY_HIST1):
        node = rd.get(key) or {}
        rows = node.get("Rows") or []
        for row in rows:
            if isinstance(row, dict):
                bills.append(parse_bill(row))
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


def _in_periods(ym: tuple[int, int], year: int, months: set[int] | None = None) -> bool:
    if months is not None:
        return ym[0] == year and ym[1] in months
    return ym[0] == year


def compute_stats(h: Household, now: datetime | None = None) -> None:
    """按实际使用月归集:自然月/季/年 用量与费用 + 费用分解。"""
    now = now or datetime.now()
    cur_year, cur_month = now.year, now.month
    quarter = (cur_month - 1) // 3 + 1
    q_months = set(range((quarter - 1) * 3 + 1, quarter * 3 + 1))

    acc = {
        "usage_month": 0.0, "cost_month": 0.0,
        "usage_quarter": 0.0, "cost_quarter": 0.0,
        "usage_year": 0.0, "cost_year": 0.0,
        "q_water": 0.0, "q_sewage": 0.0, "q_waste": 0.0, "q_penalty": 0.0,
        "y_water": 0.0, "y_sewage": 0.0, "y_waste": 0.0, "y_penalty": 0.0,
    }
    for b in h.bills:
        ym = b.actual_ym or bill_actual_ym(b.period, b.read_date)
        if ym is None:
            continue
        y, m = ym
        if (y, m) == (cur_year, cur_month):
            acc["usage_month"] += b.usage
            acc["cost_month"] += b.money
        if y == cur_year and m in q_months:
            acc["usage_quarter"] += b.usage
            acc["cost_quarter"] += b.money
            acc["q_water"] += b.water_money
            acc["q_sewage"] += b.sewage_charges
            acc["q_waste"] += b.waste_disposal_fee
            acc["q_penalty"] += b.penalty
        if y == cur_year:
            acc["usage_year"] += b.usage
            acc["cost_year"] += b.money
            acc["y_water"] += b.water_money
            acc["y_sewage"] += b.sewage_charges
            acc["y_waste"] += b.waste_disposal_fee
            acc["y_penalty"] += b.penalty
    for k, v in acc.items():
        setattr(h, k, round(v, 2))
    h.last_bill = h.bills[0] if h.bills else None


def apply_payload(h: Household, rd: dict) -> None:
    """把一次多查询响应聚合到 Household。"""
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
    h.book_name = str(fields.get("BOOK_NAME") or "")
    h.water_use_type = str(fields.get("WATER_USE_TYPE") or "")
    h.read_user_name = str(fields.get("READ_USER_NAME") or "")
    try:
        h.meter_count = int(fields.get("WATER_METERS") or 0)
    except (TypeError, ValueError):
        h.meter_count = 0
    stop_text = str(cust.get("stopStatus") or "")
    if not stop_text or stop_text in ("", "None", "null"):
        try:
            stop_text = "停水" if int(fields.get("IS_STOP") or 0) else "正常"
        except (TypeError, ValueError):
            stop_text = "正常"
    h.stop_status = stop_text

    h.bills = collect_bills(rd)
    compute_stats(h)


# ---------------------------------------------------------------------------
# Cookie 辅助
# ---------------------------------------------------------------------------

def extract_session_cookie(set_cookies: list[str], prefix: str = "Ares.Core.Session.JXSWPHALL=") -> str | None:
    """从 Set-Cookie 列表里取出会话 Cookie 的 名=值(原样保留 URL 编码)。"""
    for raw in set_cookies:
        first = str(raw).split(";", 1)[0].strip()
        if first.startswith(prefix):
            return first
    return None


# ---------------------------------------------------------------------------
# 阶梯价目(水价公示解析)
# ---------------------------------------------------------------------------

# 兜底价目(与公示一致;解析失败时使用)
FALLBACK_TIERS: list[dict] = [
    {"类别": "居民生活用水", "阶梯": "第一阶梯", "月用量范围": "15立方米以内",
     "自来水": 3.30, "污水处理": 1.10, "到户": 4.40},
    {"类别": "居民生活用水", "阶梯": "第二阶梯", "月用量范围": "16-25立方米",
     "自来水": 4.90, "污水处理": 1.10, "到户": 6.00},
    {"类别": "居民生活用水", "阶梯": "第三阶梯", "月用量范围": "26立方米以上",
     "自来水": 6.40, "污水处理": 1.10, "到户": 7.50},
    {"类别": "非居民生活用水", "阶梯": "-", "月用量范围": "-",
     "自来水": 4.40, "污水处理": 1.50, "到户": 5.90},
    {"类别": "特种行业用水", "阶梯": "-", "月用量范围": "-",
     "自来水": 8.00, "污水处理": 2.00, "到户": 10.00},
]

_GARBAGE_PER_MONTH = 10.0  # 垃圾处理费:10元/户/月(账单实测,定额)


def html_to_lines(html_text: str) -> list[str]:
    if not html_text:
        return []
    text = html_text
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
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


def _row_cells(tr: str) -> list[str]:
    import html as _html

    cells = []
    for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I):
        txt = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", td))).strip()
        cells.append(txt)
    return cells


def parse_article(rd: dict) -> dict:
    """解析文章响应 → {title, lines, tiers}。"""
    node = rd.get(QRY_ARTICLE) or {}
    fields = node.get("Fields") if isinstance(node.get("Fields"), dict) else {}
    if not fields:
        fields = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
    title = str(fields.get("ARTICLE_TITLE") or "水价公示")
    content = str(fields.get("ARTICLE_CONTENT") or "")
    lines = html_to_lines(content)
    tiers = parse_price_tiers(content, lines)
    return {"title": title, "lines": lines, "tiers": tiers}


def parse_price_tiers(html_text: str, lines: list[str] | None = None) -> list[dict]:
    """从公示 HTML 中解析阶梯价目(失败回退内置价目)。"""
    tiers: list[dict] = []
    if html_text:
        tables = re.findall(r"<table[^>]*>(.*?)</table>", html_text, flags=re.S | re.I)
        rows: list[list[str]] = []
        for tb in tables:
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, flags=re.S | re.I):
                cells = _row_cells(tr)
                if cells:
                    rows.append(cells)
        # 阶梯行: [自来水单价, "第X阶梯 范围", 到户价]
        ladder_rows = []
        for cells in rows:
            joined = "".join(cells)
            if "阶梯" not in joined:
                continue
            ladder_rows.append(cells)
        if len(ladder_rows) >= 3:
            names = ["第一阶梯", "第二阶梯", "第三阶梯", "第四阶梯"]
            for i, cells in enumerate(ladder_rows[:4]):
                vals = [c for c in cells if _is_num(c)]
                if len(vals) < 2:
                    continue
                # 范围文字取中间单元格(去掉阶梯名前缀),必要时从拼接文本中提取
                rng = ""
                for c in cells[1:-1]:
                    rng += c
                if not rng:
                    rng = "".join(cells)
                rng = re.sub(r"(第一|第二|第三|第四|第五)阶梯", "", rng)
                tiers.append({
                    "类别": "居民生活用水",
                    "阶梯": names[i] if i < len(names) else f"阶梯{i + 1}",
                    "月用量范围": re.sub(r"\s+", " ", rng).strip(),
                    "自来水": float(vals[0]), "污水处理": 1.10, "到户": float(vals[-1]),
                })
    if len(tiers) < 3:
        tiers = [dict(t) for t in FALLBACK_TIERS]
    return tiers


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def tier_for_usage(tiers: list[dict], usage_m3: float) -> str:
    """按最近一期账单用量判断适用阶梯(居民按月用量)。"""
    for t in tiers:
        if t.get("类别") != "居民生活用水":
            continue
        rng = str(t.get("月用量范围") or "")
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", rng)]
        if "以内" in rng or "以下" in rng or ("立方米" in rng and len(nums) == 1):
            # 第一档:0 ~ 上限
            hi = nums[0] if nums else None
            if hi is not None and usage_m3 <= hi:
                return str(t.get("阶梯") or "-")
        elif "-" in rng or "~" in rng or "～" in rng or "至" in rng:
            if len(nums) >= 2 and nums[0] <= usage_m3 <= nums[1]:
                return str(t.get("阶梯") or "-")
        elif "以上" in rng:
            lo = nums[0] if nums else None
            if lo is not None and usage_m3 >= lo:
                return str(t.get("阶梯") or "-")
        elif len(nums) == 1 and not ("以内" in rng or "以上" in rng):
            # 单值范围描述兜底
            hi = nums[0]
            if usage_m3 <= hi:
                return str(t.get("阶梯") or "-")
    return "-"
