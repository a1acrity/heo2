# Connecting Inverter 2 to Solar Assistant

Current state: Only inverter 1 has a direct cable to the Solar Assistant USB hub. Inverter 2 data in HA (`sensor.sa_inverter_2_*`) is reflected via the master/slave RS485 link between the two inverters, not read directly. Diagnostic: `sensor.sa_inverter_2_serial_number` shows 2401264337, the same as inverter 1, confirming the data is inferred rather than directly measured.

Important constraint: **The RS485 port on inverter 2 is occupied by the master/slave parallel-operation cable to inverter 1**. That cable must stay in place — it is what makes the parallel operation work at all. So the option used for inverter 1 (RS485 internal port) is not available for inverter 2.

## Do you actually need direct Solar Assistant telemetry for inverter 2?

Before doing anything, ask this question honestly. The answer depends on two things:

1. **Are you happy with inferred inverter-2 metrics?** The data via the master/slave link is accurate for monitoring purposes (total system view, aggregate power flows). You lose per-inverter breakdown but HEO II's rules operate on aggregate SOC/load/export anyway.

2. **Does Solar Assistant relay HEO II's MQTT writes to inverter 2 via the parallel link?** This is the real question. HEO II currently writes `solar_assistant/inverter_1/set_time_point_X/set`. The equivalent `solar_assistant/inverter_2/...` topics exist in the MQTT tree even without a direct SA connection (because SA knows about both inverters from inverter 1's parallel-mode reports). The question is whether **writes** on `inverter_2/*` get acted on through the master/slave RS485 link between the two inverters.

If writes relay via the parallel link, you don't need a second Solar Assistant connection at all — HEO-13 just needs to duplicate the writes to both topic trees.

If writes do NOT relay, you'll need a second direct connection. Then see Options A or B below.

## Testing the relay first (recommended)

This test tells us whether we need the hardware at all.

1. Note inverter 2's current setting for one timer slot, e.g. via `sensor.sa_inverter_2_capacity_point_2` (currently 10)
2. Publish a change via MQTT on the inverter_2 topic, using `mosquitto_pub` or the HA UI's MQTT dev tool:
   ```
   topic: solar_assistant/inverter_2/capacity_point_2/set
   payload: 95
   ```
3. Wait 30 seconds. Check whether `sensor.sa_inverter_2_capacity_point_2` now reads 95, AND whether the physical inverter 2 accepted the change (look at the panel if accessible, or the Sunsynk Connect app).

If the readback shows 95 and the physical inverter followed → **relay works**, no second SA connection needed.

If the readback doesn't change OR the physical inverter ignored it → **relay doesn't work**, pick Option A or B.

Undo the test change afterwards by setting the point back to its original value.

## Option A: RS232 USB serial cable (if relay fails)

Sunsynk inverters have a second comms port — the **RS232 WiFi/dongle port on the bottom of the inverter**. This is completely separate from the internal RS485 port. It's where the stock Sunsynk WiFi dongle plugs in — if you don't use the Sunsynk cloud, this port is free.

**What you need**: a USB-to-RS232 DB9 serial cable with Prolific PL2303 or FTDI chipset. Generic and available on Amazon for about £10-15. Unlike the RS485 cable, this one has no proprietary pinout — any standard USB serial cable works. Solar Assistant's docs confirm: "a normal USB serial cable available from most electronic stores" (https://solar-assistant.io/help/inverters/sunsynk/SG01LP1/rs232).

Your existing spare USB RS485 cable **will not work** for this port — wrong protocol, wrong connector.

**Caveat**: "When using this option, you will not be able to use the standard Deye/SunSynk dongle as the dongle can only be connected to this port." Not an issue if you're not using the Sunsynk cloud app.

**Steps** (only if you buy the cable):
1. Locate the RS232 port on the *underside* of inverter 2 (where a WiFi dongle would go if installed)
2. Plug the DB9 end of the USB serial cable in
3. Plug the USB end into the Solar Assistant Pi
4. In the Solar Assistant web UI → Configuration, multi-select both USB ports (the existing RS485 for inverter 1 plus the new RS232 for inverter 2)
5. Click Connect
6. Verify: `sensor.sa_inverter_2_serial_number` should change from 2401264337 to inverter 2's real serial

## Option B: Solarman WiFi dongle (if you have one spare)

If inverter 2 came with a Sunsynk/Deye WiFi dongle in the box, Solar Assistant can read from the Solarman cloud via your credentials. No cable.

**Disadvantages:**
- Cloud-dependent — Solarman outage or internet problem = no data
- Higher latency — data lags ~5 minutes versus real-time over cable
- Control latency — HEO II writes may take 5+ minutes to propagate

Not recommended for HEO II's use case. Listed here only because Solar Assistant supports it and some people prefer it.

## Option C: Leave it as-is (recommended for now)

Given where we are (dry_run still true, commissioning phase, HEO II not yet writing anything):
- Inferred inverter-2 data is sufficient
- HEO-13 (duplicate writes to both inverters) is the real blocker for using the second inverter properly, and may not need additional hardware — run the relay test above first
- Even when HEO II goes live, if the relay test passes, a second Solar Assistant cable may never be needed

**Recommendation: don't buy hardware until we've proven the MQTT relay test fails.**

## Cable reference (for when/if you do Option A)

RS232 to DB9 serial cable. Common chipsets:
- **Prolific PL2303** — widely compatible, cheapest. Some Windows driver issues but fine on Linux (Solar Assistant is Linux).
- **FTDI FT232** — slightly more expensive, generally more reliable.

Solar Assistant's shop sells one: https://solar-assistant.io/shop/products/sunsynk_rs232

Generic example referenced in their docs: https://www.amazon.com/Adapter-Chipset%EF%BC%8CDB9-Serial-Converter-Windows/dp/B0759HSLP1/

## Sources

- Solar Assistant connection options: https://solar-assistant.io/help/inverters/sunsynk/SG01LP1
- RS485 connection (used for inverter 1): https://solar-assistant.io/help/inverters/sunsynk/SG01LP1/rs485
- RS232 connection (needed for inverter 2 if relay fails): https://solar-assistant.io/help/inverters/sunsynk/SG01LP1/rs232
- Deye/SunSynk family configuration: https://solar-assistant.io/help/deye/configuration
