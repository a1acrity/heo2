"""Constants for HEO III integration."""

DOMAIN = "heo3"

# SA MQTT broker defaults (override in config_flow).
DEFAULT_MQTT_HOST = "192.168.4.7"
DEFAULT_MQTT_PORT = 1883

# Inverter identity. Per SPEC §2: writes only ever go to inverter 1.
DEFAULT_INVERTER_NAME = "inverter_1"

# Tick latency budget (§21 resolution).
TICK_HARD_BUDGET_S = 60.0
TICK_WARNING_S = 30.0

# Verification cycle (§16).
WRITE_RETRY_LIMIT = 3
WRITE_RETRY_BACKOFF_S = 5.0
