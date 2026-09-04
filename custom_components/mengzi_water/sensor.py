"""蒙自城镇供水传感器。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MengziWaterCoordinator
from .const import COMPANY_NAME, DOMAIN
from .water_api import Household

_LOGGER = logging.getLogger(__name__)

# key: (device_class, unit) —— 展示名由 translations/entity.sensor.<key>.name 提供
META: dict[str, tuple[SensorDeviceClass | None, str | None]] = {
    "balance":           (SensorDeviceClass.MONETARY, "CNY"),
    "arrears":           (SensorDeviceClass.MONETARY, "CNY"),
    "pending_bills":     (None, "笔"),
    "last_payment_time": (SensorDeviceClass.TIMESTAMP, None),
    "stop_status":       (None, None),
    "customer_no":       (None, None),
    "customer_name":     (None, None),
    "address":           (None, None),
    "meter_count":       (None, "块"),
    "water_use_type":    (None, None),
    "book_name":         (None, None),
    "last_update":       (SensorDeviceClass.TIMESTAMP, None),
}

_DIAGNOSTIC_KEYS = {
    "customer_no", "customer_name", "address", "meter_count",
    "water_use_type", "book_name", "last_update",
}

ICONS = {
    "balance": "mdi:water",
    "arrears": "mdi:cash-remove",
    "pending_bills": "mdi:file-document-alert-outline",
    "last_payment_time": "mdi:clock-outline",
    "stop_status": "mdi:pipe",
    "customer_no": "mdi:card-account-details-outline",
    "customer_name": "mdi:account-outline",
    "address": "mdi:map-marker-outline",
    "meter_count": "mdi:counter",
    "water_use_type": "mdi:water-outline",
    "book_name": "mdi:book-outline",
    "last_update": "mdi:update",
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


def _stop_flag(h: Household) -> bool:
    try:
        return int(h.raw.get("Customer", {}).get("Data", {}).get("isStop", 0)) == 1
    except (TypeError, ValueError, AttributeError):
        return False


class MengziWaterSensor(CoordinatorEntity[MengziWaterCoordinator], SensorEntity):
    """一个户号下的一个只读传感器。"""

    _attr_should_poll = False

    def __init__(self, coordinator: MengziWaterCoordinator, key: str, account_key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._account_key = account_key
        safe_no = "".join(ch for ch in str(account_key) if ch.isalnum()) or "acc"
        self._attr_unique_id = f"{DOMAIN}_{account_key}_{key}"
        # 确定性英文 entity_id,避免中文/重名导致重复后缀
        self.entity_id = f"sensor.mengzi_water_{safe_no}_{key}"
        self._attr_translation_key = key
        if key in _DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

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
            return h.stop_status or ("停水" if _stop_flag(h) else "正常")
        if self._key == "customer_no":
            return h.customer_no
        if self._key == "customer_name":
            return h.customer_name
        if self._key == "address":
            return h.address
        if self._key == "meter_count":
            return h.meter_count
        if self._key == "water_use_type":
            return h.water_use_type
        if self._key == "book_name":
            return h.book_name
        if self._key == "last_update":
            return datetime.now().astimezone()
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
            attrs["地址"] = h.address
        return attrs

    @property
    def suggested_display_precision(self) -> int | None:
        return 2 if self._key in ("balance", "arrears") else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MengziWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    accounts = list(coordinator.data.keys())
    entities = [
        MengziWaterSensor(coordinator, key, acc)
        for acc in accounts
        for key in META
    ]
    if not entities:
        _LOGGER.warning(
            "mengzi_water: 没有获取到可用户号数据，请检查会话 Cookie 是否有效、是否已绑定户号"
        )
    async_add_entities(entities)
