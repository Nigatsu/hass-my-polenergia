<div align="center">
  <img src="logo.png" alt="My Polenergia Logo" width="100"/>
</div>

<h1 align="center">My Polenergia — Home Assistant Integration</h1>

[![GitHub Release](https://img.shields.io/github/v/release/Nigatsu/hass-my-polenergia)](https://github.com/Nigatsu/hass-my-polenergia/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.1%2B-41BDF5)
![API](https://img.shields.io/badge/data_source-OAuth2_API-blue)

🇵🇱 Custom integration for customers of **Polenergia Dystrybucja** that pulls monthly meter readings from the `moja.polenergia.pl` portal API (OAuth2 + PKCE) and feeds them into the **Home Assistant Energy Dashboard** with optional cost tracking in PLN.

> [!IMPORTANT]
> Polenergia's customer API exposes **monthly granularity only** — no hourly/daily readings. Statistics in the Energy Dashboard will appear month-by-month, not minute-by-minute. If that's a dealbreaker for you, this integration is not the right fit.

---

## ✨ Features

- 📡 **OAuth2 + PKCE authentication** — no scraping, secure token flow against the official portal API.
- 📊 **Energy Dashboard ready** — monthly kWh and PLN written straight into the recorder as long-term statistics.
- 🗄️ **Full historical import** — pulls every monthly reading back to your agreement start date.
- 💰 **Cost calculation** — configure a PLN/kWh price and get a matching cost statistics stream.
- 🏠 **Multi-meter support** — one HA device per measurement point (PPE), per customer account.
- 🔁 **Auto re-authentication** — handles token expiry without user intervention.
- 🛠️ **Options menu** — set the price, adjust the poll interval, reload history, clear statistics.

---

## 🏠 Supported accounts and meters

| | |
|---|---|
| **Home Assistant** | **2026.1.0 or newer** |
| **Provider** | Polenergia Dystrybucja (`moja.polenergia.pl`) |
| **Account** | Any account that can log in to the portal, with at least one **active** agreement |
| **Meters** | Every measurement point (PPE) on the selected customer number |
| **Tariffs** | **G11 verified.** G12 / G12w / G12as / G12r / G13 are detected and handled, but unverified — see [Limitations](#️-limitations) |
| **Prosumers** | Export handling is implemented but unverified — see [Limitations](#️-limitations) |
| **Multiple customer numbers** | Supported — add one config entry per customer number |

No hardware is involved: this integration talks only to Polenergia's cloud API.

---

## 📦 Installation

### HACS (custom repository)

1. Open **HACS** → **Integrations** → ⋮ → **Custom repositories**.
2. Add `https://github.com/Nigatsu/hass-my-polenergia` as category **Integration**.
3. Find **My Polenergia** in the list and click **Install**.
4. Restart Home Assistant.

### Manual

1. Download the latest release from [Releases](https://github.com/Nigatsu/hass-my-polenergia/releases).
2. Copy `custom_components/my_polenergia/` to your `config/custom_components/` directory.
3. Restart Home Assistant.

---

## ⚙️ Configuration

1. **Settings** → **Devices & Services** → **+ Add Integration** → search **My Polenergia**.
2. Enter your `moja.polenergia.pl` email and password.
3. If your account has multiple customer numbers, pick one (you can add more entries later, one per customer number).
4. Done — sensors and historical statistics appear within a minute.

### Setup parameters

| Field | Required | Notes |
|---|---|---|
| **Email Address** | yes | The e-mail you log in to `moja.polenergia.pl` with. |
| **Password** | yes | Stored in the config entry, which Home Assistant encrypts at rest. It is needed because Polenergia issues **no refresh token**, so every poll re-logs in — see [Limitations](#️-limitations). |
| **Customer Number** | only if you have several | Which customer number this entry tracks. One entry per number. |

### Options

**Settings** → **Devices & Services** → **My Polenergia** → **Configure**:

| Option | What it does | Default |
|---|---|---|
| **Set Energy Price** | The PLN/kWh rate used to build the cost statistics. On a meter that reports tariff zones, one field per zone is shown; a zone with no price of its own falls back to the base rate. **Tariff zones** overrides automatic zone detection (leave on `auto` unless detection is wrong). | `0.95` PLN/kWh (placeholder) |
| **Update Interval** | How often to poll Polenergia, in seconds. Minimum 900 (15 min). The data only changes monthly, so the default is plenty. | `86400` (24 h) |
| **Reload Historical Statistics** | Rebuilds every statistics stream from scratch. Optional start date (`YYYY-MM-DD`); empty means "from the agreement start date". Runs the `reload_statistics` action. | — |
| **Clear Statistics** | Deletes every statistics stream this integration created. Requires an explicit confirmation tick. Runs the `clear_statistics` action. | — |

To change your password, use **Reconfigure** on the integration entry (⋮ menu on the entry card). Home Assistant also prompts for it automatically if the stored password stops working.

---

## 🔄 How the data is updated

Worth understanding, because it is not the usual "sensor polls a device" shape:

1. **Polling.** Once per update interval the coordinator fetches measurement points, the agreement list and the monthly readings. The access token lives ~30 minutes in memory and is never persisted, and Polenergia grants no refresh token, so every poll begins with a fresh login — normal, not an error.
2. **Entities are informational.** The `Last Month Consumption` sensor shows the most recently published month. It deliberately carries **no `state_class`**, because its value is a single non-cumulative month — giving it one would feed the Energy Dashboard nonsense.
3. **The dashboard is fed by external statistics, not by entities.** After each fetch the integration writes long-term statistics directly into the recorder under IDs like `my_polenergia:<meter>_energy` and `my_polenergia:<meter>_cost`. These are what you select in the Energy Dashboard.
4. **Resume, don't re-import.** Each stream resumes from its last stored month and only appends newer ones, re-checking roughly the last three months so a late correction is still picked up. A meter with no stored statistics gets its full history backfilled automatically.
5. **Month anchors.** Each point is anchored at the reading's billing-period end, plus a zero-delta point at the start of the current month so the dashboard does not extrapolate past the last real reading.
6. **Cost is derived, not billed.** Cost = kWh × your configured price. It is not reconciled against your actual invoices.

Because the streams live in the recorder rather than in an entity, **removing the integration does not remove your history** — see [Removing the integration](#-removing-the-integration).

---

## 📋 Entities

Per measurement point (PPE), each on its own device:

| Entity | Purpose | Unit |
|--------|---------|------|
| `Last Month Consumption` | Latest monthly reading (informational, no `state_class`). | kWh |
| `Import Price` | Diagnostic: the currently configured PLN/kWh. | PLN/kWh |

Both expose the attributes `ppe`, `customer_number`, `address`, `account_name` and `last_update`, plus `tariff` and `zone_count` when the API reports a tariff. `Last Month Consumption` adds `period` (`YYYY-MM`).

### Statistics streams

These are not entities; find them under **Developer Tools** → **Statistics** or in the Energy Dashboard picker.

| Statistic ID | Contents |
|---|---|
| `my_polenergia:<meter>_energy` | Cumulative imported energy (kWh) — the account total |
| `my_polenergia:<meter>_cost` | Cumulative cost (PLN) |
| `my_polenergia:<meter>_energy_z1` … `_z3` | Per-zone energy, **only** when the API supplies zone data |
| `my_polenergia:<meter>_cost_z1` … `_z3` | Per-zone cost, same condition |
| `my_polenergia:<meter>_return[_zN]` | Energy returned to the grid, only for a prosumer meter |

The per-zone streams are written **alongside** the total, never instead of it, so existing history keeps working.

---

## 📊 Energy Dashboard Setup

**Settings** → **Dashboards** → **Energy** → **Electricity grid**.

1. Click **Add consumption**.
2. Pick the statistic named **`<your address> Energy`** (statistic ID `my_polenergia:<meter>_energy`).
3. Cost tracking: **Use an entity tracking the total costs**.
4. Pick **`<your address> Cost`** (`my_polenergia:<meter>_cost`).
5. **Save**.

Monthly bars appear after the next update cycle.

> [!TIP]
> Don't see your meter? The picker lists *statistics*, not entities — type `polenergia` into the search box. `Last Month Consumption` will never appear there, by design.

---

## 💡 Use cases

- **Put your electricity bill on the Energy Dashboard** even though your meter is not smart-read hourly — monthly bars plus real PLN cost.
- **Track year-on-year consumption** in the Energy Dashboard's monthly comparison view, back to your agreement start date.
- **Notice a bad month early** — automate a notification when a newly published month exceeds the previous one, or a threshold you set.
- **Sanity-check your invoices** by comparing the imported kWh against what Polenergia billed you.
- **Split a multi-meter account** (house + garage + workshop) into separate devices and dashboard entries.

---

## 🤖 Automation examples

### Notify when a new month's consumption exceeds the previous one

```yaml
automation:
  - alias: "Polenergia: consumption up month-over-month"
    triggers:
      - trigger: state
        entity_id: sensor.main_st_1_last_month_consumption
        attribute: period
    conditions:
      - condition: template
        value_template: >-
          {{ trigger.from_state is not none
             and trigger.from_state.state not in ['unknown', 'unavailable']
             and trigger.to_state.state | float(0) > trigger.from_state.state | float(0) }}
    actions:
      - action: notify.persistent_notification
        data:
          title: "Polenergia: higher bill coming"
          message: >-
            {{ trigger.to_state.attributes.period }} used
            {{ trigger.to_state.state }} kWh, up from
            {{ trigger.from_state.state }} kWh.
```

### Notify when a month crosses a threshold

```yaml
automation:
  - alias: "Polenergia: month over 300 kWh"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.main_st_1_last_month_consumption
        above: 300
    actions:
      - action: notify.mobile_app_phone
        data:
          message: >-
            {{ state_attr('sensor.main_st_1_last_month_consumption', 'period') }}
            came in at {{ states('sensor.main_st_1_last_month_consumption') }} kWh.
```

### Rebuild statistics after a price change

```yaml
script:
  polenergia_reprice:
    sequence:
      - action: my_polenergia.reload_statistics
```

### Estimated cost of the last month, as a template sensor

```yaml
template:
  - sensor:
      - name: "Polenergia last month cost"
        unit_of_measurement: "PLN"
        device_class: monetary
        state: >-
          {{ (states('sensor.main_st_1_last_month_consumption') | float(0)
              * states('sensor.main_st_1_import_price') | float(0)) | round(2) }}
```

Replace `main_st_1` with your own device slug (it is derived from the meter's address).

---

## 🛠️ Actions

### `my_polenergia.reload_statistics`

Re-fetch all monthly readings and rebuild the energy + cost statistics. Useful after changing the price, or to recover from a failed initial import. Raises an error if the date is malformed or the rebuild fails, so it is safe to use in a script.

```yaml
action: my_polenergia.reload_statistics
data:
  from_date: "2020-01-01"  # optional; defaults to the agreement start date
```

### `my_polenergia.clear_statistics`

Wipe all energy and cost statistics created by this integration from the recorder. Run before `reload_statistics` for a clean slate. Raises an error if there is nothing to clear.

```yaml
action: my_polenergia.clear_statistics
```

Both are also reachable from the Options menu (**Reload Historical Statistics** and **Clear Statistics**).

---

## 🗑️ Removing the integration

1. *(Optional but recommended)* If you want the history gone too, run **Configure** → **Clear Statistics** **before** removing the entry. Long-term statistics live in the recorder, not in the entities, so deleting the integration leaves them behind — they would linger in the Energy Dashboard and statistics picker with no way to remove them from the UI.
2. **Settings** → **Devices & Services** → **My Polenergia** → ⋮ → **Delete**.
3. Restart Home Assistant if you also removed the files.
4. If you installed via HACS: **HACS** → **Integrations** → **My Polenergia** → ⋮ → **Remove**. For a manual install, delete `config/custom_components/my_polenergia/`.

**Nothing needs to be done on the Polenergia side.** The integration only reads the API with your own credentials; it registers no device, webhook or consent with Polenergia, and removing it revokes nothing. If you would rather not leave your password stored in Home Assistant, changing your portal password after removal is enough.

---

## ⚠️ Limitations

- **Monthly granularity only.** No hourly or daily data — Polenergia's API doesn't expose it. The portal offers hourly data as an e-mailed report, not through the API.
- **PLN only.** Cost calculation assumes Polish złoty.
- **Cost is an estimate.** It is kWh × your configured rate, never reconciled against your real invoices.
- **No refresh token** (tested against a live account, 2026-09-19). Polenergia's OAuth client refuses the `offline_access` scope, so the stored password is used to perform a full login on every poll.
- **Multi-zone tariffs (G12/G12w/G13) are supported but unverified.** Your tariff group is
  detected automatically (from the contract data) and shown on the sensor. If the API
  returns zone-split readings, per-zone statistics and per-zone price fields appear on their
  own — no configuration needed. On the single-zone (G11) meter I can test against, the
  readings are flat, so **whether Polenergia actually splits readings by zone is still
  unconfirmed**.
- **No verified prosumer / export tracking.** Export (feed-in) readings are recognised and
  written to a separate "Returned" statistic so they never pollute consumption, but I have
  no prosumer contract to confirm the API exposes them at all.

> Have a G12/G12w/G13 or prosumer account? Please download a diagnostics dump (**Configure** card → ⋮ → **Download diagnostics**; PPE and address are redacted) and open an issue — that is the one thing needed to confirm these paths.

---

## 🐛 Troubleshooting

### Authentication fails
- Confirm credentials work at https://moja.polenergia.pl.
- Wait a few minutes and retry — the portal occasionally rate-limits OAuth.

### "Your session has expired"
Click **Re-authenticate Now** in the integration card and enter your password, or use ⋮ → **Reconfigure**.

### Cost is zero, or a repair issue says the price is unset
1. **Configure** → **Set Energy Price** — save a non-zero value.
2. **Configure** → **Reload Historical Statistics** — rebuilds the cost stream at the new rate.
3. Verify `my_polenergia:<meter>_cost` has rows under **Developer Tools** → **Statistics**.

### A repair issue says a meter has no readings
Polenergia returned no readings for that measurement point. Usually it has not been billed yet, or the agreement is no longer active — check the measurement point in the portal.

### Integration doesn't load
- Check `home-assistant.log` for errors.
- Enable debug logging:
  ```yaml
  logger:
    default: info
    logs:
      custom_components.my_polenergia: debug
  ```

---

## 🧑‍💻 Development

Requires **Home Assistant 2026.1 or newer**. That means Python **3.13.2+**; HA 2026.9 and
later require Python 3.14. CI tests both.

```bash
pip install -r requirements_test.txt
pytest tests --cov=custom_components/my_polenergia --cov-fail-under=95
ruff check custom_components tests
mypy custom_components/my_polenergia   # run this one on Python 3.13
```

The integration source stays Python 3.13-compatible, so `pyproject.toml` pins ruff's
`target-version` and mypy's `python_version` to 3.13 even though CI lints on 3.14.

`custom_components/my_polenergia/quality_scale.yaml` records this integration's self-assessment against the [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/). Hassfest ignores that file for custom integrations, so it is a self-check rather than an enforced gate.

Outstanding: a PR to [`home-assistant/brands`](https://github.com/home-assistant/brands) for `custom_integrations/my_polenergia/`, which would replace the in-repo `brand/icon.png` workaround.

---

### Disclaimer

This is an unofficial custom integration. Not affiliated with or endorsed by Polenergia. Use at your own risk.
