"""蒙自城镇供水传感器。"""
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
from .water_api import Household

_LOGGER = logging.getLogger(__name__)

# 每户号实体: key -> (device_class, unit)
META: dict[str, tuple[SensorDeviceClass | None, str | None]] = {
    "balance":           (SensorDeviceClass.MONETARY, "CNY"),
    "arrears":           (SensorDeviceClass.MONETARY, "CNY"),
    "pending_bills":     (None, "笔"),
    "last_payment_time": (SensorDeviceClass.TIMESTAMP, None),
    "stop_status":       (None, None),
    # 账单统计
    "usage_month":       (SensorDeviceClass.VOLUME, "m³"),
    "usage_quarter":     (SensorDeviceClass.VOLUME, "m³"),
    "usage_year":        (SensorDeviceClass.VOLUME, "m³"),
    "cost_month":        (SensorDeviceClass.MONETARY, "CNY"),
    "cost_quarter":      (SensorDeviceClass.MONETARY, "CNY"),
    "cost_year":         (SensorDeviceClass.MONETARY, "CNY"),
    "last_bill_usage":   (SensorDeviceClass.VOLUME, "m³"),
    "last_bill_cost":    (SensorDeviceClass.MONETARY, "CNY"),
}

NAME_ZH = {
    "balance": "预存金额",
    "arrears": "待缴欠费合计",
    "pending_bills": "待缴账单数",
    "last_payment_time": "最近缴费时间",
    "stop_status": "供水状态",
    "usage_month": "本月用水量",
    "usage_quarter": "本季用水量",
    "usage_year": "本年用水量",
    "cost_month": "本月水费",
    "cost_quarter": "本季水费",
    "cost_year": "本年水费",
    "last_bill_usage": "最近一期用水量",
    "last_bill_cost": "最近一期水费",
    "price_standard": "水价标准",
}

NAME_EN = {
    "balance": "Prepaid balance",
    "arrears": "Total arrears",
    "pending_bills": "Unpaid bills",
    "last_payment_time": "Last payment time",
    "stop_status": "Water supply status",
    "usage_month": "Usage this month",
    "usage_quarter": "Usage this quarter",
    "usage_year": "Usage this year",
    "cost_month": "Cost this month",
    "cost_quarter": "Cost this quarter",
    "cost_year": "Cost this year",
    "last_bill_usage": "Latest bill usage",
    "last_bill_cost": "Latest bill cost",
    "price_standard": "Water price standard",
}

ICONS = {
    "balance": "mdi:water",
    "arrears": "mdi:cash-remove",
    "pending_bills": "mdi:file-document-alert-outline",
    "last_payment_time": "mdi:clock-outline",
    "stop_status": "mdi:pipe",
    "usage_month": "mdi:water-outline",
    "usage_quarter": "mdi:chart-line",
    "usage_year": "mdi:chart-bar",
    "cost_month": "mdi:currency-cny",
    "cost_quarter": "mdi:chart-line",
    "cost_year": "mdi:chart-bar",
    "last_bill_usage": "mdi:counter",
    "last_bill_cost": "mdi:receipt-text-outline",
    "price_standard": "mdi:scale-balance",
}


def _parse_dt(value: str | None) -> datetime | None:
    """解析服务端时间;无时区时按本机时区处理。"""
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

    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    @property
    def native_value(self) -> Any:
        h = self._household
        if h is None:
            return None
        if self._key == "balance":
            return h.balance
        if self._key == "arrears":
            return h.arrears
        if self._key == "pending_bills":
            return h.pending_bills
        if self._key == "last_payment_time":
            return _parse_dt(h.last_payment_time)
        if self._key == "stop_status":
            return h.stop_status
        if self._key == "usage_month":
            return h.usage_month
        if self._key == "usage_quarter":
            return h.usage_quarter
        if self._key == "usage_year":
            return h.usage_year
        if self._key == "cost_month":
            return h.cost_month
        if self._key == "cost_quarter":
            return h.cost_quarter
        if self._key == "cost_year":
            return h.cost_year
        if self._key == "last_bill_usage":
            return h.last_bill.usage if h.last_bill else None
        if self._key == "last_bill_cost":
            return h.last_bill.money if h.last_bill else None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        h = self._household
        if h is None:
            return {}
        attrs: dict[str, Any] = {}
        if h.customer_no:
            attrs["户号"] = h.customer_no
        if h.customer_name:
            attrs["户名"] = h.customer_name
        if h.address:
            attrs["用址"] = h.address
        if h.book_name:
            attrs["册名"] = h.book_name
        if h.water_use_type:
            attrs["用水类型"] = h.water_use_type
        if h.meter_count:
            attrs["水表数"] = h.meter_count
        attrs["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if self._key.startswith(("usage_", "cost_", "last_bill_")) and h.last_bill:
            b = h.last_bill
            attrs["最近账单期"] = b.period
            attrs["最近账单总金额"] = b.money
            attrs["最近账单分解"] = (
                f"水费 {b.water_money} / 污水 {b.sewage_charges} / "
                f"垃圾 {b.waste_disposal_fee} / 滞纳金 {b.penalty}"
            )
            if b.last_record and b.current_record:
                attrs["表码"] = f"{b.last_record} → {b.current_record}"
            if b.finish_time:
                attrs["最近缴费"] = b.finish_time
        if self._key.startswith("usage_") and h.bills:
            recent = []
            for bill in h.bills[:12]:
                recent.append(bill.as_attr())
            attrs["近12期账单"] = recent
        return attrs

    @property
    def suggested_display_precision(self) -> int | None:
        if self._key in (
            "balance", "arrears", "cost_month", "cost_quarter", "cost_year",
            "last_bill_cost",
        ):
            return 2
        if self._key in ("usage_month", "usage_quarter", "usage_year", "last_bill_usage"):
            return 1
        return None


class MengziWaterPriceSensor(CoordinatorEntity[MengziWaterCoordinator], SensorEntity):
    """水价标准(公示文章),不隶属于某个户号设备。"""

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
        lines = info.get("lines") or []
        attrs: dict[str, Any] = {}
        if lines:
            attrs["公示内容"] = lines
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
