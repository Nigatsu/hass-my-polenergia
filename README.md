<div align="center">
  <img src="logo.png" alt="My Polenergia Logo" width="200"/>
</div>

<h1 align="center">My Polenergia — Home Assistant Integration</h1>

[![GitHub Release](https://img.shields.io/github/v/release/Nigatsu/hass-my-polenergia)](https://github.com/Nigatsu/hass-my-polenergia/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![API](https://img.shields.io/badge/data_source-OAuth2_API-blue)

🇵🇱 Custom integration for customers of **Polenergia Dystrybucja** that pulls monthly meter readings from the `moja.polenergia.pl` portal API (OAuth2 + PKCE) and feeds them into the **Home Assistant Energy Dashboard** with optional cost tracking in PLN.

> [!IMPORTANT]
> Polenergia's customer API exposes **monthly granularity only** — no hourly/daily readings. Statistics in the Energy Dashboard will appear month-by-month, not minute-by-minute. If that's a dealbreaker for you, this integration is not the right fit.

---

## ✨ Features

- 📡 **OAuth2 + PKCE authentication** — no scraping, secure token flow against the official portal API.
- 📊 **Energy Dashboard ready** — cumulative monthly kWh statistics fed straight into the HA recorder.
- 💰 **Cost calculation** — configure a PLN/kWh price, get a cumulative cost statistics sensor for Energy Dashboard cost tracking.
- 🏠 **Multi-meter support** — one HA device per measurement point (PPE), per customer account.
- 🔁 **Auto re-authentication** — handles token expiry without user intervention.
- 🛠️ **Options menu** — set price, change credentials, reload history, clear statistics, all from the UI.

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

---

## 💰 Cost Calculation

The integration multiplies each month's kWh by your configured PLN/kWh rate to produce a cumulative cost statistics stream (`Cost Statistics` sensor) that the Energy Dashboard can consume as a total-cost entity.

**Set the price:**

1. **Settings** → **Devices & Services** → **My Polenergia** → **Configure**.
2. Pick **Set Energy Price**.
3. Enter your `import_price` in PLN/kWh (e.g. `0.95`).

After saving, the integration recomputes historical cost statistics with the new rate. The price is also exposed as the `Import Price` diagnostic sensor on each device.

---

## 📋 Sensors

Per measurement point (PPE):

| Sensor | Purpose | Unit |
|--------|---------|------|
| `Last Month Consumption` | Latest monthly reading (informational). | kWh |
| `Historical Statistics` | Cumulative kWh — feed this into Energy Dashboard. Always shows "unavailable" as a state. | kWh |
| `Cost Statistics` | Cumulative PLN — Energy Dashboard cost-tracking entity. Always shows "unavailable" as a state. | PLN |
| `Import Price` | Diagnostic: currently configured PLN/kWh. | PLN/kWh |

The "Historical Statistics" and "Cost Statistics" sensors are **statistics-only** — they appear `unavailable` in the entity list, but their data lives in the recorder and shows up in the Energy Dashboard.

Each sensor exposes attributes: `ppe`, `customer_number`, `address`, `tariff` (when available), `account_name`, `last_update`. The `Last Month Consumption` sensor adds a `period` (`YYYY-MM`) attribute.

---

## 📊 Energy Dashboard Setup

**Settings** → **Dashboards** → **Energy** → **Electricity grid**.

### Grid consumption

1. Click **Add consumption**.
2. Select `sensor.<device_name>_historical_statistics`.
3. Cost tracking: **Use an entity tracking the total costs**.
4. Pick `sensor.<device_name>_cost_statistics`.
5. **Save**.

Monthly bars will appear once the next data update cycle runs.

> [!TIP]
> Don't see your meter under "Add consumption"? Make sure you picked the `_historical_statistics` entity, not `_last_month_consumption` — the latter intentionally has no `state_class` so HA won't offer it as a dashboard candidate.

> [!WARNING]
> Statistics-only sensors show as `unavailable` in the entity list. This is **normal** — their data lives in the recorder, not in live state.

---

## 🛠️ Services

### `my_polenergia.reload_statistics`

Re-fetch all monthly readings and rebuild energy + cost statistics. Useful after changing the price or recovering from a failed initial import.

```yaml
service: my_polenergia.reload_statistics
data:
  from_date: "2020-01-01"  # optional; defaults to agreement start date
```

### `my_polenergia.clear_statistics`

Wipe all energy and cost statistics created by this integration from the recorder. Run before `reload_statistics` for a clean slate.

```yaml
service: my_polenergia.clear_statistics
```

Both services are also reachable from the Options menu (**Reload Historical Statistics** and **Clear Statistics**).

---

## ⚠️ Limitations

- **Monthly granularity only.** No hourly or daily data — Polenergia's API doesn't expose it.
- **PLN only.** Cost calculation assumes Polish złoty.
- **Single import rate.** Currently no zone-aware pricing (G12/G12w/G13). This is **not confirmed as an API limitation** — I only have a single-zone (G11) meter to test against, so the readings I see are flat. The Polenergia API may well return zone-split readings or different fields for G12/G12w/G13 contracts that I simply can't observe. If you're on a multi-zone tariff and willing to share anonymised API responses (or open an issue with what you see), I'd love to add proper zone support.
- **No prosumer / export tracking.** Same caveat — I don't have a prosumer (PV producer-consumer) contract on Polenergia, so I haven't seen export readings in the API responses. The endpoints may exist; data from prosumer users would help confirm or rule it out.

---

## 🐛 Troubleshooting

### Authentication fails
- Confirm credentials work at https://moja.polenergia.pl.
- Wait a few minutes and retry — the portal occasionally rate-limits OAuth.

### "Your session has expired"
Click **Re-authenticate Now** in the integration card and enter your password, or use **Configure** → **Change Credentials**.

### Cost is zero in Energy Dashboard
1. **Configure** → **Set Energy Price** — make sure you saved a non-zero value.
2. **Configure** → **Reload Historical Statistics** — this rebuilds the cost stream with the new price.
3. Verify the `Cost Statistics` entity has rows under **Developer Tools** → **Statistics**.

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

### Disclaimer

This is an unofficial custom integration. Not affiliated with or endorsed by Polenergia. Use at your own risk.
