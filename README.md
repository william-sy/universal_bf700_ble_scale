# Universal BF700 BLE Scale (Home Assistant)

Native Home Assistant integration for the Beurer / Sanitas "BF700 family" of Bluetooth scales:
Sanitas SBF70, Silvercrest SBF75, Beurer BF700 / BF710 / BF800, Runtastic Libra.
No Docker add-on, no MQTT, no helper service. It uses HA's Bluetooth stack
(`bluetooth` + `bleak-retry-connector`), so **ESPHome Bluetooth proxies work**
(they must have `active: true` connections).

Protocol from [openScale's `BeurerSanitasHandler`](https://github.com/oliexdev/openScale) and
[danielfaust/bt-scale](https://github.com/danielfaust/bt-scale) (both GPL-3.0).

## Install
**HACS:** add `https://github.com/william-sy/ha_universal_ble_scale` as a custom repository (type
Integration), install, and restart. **Manual:** copy `custom_components/universal_bf700_ble_scale` into
your HA `config/custom_components/` and restart. Requires Home Assistant 2026.10 or newer.
Then then step on the scale: it should
be discovered. Otherwise: Settings → Devices → Add Integration → Universal BF700 BLE Scale.

## First-time setup of a profile
Right after you submit the profile, **step on the scale within 30 seconds (barefoot) and stay on until it
finishes.** The profile can only be written while the scale is awake, and the first weigh-in is what
teaches the scale to recognise you by weight (it keeps a reference weight per user). If you stepped on
*before* the profile existed, that weigh-in is stored as "unknown" on the scale and is not imported yet;
just weigh in again. Repeat this for each additional person.

**Tip:** the Bluetooth symbol on the scale's display means Home Assistant is connected. Wait for it
before stepping on (the first weigh-in may be stored as "unknown" if you step on too early), then
weigh in again. Once the scale has learned you, it recognises you by weight and this isn't needed.

## How it works
The scale sleeps and only advertises when stepped on. On advertisement HA connects, syncs the
clock, finds (or creates) your user slot, downloads stored measurements or triggers a live one,
then disconnects. Values appear a few seconds after you step off.

The **scale calculates body fat / water / muscle / bone itself** from the profile stored on it, so
height, age, sex and activity level are written to the scale. Use bare feet on the electrodes or
you only get weight. Change the profile via ⋮ → Reconfigure; it is pushed on next use.

**One entry per scale, one sub-entry per person.** The scale holds the Bluetooth device, battery,
last sync and the scan controls; each person (up to 8 on the scale) has their own profile and
sensors. Add people from the scale's page → **Add person** (each needs a unique 3-character nickname;
edit one via its ⋮ → Reconfigure). A person already on the scale from the vendor app (same nickname
+ birth year) is adopted instead of duplicated. One connection per wake-up serves everyone and also
drains stored weigh-ins so the scale's memory can't fill up.

Every weigh-in is announced as a `universal_bf700_ble_scale_measurement` event (person, timestamp, weight,
body composition), so piled-up history isn't lost even though the sensors show the newest value.

Per person: weight, body fat, body water, muscle, bone mass, BMI (calculated), impedance (disabled by
default), last measurement. Per scale: battery, last sync, scanning, **Scan now** button and
**Scan duration** number. Last values are restored after restart.

## Scan now
HA cannot wake a sleeping scale, only the scale's own step-on does that. **Scan now** opens a scan
window (length = *Scan duration*, default 10 min) in which HA is patient: no cooldown between
sessions and up to 2 minutes of waiting for the weigh-in after the scale wakes. If the scale is
already awake it connects immediately. Press it, then step on. Automate a regular weigh-in:

```yaml
automation:
  - alias: Saturday weigh-in window
    triggers:
      - trigger: time
        at: "10:00:00"
    conditions:
      - condition: time
        weekday: [sat]
    actions:
      - action: number.set_value
        target: {entity_id: number.sanitas_sbf70_scan_duration}
        data: {value: 60}
      - action: button.press
        target: {entity_id: button.sanitas_sbf70_scan_now}
```

**Upgrading from v0.1.x:** the integration was renamed (domain `universal_ble_scale` →
`universal_bf700_ble_scale`), so Home Assistant sees it as a new integration. Remove the old entry and
delete `custom_components/universal_ble_scale`, install this version and set up again. The scale keeps
your user slot (it is matched by nickname and birth year), so the first weigh-in works as before.

## Managing users on the scale
The scale has up to 8 user slots and no screen to manage them. The **Users on scale** sensor (scale
device, diagnostic) shows how many are in use; its attributes list each slot (name, birth year, `uid`,
whether it is a person in HA, whether a delete is queued).

* **Remove a person in HA** → the slot HA created for them is deleted from the scale at its next
  wake-up. A slot HA *adopted* from the vendor app is left alone (it may still be used there).
* **Delete any other slot** (a test user, a leftover): call `universal_bf700_ble_scale.delete_scale_user`
  on the *Users on scale* sensor with the `uid` from its attributes. It's queued and carried out the
  next time the scale wakes (step on it, or press **Scan now** first). People configured in HA are
  refused; remove them from the integration instead.

## The 12.80 kg bug
12.80 kg is raw `0x0100`. Live frames are `[start][0x58][flag][weight u16]` with flag `1` while the
scale settles. Reading the weight from the wrong offset, or trusting the first live frame, yields
exactly 12.80. We read the correct offset, only trust `flag == 0` frames, prefer the full record,
and discard a `0x0100` record that has no impedance/body data (logged as a warning). A genuine
12.80 kg weigh-in with impedance is kept. This is a hypothesis from the protocol; if you see it
anyway, enable debug logging for `custom_components.universal_bf700_ble_scale` and send the hex frames.

## Status / caveats
* Working on a real Sanitas SBF70 (single profile, migration, scan window). Protocol checked against
  openScale, bt-scale and the official Sanitas Health Coach app; multi-person use is lightly tested. With several people configured we do
  not force a measurement for one of them; a weigh-in is picked up live or on the next sync.
* The advertised BLE name of the SBF70 isn't verified; auto-discovery matches several names.
  If yours isn't discovered, use the manual picker.
* Not an official core integration: it is community-maintained and not reviewed by the Home Assistant
  project. Tests cover ~97% and CI runs lint, tests, hassfest and HACS validation.

## Tests
`uv venv --python 3.14 .venv && uv pip install -r requirements_test.txt && pytest`

## Licence
GPL-3.0, see [LICENSE](LICENSE). The wire protocol was worked out from the GPL-3.0 projects
[openScale](https://github.com/oliexdev/openScale) and
[bt-scale](https://github.com/danielfaust/bt-scale) and checked against the vendor's own app.
