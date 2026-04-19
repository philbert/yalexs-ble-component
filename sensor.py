"""Support for yalexs ble sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import logging

from yalexs_ble import (
    BatterySource,
    ConnectionInfo,
    DoorActivity,
    LockActivity,
    LockInfo,
    LockState,
)

from homeassistant.components.recorder import get_instance as get_recorder_instance
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfElectricPotential,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er, event as evt
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import YALEXSBLEConfigEntry
from .const import (
    ATTR_REMOTE_TYPE,
    ATTR_SLOT,
    ATTR_SOURCE,
    ATTR_TIMESTAMP,
    DOMAIN,
    OPERATION_SENSOR_WRITE_DELAY,
)
from .entity import YALEXSBLEEntity
from .models import YaleXSBLEData

_LOGGER = logging.getLogger(__name__)

# Restore the last known value for sensors that are populated from drained
# lock log entries (battery, activity). The lock does not re-send these on
# demand, so "unknown" after restart is worse than showing the prior value.
# Cap the age so we don't show indefinitely stale data.
RESTORE_MAX_AGE = timedelta(days=14)


@dataclass(frozen=True, kw_only=True)
class YaleXSBLESensorEntityDescription(SensorEntityDescription):
    """Describes Yale Access Bluetooth sensor entity."""

    value_fn: Callable[[LockState, LockInfo, ConnectionInfo], int | float | None]
    restore: bool = False
    # When True, only restore a previous value if the stored reading came
    # from LOCK_ACTIVITY (i.e. source=LOG). Live GATT readings are fetched
    # again quickly after startup, so restoring stale GATT values just masks
    # the fact that we haven't re-read yet.
    restore_requires_log_source: bool = False


SENSORS: tuple[YaleXSBLESensorEntityDescription, ...] = (
    YaleXSBLESensorEntityDescription(
        key="",  # No key for the original RSSI sensor unique id
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        has_entity_name=True,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_registry_enabled_default=False,
        value_fn=lambda state, info, connection: connection.rssi,
    ),
    YaleXSBLESensorEntityDescription(
        key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        has_entity_name=True,
        native_unit_of_measurement=PERCENTAGE,
        restore=True,
        value_fn=lambda state, info, connection: state.battery.percentage
        if state.battery
        else None,
    ),
    YaleXSBLESensorEntityDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        has_entity_name=True,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        entity_registry_enabled_default=False,
        restore=True,
        restore_requires_log_source=True,
        value_fn=lambda state, info, connection: round(state.battery.voltage * 1000)
        if state.battery
        else None,
    ),
)


def _format_datetime(value: datetime) -> str:
    """Format datetimes for state attributes."""
    return dt_util.as_utc(value).isoformat()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YALEXSBLEConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up YALE XS Bluetooth sensors."""
    data = entry.runtime_data
    registry = er.async_get(hass)
    entities: list[SensorEntity] = []
    connection_state_unique_id = f"{data.lock.address}_connection_state"
    if data.always_connected:
        entities.append(YaleXSBLEConnectionStateSensor(data))
    else:
        # Clean up a stale connection state entity from the registry if the
        # user turned always_connected off after having it enabled.
        if (
            existing := registry.async_get_entity_id(
                "sensor", DOMAIN, connection_state_unique_id
            )
        ) is not None:
            registry.async_remove(existing)
    _migrate_battery_voltage_unit(registry, data.lock.address)
    entities.extend(YaleXSBLESensor(description, data) for description in SENSORS)
    entities.append(YaleXSBLEOperationSensor(data))
    async_add_entities(entities)


def _migrate_battery_voltage_unit(
    registry: er.EntityRegistry, address: str
) -> None:
    """Drop stale 'V' unit overrides from earlier versions of the battery voltage sensor.

    The sensor used to report volts; it now reports millivolts. HA preserves
    the previously-displayed unit in two places: an explicit user override at
    options["sensor"]["unit_of_measurement"], and an auto-set
    options["sensor.private"]["suggested_unit_of_measurement"] that gets
    written when the integration changes native_unit_of_measurement on a
    convertible device class. Both must be cleared so the new mV native unit
    is what actually gets displayed.
    """
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{address}battery_voltage"
    )
    if entity_id is None:
        return
    entry = registry.async_get(entity_id)
    if entry is None:
        return
    sensor_options = entry.options.get("sensor", {})
    if sensor_options.get("unit_of_measurement") == "V":
        new_options = {k: v for k, v in sensor_options.items() if k != "unit_of_measurement"}
        registry.async_update_entity_options(entity_id, "sensor", new_options or None)
    private_options = entry.options.get("sensor.private", {})
    if private_options.get("suggested_unit_of_measurement") == "V":
        new_private = {
            k: v
            for k, v in private_options.items()
            if k != "suggested_unit_of_measurement"
        }
        registry.async_update_entity_options(
            entity_id, "sensor.private", new_private or None
        )


class YaleXSBLEOperationSensor(YALEXSBLEEntity, RestoreSensor):
    """Representation of an YaleXSBLE lock operation sensor."""

    _attr_translation_key = "operation"
    _attr_name = "activity"
    _attr_icon = "mdi:lock-clock"
    _pending_activity_update: DoorActivity | LockActivity | None = None
    _cancel_pending_activity_update: CALLBACK_TYPE | None = None

    def __init__(
        self,
        data: YaleXSBLEData,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(data)
        self._attr_unique_id = f"{data.lock.address}operation"

    @callback
    def _async_activity_update(
        self,
        activity: DoorActivity | LockActivity,
        lock_info: LockInfo,
        connection_info: ConnectionInfo,
    ) -> None:
        """Handle activity update."""

        value, attributes = self._extract_values(activity)

        _LOGGER.debug("creating event for activity update")

        self.hass.bus.async_fire(
            "yalexs_ble.activity",
            {
                "state": value,
                "attributes": attributes,
            },
        )

        if self._pending_activity_update:
            self._record_pending_update()

        self._pending_activity_update = activity

        if self._cancel_pending_activity_update:
            self._cancel_pending_activity_update()

        self._cancel_pending_activity_update = evt.async_call_later(
            self.hass,
            OPERATION_SENSOR_WRITE_DELAY,
            self._flush_pending_update,
        )

    def _record_pending_update(self) -> None:
        if not (activity := self._pending_activity_update):
            return

        native_value, attributes = self._extract_values(activity)
        # now = dt_util.now()
        # timestamp = dt_util.as_timestamp(now)

        state_changed_data: EventStateChangedData = {
            "entity_id": self.entity_id,
            "old_state": None,
            "new_state": State(
                self.entity_id,
                native_value or STATE_UNAVAILABLE,
                attributes,
                last_changed=activity.timestamp,
                last_reported=activity.timestamp,
                last_updated=activity.timestamp,
                last_updated_timestamp=dt_util.as_timestamp(activity.timestamp),
            ),
        }

        _LOGGER.debug("writing historic activity update: %s", state_changed_data)

        instance = get_recorder_instance(self.hass)
        instance.queue_task(Event(str(EVENT_STATE_CHANGED), state_changed_data))

    @callback
    def _flush_pending_update(self, now: Any) -> None:
        if not (activity := self._pending_activity_update):
            return

        _LOGGER.debug("flushing pending activity update")

        self._attr_native_value, self._attr_extra_state_attributes = (
            self._extract_values(activity)
        )
        self._pending_activity_update = None

        self.async_write_ha_state()

    def _extract_values(
        self, activity: DoorActivity | LockActivity
    ) -> tuple[str | None, dict[str, Any]]:
        value: str | None = None
        attributes: dict[str, Any] = {}

        if isinstance(activity, DoorActivity):
            value = f"door_{activity.status.name.lower()}"
            attributes[ATTR_TIMESTAMP] = activity.timestamp
        elif isinstance(activity, LockActivity):
            value = f"lock_{activity.status.name.lower()}"
            attributes[ATTR_TIMESTAMP] = activity.timestamp
            attributes[ATTR_SOURCE] = activity.source.name.lower()
            if activity.remote_type is not None:
                attributes[ATTR_REMOTE_TYPE] = activity.remote_type.name.lower()
            if activity.slot is not None:
                attributes[ATTR_SLOT] = activity.slot

        return (value, attributes)

    async def async_added_to_hass(self) -> None:
        """Register callbacks & perform initial updates."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if (
            last_state is not None
            and last_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            and dt_util.utcnow() - last_state.last_updated <= RESTORE_MAX_AGE
        ):
            self._attr_native_value = last_state.state
            restored_attrs = {
                key: last_state.attributes[key]
                for key in (ATTR_TIMESTAMP, ATTR_SOURCE, ATTR_REMOTE_TYPE, ATTR_SLOT)
                if key in last_state.attributes
            }
            if restored_attrs:
                self._attr_extra_state_attributes = restored_attrs

        self.async_on_remove(
            self._device.register_activity_callback(
                self._async_activity_update, request_update=True
            )
        )

class YaleXSBLESensor(YALEXSBLEEntity, RestoreSensor):
    """Yale XS Bluetooth sensor."""

    entity_description: YaleXSBLESensorEntityDescription

    def __init__(
        self,
        description: YaleXSBLESensorEntityDescription,
        data: YaleXSBLEData,
    ) -> None:
        """Initialize the sensor."""
        self.entity_description = description
        super().__init__(data)
        self._attr_unique_id = f"{data.lock.address}{description.key}"

    @callback
    def _async_update_state(
        self, new_state: LockState, lock_info: LockInfo, connection_info: ConnectionInfo
    ) -> None:
        """Update the state."""
        value = self.entity_description.value_fn(
            new_state, lock_info, connection_info
        )
        # For restore-enabled sensors (battery), the lock only re-reports
        # values via drained log entries. Don't clobber a known value with
        # None just because the latest state read had no battery field.
        if not (
            value is None
            and self.entity_description.restore
            and self._attr_native_value is not None
        ):
            self._attr_native_value = value
        if new_state.battery is not None:
            self._attr_extra_state_attributes = {
                ATTR_SOURCE: new_state.battery.source.value,
            }
        super()._async_update_state(new_state, lock_info, connection_info)

    async def async_added_to_hass(self) -> None:
        """Register callbacks & restore last value if applicable."""
        await super().async_added_to_hass()
        if not self.entity_description.restore:
            return
        last_sensor_data = await self.async_get_last_sensor_data()
        if last_sensor_data is None or last_sensor_data.native_value is None:
            return
        last_state = await self.async_get_last_state()
        if (
            last_state is None
            or dt_util.utcnow() - last_state.last_updated > RESTORE_MAX_AGE
        ):
            return
        if self.entity_description.restore_requires_log_source and (
            self._device.battery_retrieval_method != BatterySource.LOG
        ):
            return
        if self._attr_native_value is None:
            self._attr_native_value = last_sensor_data.native_value
            if (source := last_state.attributes.get(ATTR_SOURCE)) is not None:
                self._attr_extra_state_attributes = {ATTR_SOURCE: source}


class YaleXSBLEConnectionStateSensor(YALEXSBLEEntity, SensorEntity):
    """Yale XS Bluetooth connection health sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connection state"

    def __init__(self, data: YaleXSBLEData) -> None:
        """Initialize the sensor."""
        self._health: Any | None = None
        super().__init__(data)
        self._attr_unique_id = f"{self._device.address}_connection_state"

    @callback
    def _async_update_state(
        self, new_state: LockState, lock_info: LockInfo, connection_info: ConnectionInfo
    ) -> None:
        """Update the state."""
        health = getattr(connection_info, "health", None)
        self._health = health
        self._attr_native_value = health.state if health else None
        super()._async_update_state(new_state, lock_info, connection_info)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return connection health attributes."""
        health = self._health
        if health is None:
            return {}
        attrs: dict[str, Any] = {}
        if health.last_success is not None:
            attrs["last_success"] = _format_datetime(health.last_success)
        if health.last_failure is not None:
            attrs["last_failure"] = _format_datetime(health.last_failure)
        if health.last_presence_seen is not None:
            attrs["last_presence_seen"] = _format_datetime(health.last_presence_seen)
        if health.consecutive_failures is not None:
            attrs["consecutive_failures"] = health.consecutive_failures
        if health.last_error is not None:
            attrs["last_error"] = health.last_error
        return attrs
