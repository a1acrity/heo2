# Connecting Inverter 2 to Solar Assistant

Current state: only inverter 1 is cabled to the Solar Assistant USB hub. Inverter 2 metrics in HA are reflections of whatever Solar Assistant infers from inverter 1's response over the master/slave RS485 link (note `sensor.sa_inverter_2_serial_number` currently shows 2401264337, the same as inverter 1).

To get real per-inverter telemetry and writable control of inverter 2, connect a second USB RS485 cable from the Solar Assistant box into inverter 2's RS485 monitoring port. Both inverters will then report individually.

Source: Solar Assistant documentation for SunSynk SG01LP1 (the 5kW hybrid family your inverters are part of). See https://solar-assistant.io/help/inverters/sunsynk/SG01LP1/rs485 and the family-wide https://solar-assistant.io/help/deye/configuration.

## What you need

- **1 × USB RS485 cable, Sunsynk/Deye specialised pinout** — you said you already have a spare one of the same type used for inverter 1. Verify this: generic USB RS485 cables from Amazon or eBay will not work. The pinout below is non-standard. If the cable is identical to the one on inverter 1, you're good.
- **Access inside inverter 2** (power down inverter 2, open case, locate the RS485 port).

## Cable pinout (for reference)

| RJ45 pin | Signal  |
|----------|---------|
| 1        | RS485B  |
| 2        | RS485A  |
| 3        | GND     |
| 4–8      | unused  |

## Steps

### 1. Power down inverter 2

Follow your normal Sunsynk power-down procedure. Switch off AC, DC, and battery breakers before opening the case.

### 2. Locate the RS485 monitoring port on inverter 2

The RS485 port is **inside** the inverter (not on the outer case). For the 5kW SG01LP1 models, it's on the communications board near the CAN/BMS ports. The labelling is not always intuitive. Solar Assistant's docs show a diagram — the image at https://solar-assistant.io/help-images/docs/inverters/deye/5k-rs485-port.png is what you want.

Note: this is the port used for RS485 monitoring, NOT the CAN/BMS port used for the battery link, and NOT the WiFi RS232 port on the underside (though the RS232 port can also be used as an alternative — Solar Assistant supports either). You already used RS485 for inverter 1, so stay consistent and use RS485 on inverter 2.

**"2-in-1 BMS port" caveat**: On the latest 5kW firmware the BMS port is used for BOTH SolarAssistant monitoring AND battery CAN at the same time. If your inverter 1 is already connected that way, inverter 2 should be done the same way. See https://solar-assistant.io/help/inverters/sunsynk/SG01LP1/2-in-1-bms-port.

### 3. Plug in the cable

RJ45 end → RS485 port on inverter 2. USB end → a spare USB port on the Solar Assistant Pi.

### 4. Reassemble and power inverter 2 back up

Close the case and restore power.

### 5. Configure Solar Assistant

Open the Solar Assistant web UI (http://192.168.4.84 if the hostname hasn't changed).

Navigate to Configuration. You should see:

- **Inverter model**: "Deye, SunSynk, Sol-Ark" (already selected for inverter 1)
- **USB port**: currently only one USB port is selected. You now need to **multi-select** both USB ports (the existing one for inverter 1 plus the new one for inverter 2).

Click Connect.

### 6. Verify in Home Assistant

After a couple of minutes the MQTT topics `solar_assistant/inverter_2/*` will carry real inverter-2 data rather than repeats of inverter 1's. Easy smoke test: `sensor.sa_inverter_2_serial_number` should change from 2401264337 (currently a duplicate of inverter 1) to inverter 2's actual serial.

Also check:
- `sensor.sa_inverter_2_load_power` now tracks inverter 2 specifically
- `sensor.sa_inverter_2_pv_power_1` / `_2` appear
- `sensor.sa_inverter_2_capacity_point_1` through `_6` and the grid_charge switches reflect the REAL programme on inverter 2

Right now inverter 2 reports a different set of capacity points than inverter 1 (we saw 100/10/100/15/15/15 vs inverter 1's 20/100/100/100/20/20) — once properly linked you can decide whether to harmonise those or keep them intentionally different. HEO II currently only writes to inverter 1 (see HEO-13), so this is cosmetic until we wire up the slave.

## Risks and gotchas

- **Power down before opening** — the inverter has live AC, DC and battery DC inside. Use proper isolation.
- **If the cable doesn't work**: check it's the genuine Sunsynk pinout cable (pin 1 = RS485B, pin 2 = RS485A, pin 3 = GND). If you bought it from Solar Assistant's shop alongside the first one, it's fine.
- **If Solar Assistant shows the USB port but no data** appears after a few minutes: swap the two USB cables between ports (sometimes one port is flaky), or power-cycle the Solar Assistant device.
- **Serial numbers** are the quickest way to confirm each cable talks to the correct inverter.

## After the physical connection works

- HEO II currently hardcodes MQTT writes to `inverter_1` only (tracked as issue #13). Once both inverters are visible in Solar Assistant, we'll need to extend the MqttWriter to publish to both so the slave inverter actually follows the HEO II programme. This is a separate piece of work, not part of the cabling.
- You'll also want to double-check that the master/slave RS485 link between the two inverters stays in place — don't unplug that thinking it's now redundant. The inverters use it for parallel-inverter coordination, which is separate from Solar Assistant monitoring.
