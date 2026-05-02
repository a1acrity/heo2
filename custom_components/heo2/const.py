"""Constants for HEO II integration."""

from datetime import time

DOMAIN = "heo2"

# Battery defaults
DEFAULT_MIN_SOC = 20
DEFAULT_MAX_SOC = 100
DEFAULT_BATTERY_CAPACITY_KWH = 20.48  # 4 x Sunsynk BP51.2 nominal
DEFAULT_MAX_CHARGE_KW = 5.0
DEFAULT_MAX_DISCHARGE_KW = 5.0
DEFAULT_CHARGE_EFFICIENCY = 0.95
DEFAULT_DISCHARGE_EFFICIENCY = 0.95
DEFAULT_DEGRADATION_COST_PER_KWH = 0.001

# Economic constants
ROUND_TRIP_EFFICIENCY = DEFAULT_CHARGE_EFFICIENCY * DEFAULT_DISCHARGE_EFFICIENCY  # 0.9025
DEFAULT_IGO_NIGHT_RATE_PENCE = 7.0
DEFAULT_IGO_DAY_RATE_PENCE = 27.88
DEFAULT_IGO_NIGHT_START = time(23, 30)
DEFAULT_IGO_NIGHT_END = time(5, 30)
EFFECTIVE_STORED_COST_PENCE = (
    DEFAULT_IGO_NIGHT_RATE_PENCE / ROUND_TRIP_EFFICIENCY
) + DEFAULT_DEGRADATION_COST_PER_KWH  # ~7.86

# SPEC-aligned IGO tariff knobs (docs/SPEC.md §1, §10).
# These are the values quoted in the spec; legacy DEFAULT_IGO_*_RATE_PENCE
# above are kept for the synthetic fallback path in igo_rates.py and the
# old EFFECTIVE_STORED_COST_PENCE constant (still used by SolarSurplusRule).
DEFAULT_IGO_OFF_PEAK_PENCE = 4.9524
DEFAULT_IGO_PEAK_PENCE = 24.8423
DEFAULT_PEAK_THRESHOLD_PENCE = 24.0  # H1 detection - covers IGO peak with margin

# SPEC §5a rank-based pricing knobs.
DEFAULT_SELL_TOP_PCT = 30           # medium SOC, normal tomorrow forecast
DEFAULT_SELL_TOP_PCT_LOW_SOC = 15   # low SOC OR low tomorrow forecast
DEFAULT_SELL_TOP_PCT_HIGH_SOC = 50  # high SOC AND high tomorrow forecast
DEFAULT_CHEAP_CHARGE_BOTTOM_PCT = 25  # bottom-N% of import rates is "cheap"
DEFAULT_LOW_SOC_THRESHOLD = 50.0
DEFAULT_HIGH_SOC_THRESHOLD = 80.0

# Load profile
DEFAULT_LOAD_BASELINE_W = 1900.0
DEFAULT_LOAD_LOOKBACK_DAYS = 14
MIN_LOAD_LOOKBACK_DAYS = 7

# MQTT
MQTT_BASE_TOPIC = "solar_assistant"
MQTT_WRITE_TIMEOUT_SECONDS = 10
MQTT_MAX_RETRIES = 3
SLOT_COUNT = 6

# Coordinator
UPDATE_INTERVAL_MINUTES = 15
SOC_DEVIATION_THRESHOLD_PCT = 10.0

# Appliance defaults (draw_kw, duration_hours)
DEFAULT_APPLIANCES = {
    "wash": {"draw_kw": 2.0, "duration_hours": 1.0},
    "dryer": {"draw_kw": 2.5, "duration_hours": 1.0},
    "dishwasher": {"draw_kw": 1.8, "duration_hours": 1.5},
    "ev": {"draw_kw": 7.0, "duration_hours": 2.5},
}

# Payback defaults
DEFAULT_SYSTEM_COST = 16800.0
DEFAULT_ADDITIONAL_COSTS = 0.0
DEFAULT_SAVINGS_TO_DATE = 1131.47
DEFAULT_INSTALL_DATE = "2025-02-01"

# Flat tariff for savings comparison (typical UK SVT rate, p/kWh)
DEFAULT_FLAT_RATE_PENCE = 24.5
