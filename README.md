# Universal BLE Scale (Home Assistant)

Native Home Assistant integration for the Beurer / Sanitas "BF700 family" of Bluetooth scales:
Sanitas SBF70, Silvercrest SBF75, Beurer BF700 / BF710 / BF800, Runtastic Libra.
No Docker add-on, no MQTT, no helper service. It uses HA's Bluetooth stack
(`bluetooth` + `bleak-retry-connector`), so **ESPHome Bluetooth proxies work**
(they must have `active: true` connections).

Protocol from [openScale's `BeurerSanitasHandler`](https://github.com/oliexdev/openScale) and
[danielfaust/bt-scale](https://github.com/danielfaust/bt-scale) (both GPL-3.0).

## Install
Copy `custom_components/universal_ble_scale` into your HA `config/custom_components/`
(or add this repo to HACS as a custom repository), restart, then step on the scale: it should
be discovered. Otherwise: Settings → Devices → Add Integration → Universal BLE Scale.

## First-time setup of a profile
Right after you submit the profile, **step on the scale within 30 seconds (barefoot) and stay on until it
finishes.** The profile can only be written while the scale is awake, and the first weigh-in is what
teaches the scale to recognise you by weight (it keeps a reference weight per user). If you stepped on
*before* the profile existed, that weigh-in is stored as "unknown" on the scale and is not imported yet;
just weigh in again. Repeat this for each additional person.

## How it works
The scale sleeps and only advertises when stepped on. On advertisement HA connects, syncs the
clock, finds (or creates) your user slot, downloads stored measurements or triggers a live one,
then disconnects. Values appear a few seconds after you step off.

The **scale calculates body fat / water / muscle / bone itself** from the profile stored on it, so
height, age, sex and activity level are written to the scale. Use bare feet on the electrodes or
you only get weight. Change the profile via ⋮ → Reconfigure; it is pushed on next use.

One config entry per person. A second person on the same scale: Add Integration → pick the
scale manually and use a different 3-character nickname. If you already have a user on the scale
from the vendor app (same nickname + birth year) it is adopted instead of duplicated.

Sensors: weight, body fat, body water, muscle, bone mass, BMI (calculated), impedance (disabled by
default), battery, last measurement. Last values are restored after restart.

## The 12.80 kg bug
12.80 kg is raw `0x0100`. Live frames are `[start][0x58][flag][weight u16]` with flag `1` while the
scale settles. Reading the weight from the wrong offset, or trusting the first live frame, yields
exactly 12.80. We read the correct offset, only trust `flag == 0` frames, prefer the full record,
and discard a `0x0100` record that has no impedance/body data (logged as a warning). A genuine
12.80 kg weigh-in with impedance is kept. This is a hypothesis from the protocol; if you see it
anyway, enable debug logging for `custom_components.universal_ble_scale` and send the hex frames.

## Status / caveats
* Developed against the reference sources and a simulated scale, **not yet tested on real hardware**.
  Frame offsets for the full record follow openScale; bt-scale differs slightly.
* The advertised BLE name of the SBF70 isn't verified; auto-discovery matches several names.
  If yours isn't discovered, use the manual picker.
* Quality scale: all rules but strict typing (mypy) are addressed, tests cover ~95%. Not an official
  core integration, so no official tier.

## Tests
`uv venv --python 3.14 .venv && uv pip install -r requirements_test.txt && pytest`
