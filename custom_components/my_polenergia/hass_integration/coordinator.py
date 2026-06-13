"""Data update coordinator for PolEnergia integration."""

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import CONF_IMPORT_PRICE, CURRENCY_PLN, DEFAULT_IMPORT_PRICE, DOMAIN
from ..polenergia.client import PolEnergiaClient
from ..polenergia.data import EnergyReading, MeasurementPoint, PolEnergiaData
from ..polenergia.errors import (
    PolEnergiaAPIError,
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
)

_LOGGER = logging.getLogger(__name__)

# When resuming, re-fetch a little over two months so a freshly published month
# (and any late correction to the previous one) is always covered.
_RESUME_LOOKBACK = timedelta(days=95)
# Fallback history window when the account has no agreement start date.
_FALLBACK_HISTORY = timedelta(days=730)


class PolEnergiaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching PolEnergia data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PolEnergiaClient,
        customer_number: str,
        update_interval: timedelta,
        config_entry: ConfigEntry,
    ):
        self.client = client
        self.customer_number = customer_number

        super().__init__(
            hass,
            _LOGGER,
            name="Polenergia",
            update_interval=update_interval,
            config_entry=config_entry,
        )

    async def _async_setup(self) -> None:
        """One-time authentication before the first refresh.

        ``DataUpdateCoordinator`` calls this once. Raising ``ConfigEntryAuthFailed``
        starts the reauth flow; raising ``UpdateFailed`` here is converted by HA into
        ``ConfigEntryNotReady`` (retried later).
        """
        username = self.config_entry.data[CONF_USERNAME]
        password = self.config_entry.data.get(CONF_PASSWORD)

        if not password:
            raise ConfigEntryAuthFailed(
                "No password available. Please reconfigure the integration."
            )

        try:
            # Token kept in client memory only — re-auth on each HA restart.
            authenticated = await self.client.authenticate(username, password)
            if not authenticated:
                raise ConfigEntryAuthFailed(
                    "Authentication failed. Please check your credentials."
                )
            _LOGGER.info("Authenticated %s", username)
        except PolEnergiaAuthorizationError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed. Please check your credentials."
            ) from err
        except PolEnergiaConnectionError as err:
            raise UpdateFailed(f"Connection failed: {err}") from err

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint and refresh recorder statistics."""
        try:
            data = await self.client.get_all_data(customer_number=self.customer_number)
        except PolEnergiaAuthorizationError as err:
            data = await self._refetch_after_reauth(err)
        except PolEnergiaConnectionError as err:
            raise UpdateFailed(f"Connection failed: {err}") from err
        except PolEnergiaAPIError as err:
            raise UpdateFailed(f"API error: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching data")
            raise UpdateFailed(f"Unexpected error: {err}") from err

        # Statistics import must never break the data refresh — sensors still
        # update even if the recorder write fails.
        try:
            await self.import_statistics(data)
        except Exception:
            _LOGGER.exception("Statistics import failed (sensor data still updated)")

        return {"data": data}

    async def _refetch_after_reauth(self, original_err: Exception) -> PolEnergiaData:
        """Handle an expired token: re-login once, then refetch.

        Token expiry is expected (the token lives in client memory only). If the
        re-login fails on credentials, surface a reauth flow rather than failing
        forever.
        """
        _LOGGER.warning("Token expired during data fetch — attempting re-authentication")

        username = self.config_entry.data[CONF_USERNAME]
        password = self.config_entry.data[CONF_PASSWORD]

        try:
            authenticated = await self.client.authenticate(username, password)
        except PolEnergiaAuthorizationError as reauth_err:
            raise ConfigEntryAuthFailed("Re-authentication failed") from reauth_err
        except PolEnergiaConnectionError as conn_err:
            raise UpdateFailed(f"Connection failed during re-auth: {conn_err}") from conn_err

        if not authenticated:
            raise ConfigEntryAuthFailed("Re-authentication failed") from original_err

        _LOGGER.info("Re-authenticated successfully")

        try:
            return await self.client.get_all_data(customer_number=self.customer_number)
        except PolEnergiaConnectionError as conn_err:
            raise UpdateFailed(f"Connection failed: {conn_err}") from conn_err
        except PolEnergiaAPIError as api_err:
            raise UpdateFailed(f"API error: {api_err}") from api_err

    # ------------------------------------------------------------------ #
    # Statistics import (Energy Dashboard)                               #
    # ------------------------------------------------------------------ #

    def _price(self) -> float:
        return float(self.config_entry.options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE))

    async def _last_stat_sum(self, statistic_id: str) -> tuple[float, float] | None:
        """Return (last_sum, last_start_timestamp) for a stream, or None if empty."""
        rows = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if rows and rows.get(statistic_id):
            row = rows[statistic_id][0]
            return float(row["sum"]), float(row["start"])
        return None

    async def import_statistics(
        self,
        data: PolEnergiaData,
        *,
        from_date: datetime | None = None,
        full_rebuild: bool = False,
    ) -> None:
        """Convert monthly readings to external statistics and push to recorder.

        Runs on every refresh. For each measurement point it resumes from the last
        imported month (``get_last_statistics``) and only appends newer months, so a
        freshly published month reaches the Energy Dashboard without any service call.

        ``full_rebuild`` (used by the reload service) ignores stored statistics and
        recomputes cumulative sums from zero starting at ``from_date`` (or the
        earliest agreement date).
        """
        measurement_points = data.measurement_points
        if not measurement_points:
            return

        price = self._price()
        single_meter = len(measurement_points) == 1

        # Per-mp baselines: (energy_sum, cost_sum, last_start_ts | None).
        baselines: dict[str, tuple[float, float, float | None]] = {}
        earliest_seen: float | None = None
        any_missing = False

        if full_rebuild:
            for mp in measurement_points:
                baselines[mp.id] = (0.0, 0.0, None)
            any_missing = True
        else:
            for mp in measurement_points:
                energy_last = await self._last_stat_sum(f"{DOMAIN}:{mp.id}_energy")
                cost_last = await self._last_stat_sum(f"{DOMAIN}:{mp.id}_cost")
                if energy_last is None:
                    baselines[mp.id] = (0.0, 0.0, None)
                    any_missing = True  # new/first-run meter needs full history
                    continue
                energy_sum, last_ts = energy_last
                cost_sum = cost_last[0] if cost_last else 0.0
                baselines[mp.id] = (energy_sum, cost_sum, last_ts)
                earliest_seen = last_ts if earliest_seen is None else min(earliest_seen, last_ts)

        # If every meter already has stats, resume from the recent window;
        # otherwise fetch full history so a first-run meter is backfilled.
        resume_from = None if any_missing else earliest_seen
        fetch_from = await self._resolve_fetch_from(from_date, resume_from)
        readings = await self.client.get_readings(from_date=fetch_from, to_date=None)
        if not readings:
            _LOGGER.debug("No readings returned from %s — nothing to import", fetch_from)
            return

        for mp in measurement_points:
            energy_sum, cost_sum, last_ts = baselines[mp.id]
            self._import_measurement_point(
                mp, readings, single_meter, price, energy_sum, cost_sum, last_ts
            )

    async def _resolve_fetch_from(
        self,
        from_date: datetime | None,
        resume_from: float | None,
    ) -> datetime:
        """Decide the lower bound for the readings query."""
        if from_date is not None:
            return from_date
        # Resume: refetch a couple of months back from the oldest stored point.
        if resume_from is not None:
            return datetime.fromtimestamp(resume_from, tz=timezone.utc) - _RESUME_LOOKBACK
        # First run / full rebuild with no explicit date: go back to the start.
        earliest = await self.client.get_earliest_agreement_date(self.customer_number)
        if earliest:
            return earliest
        _LOGGER.warning("No agreement date for %s — using 2-year fallback", self.config_entry.title)
        return datetime.now(tz=timezone.utc) - _FALLBACK_HISTORY

    def _import_measurement_point(
        self,
        mp: MeasurementPoint,
        readings: list[EnergyReading],
        single_meter: bool,
        price: float,
        energy_sum: float,
        cost_sum: float,
        last_ts: float | None,
    ) -> None:
        """Build and write the energy + cost streams for one measurement point."""
        mp_readings = [r for r in readings if r.measurement_point_id == mp.id]
        if not mp_readings and single_meter:
            # API omits the id on single-meter accounts — attribute all readings.
            mp_readings = list(readings)
        if not mp_readings:
            return

        mp_readings.sort(key=lambda r: r.period_anchor)

        energy_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []

        for reading in mp_readings:
            if last_ts is not None and reading.period_anchor.timestamp() <= last_ts:
                continue  # already imported
            energy_sum += reading.value
            cost_sum += reading.value * price
            energy_stats.append(StatisticData(
                start=reading.period_anchor,
                state=reading.value,
                sum=energy_sum,
            ))
            cost_stats.append(StatisticData(
                start=reading.period_anchor,
                state=reading.value * price,
                sum=cost_sum,
            ))

        if not energy_stats:
            return  # nothing new for this meter

        # Anchor the start of the current month at zero delta so the dashboard
        # doesn't extrapolate forward from the last real (end-of-month) point.
        now = datetime.now(tz=timezone.utc)
        current_month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if energy_stats[-1]["start"] < current_month_start:
            energy_stats.append(StatisticData(
                start=current_month_start, state=0.0, sum=energy_sum
            ))
            cost_stats.append(StatisticData(
                start=current_month_start, state=0.0, sum=cost_sum
            ))

        self._add_external(f"{DOMAIN}:{mp.id}_energy", mp, "energy", energy_stats)
        self._add_external(f"{DOMAIN}:{mp.id}_cost", mp, "cost", cost_stats)

        _LOGGER.info(
            "Imported %d new %s-stream points for %s (total %.1f kWh, %.2f PLN @ %.4f PLN/kWh)",
            len(energy_stats),
            "energy/cost",
            mp.id,
            energy_sum,
            cost_sum,
            price,
        )

    def _add_external(
        self,
        statistic_id: str,
        mp: MeasurementPoint,
        kind: str,
        stats: list[StatisticData],
    ) -> None:
        """Write one external statistics stream (energy or cost)."""
        if kind == "energy":
            unit = UnitOfEnergy.KILO_WATT_HOUR
            name = f"{mp.display_name} Energy"
        else:
            unit = CURRENCY_PLN
            name = f"{mp.display_name} Cost"

        metadata = StatisticMetaData(
            source=DOMAIN,
            statistic_id=statistic_id,
            name=name,
            unit_of_measurement=unit,
            has_mean=False,
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
        )
        async_add_external_statistics(self.hass, metadata, stats)
