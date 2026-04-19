"""Constants for the Yale Access Bluetooth integration."""

from typing import Final

DOMAIN = "yalexs_ble"

CONF_LOCAL_NAME = "local_name"
CONF_KEY = "key"
CONF_SLOT = "slot"
CONF_ALWAYS_CONNECTED = "always_connected"
# Learned-at-runtime value: the battery retrieval method confirmed to work
# for this lock ("gatt" or "log"). Persisted in entry.data (not options)
# because it's not user-tunable — the library flips this on its own when
# GATT probes time out and an activity-log battery reading arrives.
CONF_BATTERY_RETRIEVAL_METHOD = "battery_retrieval_method"

ATTR_REMOTE_TYPE: Final = "remote_type"
ATTR_SLOT: Final = "slot"
ATTR_SOURCE: Final = "source"
ATTR_TIMESTAMP: Final = "timestamp"

DEVICE_TIMEOUT = 55

OPERATION_SENSOR_WRITE_DELAY: Final = 2