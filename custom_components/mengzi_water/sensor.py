"""蒙自城镇供水传感器(全量信息版)。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MengziWaterCoordinator
from .const import COMPANY_NAME, DOMAIN
from .protocol import tier_for_usage
from .water_api import Household

_LOGGER = logging.getLogger(__name__)

# key -> (device_class, unit)
META: dict[str, tuple[SensorDeviceClass | None, str | None]] = {
    # 基础
    "balance":           (SensorDeviceClass.MONETARY, "CNY"),
    "arrears":           (SensorDeviceClass.MONETARY, "CNY"),
    "pending_bills":     (None, "笔"),
    "last_payment_time": (SensorDeviceClass.TIMESTAMP, None),
    "stop_status":       (None, None),
    # 用量/总费用(按实际使用月归集)
    "usage_month":       (SensorDeviceClass.VOLUME, "m³"),
    "usage_quarter":     (SensorDeviceClass.VOLUME, "m³"),
    "usage_year":        (SensorDeviceClass.VOLUME, "m³"),
    "cost_month":        (SensorDeviceClass.MONETARY, "CNY"),
    "cost_quarter":      (SensorDeviceClass.MONETARY, "CNY"),
    "cost_year":         (SensorDeviceClass.MONETARY, "CNY"),
    # 费用分解(季度)
    "water_fee_quarter":   (SensorDeviceClass.MONETARY, "CNY"),
    "sewage_quarter":      (SensorDeviceClass.MONETARY, "CNY"),
    "waste_quarter":       (SensorDeviceClass.MONETARY, "CNY"),
    "penalty_quarter":     (SensorDeviceClass.MONETARY, "CNY"),
    # 费用分解(年度)
    "water_fee_year":    (SensorDeviceClass.MONETARY, "CNY"),
    "sewage_year":       (SensorDeviceClass.MONETARY, "CNY"),
    "waste_year":        (SensorDeviceClass.MONETARY, "CNY"),
    "penalty_year":      (SensorDeviceClass.MONETARY, "CNY"),
    # 最近一期账单
    "last_bill_usage":   (SensorDeviceClass.VOLUME, "m³"),
    "last_bill_cost":    (SensorDeviceClass.MONETARY, "CNY"),
    "last_bill_water":   (SensorDeviceClass.MONETARY, "CNY"),
    "last_bill_sewage":  (SensorDeviceClass.MONETARY, "CNY"),
    "last_bill_waste":   (SensorDeviceClass.MONETARY, "CNY"),
    "last_bill_penalty": (SensorDeviceClass.MONETARY, "CNY"),
    # 户号信息(普通可见实体)
    "customer_no":       (None, None),
    "customer_name":     (None, None),
    "address":           (None, None),
    "book_name":         (None, None),
    "water_use_type":    (None, None),
    "meter_count":       (None, "块"),
    "read_user_name":    (None, None),
    "last_update":       (SensorDeviceClass.TIMESTAMP, None),
}

NAME_ZH = {
    "balance": "预存金额", "arrears": "待缴欠费合计", "pending_bills": "待缴账单数",
    "last_payment_time": "最近缴费时间", "stop_status": "供水状态",
    "usage_month": "本月实际用水量", "usage_quarter": "本季实际用水量", "usage_year": "本年实际用水量",
    "cost_month": "本月实际水费", "cost_quarter": "本季实际水费", "cost_year": "本年实际水费",
    "water_fee_quarter": "本季自来水费", "sewage_quarter": "本季污水处理费",
    "waste_quarter": "本季垃圾处理费", "penalty_quarter": "本季违约金",
    "water_fee_year": "本年自来水费", "sewage_year": "本年污水处理费",
    "waste_year": "本年垃圾处理费", "penalty_year": "本年违约金",
    "last_bill_usage": "最近一期用水量", "last_bill_cost": "最近一期水费合计",
    "last_bill_water": "最近一期自来水费", "last_bill_sewage": "最近一期污水费",
    "last_bill_waste": "最近一期垃圾费", "last_bill_penalty": "最近一期违约金",
    "customer_no": "户号", "customer_name": "户名", "address": "用址",
    "book_name": "册名", "water_use_type": "用水类型", "meter_count": "水表数",
    "read_user_name": "抄表员", "last_update": "最近更新",
    "price_standard": "水价标准",
}

NAME_EN = {
    "balance": "Prepaid balance", "arrears": "Total arrears", "pending_bills": "Unpaid bills",
    "last_payment_time": "Last payment time", "stop_status": "Water supply status",
    "usage_month": "Usage this month", "usage_quarter": "Usage this quarter", "usage_year": "Usage this year",
    "cost_month": "Cost this month", "cost_quarter": "Cost this quarter", "cost_year": "Cost this year",
    "water_fee_quarter": "Water fee (quarter)", "sewage_quarter": "Sewage fee (quarter)",
    "waste_quarter": "Waste fee (quarter)", "penalty_quarter": "Penalty (quarter)",
    "water_fee_year": "Water fee (year)", "sewage_year": "Sewage fee (year)",
    "waste_year": "Waste fee (year)", "penalty_year": "Penalty (year)",
    "last_bill_usage": "Latest bill usage", "last_bill_cost": "Latest bill cost",
    "last_bill_water": "Latest bill water fee", "last_bill_sewage": "Latest bill sewage fee",
    "last_bill_waste": "Latest bill waste fee", "last_bill_penalty": "Latest bill penalty",
    "customer_no": "Customer number", "customer_name": "Customer name", "address": "Address",
    "book_name": "Book", "water_use_type": "Water use type", "meter_count": "Meters",
    "read_user_name": "Meter reader", "last_update": "Last update",
    "price_standard": "Water price standard",
}

ICONS = {
    "balance": "mdi:water", "arrears": "mdi:cash-remove", "pending_bills": "mdi:file-document-alert-outline",
    "last_payment_time": "mdi:clock-outline", "stop_status": "mdi:pipe",
    "usage_month": "mdi:water-outline", "usage_quarter": "mdi:chart-line", "usage_year": "mdi:chart-bar",
    "cost_month": "mdi:currency-cny", "cost_quarter": "mdi:chart-line", "cost_year": "mdi:chart-bar",
    "water_fee_quarter": "mdi:chart-line", "sewage_quarter": "mdi:chart-line",
    "waste_quarter": "mdi:chart-line", "penalty_quarter": "mdi:chart-line",
    "water_fee_year": "mdi:chart-bar", "sewage_year": "mdi:chart-bar",
    "waste_year": "mdi:chart-bar", "penalty_year": "mdi:chart-bar",
    "last_bill_usage": "mdi:counter", "last_bill_cost": "mdi:receipt-text-outline",
    "last_bill_water": "mdi:receipt-text-outline", "last_bill_sewage": "mdi:receipt-text-outline",
    "last_bill_waste": "mdi:recycle", "last_bill_penalty": "mdi:alert-circle-outline",
    "customer_no": "mdi:card-account-details-outline", "customer_name": "mdi:account-outline",
    "address": "mdi:map-marker-outline", "book_name": "mdi:book-outline",
    "water_use_type": "mdi:water-outline", "meter_count": "mdi:counter",
    "read_user_name": "mdi:account-search-outline", "last_update": "mdi:update",
    "price_standard": "mdi:scale-balance",
}

# 计费口径说明(附加到相应费用实体)
_FEE_NOTE = {
    "water_fee_quarter": "自来水费 = 用量 × 阶梯自来水单价(一档 3.30 元/m³)",
    "sewage_quarter": "污水处理费 = 用量 × 1.10 元/m³",
    "waste_quarter": "垃圾处理费为定额:10 元/户/月",
    "penalty_quarter": "违约金(滞纳金)按营业厅账单计收",
    "water_fee_year": "自来水费 = 用量 × 阶梯自来水单价(一档 3.30 元/m³)",
    "sewage_year": "污水处理费 = 用量 × 1.10 元/m³",
    "waste_year": "垃圾处理费为定额:10 元/户/月",
    "penalty_year": "违约金(滞纳金)按营业厅账单计收",
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(str(value).strip(), fmt)
            break
        except ValueError:
            continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _hget(h: Household, key: str) -> Any:
    if key == "balance":
        return h.balance
    if key == "arrears":
        return h.arrears
    if key == "pending_bills":
        return h.pending_bills
    if key == "last_payment_time":
        return _parse_dt(h.last_payment_time)
    if key == "stop_status":
        return h.stop_status
    if key == "usage_month":
        return h.usage_month
    if key == "usage_quarter":
        return h.usage_quarter
    if key == "usage_year":
        return h.usage_year
    if key == "cost_month":
        return h.cost_month
    if key == "cost_quarter":
        return h.cost_quarter
    if key == "cost_year":
        return h.cost_year
    if key == "water_fee_quarter":
        return h.q_water
    if key == "sewage_quarter":
        return h.q_sewage
    if key == "waste_quarter":
        return h.q_waste
    if key == "penalty_quarter":
        return h.q_penalty
    if key == "water_fee_year":
        return h.y_water
    if key == "sewage_year":
        return h.y_sewage
    if key == "waste_year":
        return h.y_waste
    if key == "penalty_year":
        return h.y_penalty
    if key == "last_bill_usage":
        return h.last_bill.usage if h.last_bill else None
    if key == "last_bill_cost":
        return h.last_bill.money if h.last_bill else None
    if key == "last_bill_water":
        return h.last_bill.water_money if h.last_bill else None
    if key == "last_bill_sewage":
        return h.last_bill.sewage_charges if h.last_bill else None
    if key == "last_bill_waste":
        return h.last_bill.waste_disposal_fee if h.last_bill else None
    if key == "last_bill_penalty":
        return h.last_bill.penalty if h.last_bill else None
    if key == "customer_no":
        return h.customer_no
    if key == "customer_name":
        return h.customer_name
    if key == "address":
        return h.address
    if key == "book_name":
        return h.book_name
    if key == "water_use_type":
        return h.water_use_type
    if key == "meter_count":
        return h.meter_count
    if key == "read_user_name":
        return h.read_user_name
    if key == "last_update":
        return datetime.now().astimezone()
    return None


class MengziWaterSensor(CoordinatorEntity[MengziWaterCoordinator], SensorEntity):
    """一个户号下的一个只读传感器。"""

    _attr_should_poll = False

    def __init__(self, coordinator: MengziWaterCoordinator, key: str, account_key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._account_key = account_key
        safe_no = "".join(ch for ch in str(account_key) if ch.isalnum()) or "acc"
        self._attr_unique_id = f"{DOMAIN}_{account_key}_{key}"
        self.entity_id = f"sensor.mengzi_water_{safe_no}_{key}"
        self._attr_name = _localized_name(coordinator, key)

    @property
    def _household(self) -> Household | None:
        return self.coordinator.data.get(self._account_key)

    @property
    def device_info(self) -> dict[str, Any] | None:
        h = self._household
        if h is None:
            return None
        return {
            "identifiers": {(DOMAIN, str(h.customer_id))},
            "name": f"供水户号 {h.customer_no or self._account_key} {h.customer_name}".strip(),
            "manufacturer": COMPANY_NAME,
            "model": "网上营业厅",
        }

    @property
    def device_class(self) -> SensorDeviceClass | None:
        return META[self._key][0]

    @property
    def native_unit_of_measurement(self) -> str | None:
        return META[self._key][1]

    @property
    def icon(self) -> str:
        return ICONS.get(self._key, "mdi:water")

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> Any:
        h = self._household
        if h is None:
            return None
        return _hget(h, self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        h = self._household
        if h is None:
            return {}
        attrs: dict[str, Any] = {}
        k = self._key
        attrs["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if k in _FEE_NOTE:
            attrs["计费口径"] = _FEE_NOTE[k]
        if k in ("usage_month", "cost_month"):
            attrs["统计口径"] = "实际使用月 = 抄表月-1(月头抄表,本期账单实为上期用量)"
        if k.startswith(("usage_", "cost_", "water_fee_", "sewage_", "waste_", "penalty_", "last_bill_")):
            self._attach_bill_info(attrs, h)
        if k == "usage_month" and h.bills:
            attrs["近12期账单"] = [b.as_attr(self._tier_text(b)) for b in h.bills[:12]]
        return attrs

    def _tier_text(self, bill) -> str:
        tiers = (self.coordinator.price_standard or {}).get("tiers") or []
        stage = tier_for_usage(tiers, bill.usage)
        total = next((t.get("到户") for t in tiers
                      if t.get("阶梯") == stage and t.get("类别") == "居民生活用水"), None)
        if stage != "-" and total is not None:
            return f"{stage} 到户 {total} 元/m³"
        return stage

    def _attach_bill_info(self, attrs: dict[str, Any], h: Household) -> None:
        b = h.last_bill
        if b is None:
            return
        actual = f"{b.actual_ym[0]}-{b.actual_ym[1]:02d}" if b.actual_ym else "-"
        attrs["最近账单期(抄表月)"] = b.period
        attrs["最近账单实际使用月"] = actual
        attrs["抄表日"] = b.read_date or "-"
        attrs["上次抄表日"] = b.last_read_date or "-"
        if b.last_record and b.current_record:
            attrs["表码"] = f"{b.last_record} → {b.current_record}"
        attrs["折合自来水单价"] = b.unit_water_price
        attrs["折合到户均价(含定额费)"] = b.avg_total_price
        attrs["适用阶梯"] = self._tier_text(b)
        attrs["最近账单费用分解"] = {
            "自来水费": b.water_money,
            "污水处理费": b.sewage_charges,
            "垃圾处理费(定额)": b.waste_disposal_fee,
            "违约金": b.penalty,
            "合计": b.money,
        }

    @property
    def suggested_display_precision(self) -> int | None:
        if META[self._key][1] == "CNY":
            return 2
        if META[self._key][1] == "m³":
            return 1
        return None


class MengziWaterPriceSensor(CoordinatorEntity[MengziWaterCoordinator], SensorEntity):
    """水价标准(公示文章)。"""

    _attr_should_poll = False

    def __init__(self, coordinator: MengziWaterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_price_standard"
        self.entity_id = f"sensor.{DOMAIN}_price_standard"
        self._attr_name = _localized_name(coordinator, "price_standard")
        self._attr_icon = ICONS["price_standard"]

    @property
    def native_value(self) -> str | None:
        info = self.coordinator.price_standard
        return info.get("title") if info else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self.coordinator.price_standard or {}
        attrs: dict[str, Any] = {}
        tiers = info.get("tiers") or []
        if tiers:
            attrs["价目表"] = [
                {
                    "类别": t.get("类别"),
                    "阶梯": t.get("阶梯"),
                    "范围": t.get("月用量范围"),
                    "自来水(元/m³)": t.get("自来水"),
                    "污水处理(元/m³)": t.get("污水处理"),
                    "到户价(元/m³)": t.get("到户"),
                }
                for t in tiers
            ]
        attrs["垃圾处理费"] = "10 元/户/月(定额,账单实测)"
        lines = info.get("lines") or []
        if lines:
            attrs["公示原文"] = lines
        attrs["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return attrs


def _localized_name(coordinator: MengziWaterCoordinator, key: str) -> str:
    lang = ""
    try:
        lang = str(coordinator.hass.config.language or "")
    except Exception:  # noqa: BLE001
        lang = ""
    names = NAME_ZH if lang.lower().startswith("zh") else NAME_EN
    return names.get(key, key)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MengziWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    accounts = list(coordinator.data.keys())
    entities: list[SensorEntity] = [
        MengziWaterSensor(coordinator, key, acc)
        for acc in accounts
        for key in META
    ]
    if accounts:
        entities.append(MengziWaterPriceSensor(coordinator))
    if not entities:
        _LOGGER.warning(
            "mengzi_water: 没有获取到可用户号数据,请检查会话 Cookie 是否有效、是否已绑定户号"
        )
    async_add_entities(entities)
