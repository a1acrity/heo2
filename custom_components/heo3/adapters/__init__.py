"""Adapters: mechanical I/O between the Operator and the world.

Three adapters compose the State layer (§3):
- InverterAdapter — MQTT W/R against SA broker.
- PeripheralAdapter — HA service calls + reads for zappi, Tesla, appliances.
- WorldGatherer — read-only collation of HA entities (rates, forecasts, flags).

Each adapter produces a slice of the Snapshot. The Operator composes
them in one snapshot() call.
"""
