"""Constants for HEO III integration."""

DOMAIN = "heo3"

# SA MQTT broker defaults (override in config_flow).
DEFAULT_MQTT_HOST = "192.168.4.7"
DEFAULT_MQTT_PORT = 1883

# Inverter identity. Per SPEC §2: writes only ever go to inverter 1.
DEFAULT_INVERTER_NAME = "inverter_1"

# Tick latency budget. Original §21 resolution was 60s/30s but live
# observation showed ~6-10s per write under SA's actual response
# latency, so 10 writes per tick saturated the budget. Bumped to give
# the planner headroom; revisit once we have proper profiling.
TICK_HARD_BUDGET_S = 120.0
TICK_WARNING_S = 60.0

# Verification cycle (§16). RESPONSE_TIMEOUT_S bumped from 5s to 10s
# because some SA responses arrive at the 5-7s mark on Paddy's install
# (probably broker queueing or paho dispatch lag). 10s avoids spurious
# retries that double the per-write cost.
WRITE_RETRY_LIMIT = 3
WRITE_RETRY_BACKOFF_S = 2.0
