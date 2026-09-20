"""Data update coordinator for PolEnergia integration."""

from datetime import UTC, datetime, timedelta
import logging

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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_IMPORT_PRICE,
    CURRENCY_PLN,
    DEFAULT_IMPORT_PRICE,
    DOMAIN,
    ISSUE_IMPORT_PRICE_UNSET,
    ISSUE_NO_READINGS,
    import_price_key,
)
from .polenergia.client import PolEnergiaClient
from .polenergia.data import EnergyReading, MeasurementPoint, PolEnergiaData
from .polenergia.errors import (
    PolEnergiaAPIError,
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
)
from .polenergia.tariffs import (
    DIRECTION_EXPORT,
    DIRECTION_IMPORT,
    zone_count,
    zone_display_name,
)

_LOGGER = logging.getLogger(__name__)

type PolEnergiaConfigEntry = ConfigEntry[PolEnergiaDataUpdateCoordinator]

# When resuming, re-fetch a little over two months so a freshly published month
# (and any late correction to the previous one) is always covered.
_RESUME_LOOKBACK = timedelta(days=95)
# Fallback history window when the account has no agreement start date.
_FALLBACK_HISTORY = timedelta(days=730)


class PolEnergiaDataUpdateCoordinator(DataUpdateCoordinator[dict[str, PolEnergiaData]]):
    """Class to manage fetching PolEnergia data from the API."""

    # Narrower than the base class's optional entry: this coordinator is only
    # ever built from an entry, and every helper below relies on it.
    config_entry: "PolEnergiaConfigEntry"

    def __init__(
        self,
        hass: HomeAssistant,
        client: PolEnergiaClient,
        customer_number: str,
        update_interval: timedelta,
        config_entry: ConfigEntry,
    ) -> None:
        """Set up the coordinator for one Polenergia customer account."""
        self.client = client
        self.customer_number = customer_number
        # Zone slugs actually seen in readings, per measurement point id. Read by
        # the options flow to decide how many price fields to render. Kept on the
        # coordinator rather than written to the config entry, because calling
        # async_update_entry during a refresh causes a reload loop.
        self.zones_seen: dict[str, list[str]] = {}

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
                translation_domain=DOMAIN, translation_key="no_password"
            )

        try:
            # Token kept in client memory only — re-auth on each HA restart.
            authenticated = await self.client.authenticate(username, password)
            if not authenticated:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="invalid_auth"
                )
            _LOGGER.info("Authenticated %s", username)
        except PolEnergiaAuthorizationError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except PolEnergiaConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _async_update_data(self) -> dict[str, PolEnergiaData]:
        """Fetch data from API endpoint and refresh recorder statistics."""
        try:
            data = await self.client.get_all_data(customer_number=self.customer_number)
        except PolEnergiaAuthorizationError as err:
            data = await self._refetch_after_reauth(err)
        except PolEnergiaConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except PolEnergiaAPIError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching data")
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="unknown_error",
                translation_placeholders={"error": str(err)},
            ) from err

        # Statistics import must never break the data refresh — sensors still
        # update even if the recorder write fails.
        try:
            await self.import_statistics(data)
        except Exception:
            _LOGGER.exception("Statistics import failed (sensor data still updated)")

        self._async_remove_stale_devices(data)
        self._async_update_repair_issues(data)

        return {"data": data}

    # ------------------------------------------------------------------ #
    # Device + repair lifecycle                                          #
    # ------------------------------------------------------------------ #

    def _async_remove_stale_devices(self, data: PolEnergiaData) -> None:
        """Detach devices for measurement points the account no longer has.

        Devices are keyed by PPE. A meter dropped from the contract would
        otherwise keep its device and entities forever.
        """
        current = {mp.ppe for mp in data.measurement_points}
        if not current:
            return  # never prune on an empty payload — that is a fetch problem

        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            ppes = {ident for domain, ident in device.identifiers if domain == DOMAIN}
            if ppes and not ppes & current:
                _LOGGER.info("Removing device for measurement point no longer on the account")
                registry.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )

    def _async_update_repair_issues(self, data: PolEnergiaData) -> None:
        """Raise/clear the repair issues the user can actually act on."""
        options = self.config_entry.options
        price_set = any(
            options.get(import_price_key(zone)) is not None
            for zone in (None, "z1", "z2", "z3")
        )
        issue_id = f"{ISSUE_IMPORT_PRICE_UNSET}_{self.config_entry.entry_id}"
        if price_set:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        else:
            # Cost statistics are computed from this rate, so until it is set
            # every PLN figure on the Energy Dashboard is the placeholder.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_IMPORT_PRICE_UNSET,
                translation_placeholders={
                    "title": self.config_entry.title,
                    "price": f"{DEFAULT_IMPORT_PRICE:.2f}",
                },
            )

        for mp in data.measurement_points:
            mp_issue_id = f"{ISSUE_NO_READINGS}_{mp.id}"
            if data.readings.get(mp.id):
                ir.async_delete_issue(self.hass, DOMAIN, mp_issue_id)
            else:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    mp_issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_NO_READINGS,
                    translation_placeholders={"name": mp.display_name},
                )

    async def _refetch_after_reauth(self, original_err: Exception) -> PolEnergiaData:
        """Handle an expired token: re-login once, then refetch.

        Token expiry is expected (the token lives in client memory only). If the
        re-login fails on credentials, surface a reauth flow rather than failing
        forever.
        """
        # Expected on virtually every scheduled refresh: the OAuth scope has no
        # offline_access, so there is no refresh token and the short-lived access
        # token is always stale by the time the next poll runs.
        _LOGGER.debug(
            "Access token expired during data fetch (expected — no offline_access "
            "scope is granted); re-authenticating"
        )

        username = self.config_entry.data[CONF_USERNAME]
        password = self.config_entry.data[CONF_PASSWORD]

        try:
            authenticated = await self.client.authenticate(username, password)
        except PolEnergiaAuthorizationError as reauth_err:
            _LOGGER.warning("Re-authentication failed for %s", username)
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="reauth_failed"
            ) from reauth_err
        except PolEnergiaConnectionError as conn_err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(conn_err)},
            ) from conn_err

        if not authenticated:
            _LOGGER.warning("Re-authentication failed for %s", username)
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="reauth_failed"
            ) from original_err

        _LOGGER.debug("Re-authenticated successfully")

        try:
            return await self.client.get_all_data(customer_number=self.customer_number)
        except PolEnergiaConnectionError as conn_err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(conn_err)},
            ) from conn_err
        except PolEnergiaAPIError as api_err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(api_err)},
            ) from api_err

    # ------------------------------------------------------------------ #
    # Statistics import (Energy Dashboard)                               #
    # ------------------------------------------------------------------ #

    def _price(self, zone: str | None = None) -> float:
        """Price for a tariff zone, falling back to the single configured rate.

        A multi-zone user who has not set per-zone prices still gets a correct
        energy stream and a blended-rate cost, rather than an error.
        """
        options = self.config_entry.options
        if zone:
            zone_price = options.get(f"{CONF_IMPORT_PRICE}_{zone}")
            if zone_price is not None:
                return float(zone_price)
        return float(options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE))

    def tariff_zone_count(self, measurement_points: list[MeasurementPoint]) -> int:
        """Zone count for the account: what the data shows, else what the tariff implies."""
        seen = {zone for zones in self.zones_seen.values() for zone in zones}
        if seen:
            return max(len(seen), 1)
        counts = [zone_count(mp.tariff) for mp in measurement_points if mp.tariff]
        return max(counts) if counts else 1

    async def _last_stat_sum(self, statistic_id: str) -> tuple[float, float] | None:
        """Return (last_sum, last_start_timestamp) for a stream, or None if empty."""
        rows = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if rows and rows.get(statistic_id):
            row = rows[statistic_id][0]
            return float(row["sum"] or 0.0), float(row["start"])
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

        single_meter = len(measurement_points) == 1

        # The fetch window is decided from the always-present total energy stream;
        # per-zone streams are discovered from the readings and resumed
        # individually in _import_measurement_point.
        earliest_seen: float | None = None
        any_missing = full_rebuild

        if not full_rebuild:
            for mp in measurement_points:
                energy_last = await self._last_stat_sum(f"{DOMAIN}:{mp.id}_energy")
                if energy_last is None:
                    any_missing = True  # new/first-run meter needs full history
                    continue
                _energy_sum, last_ts = energy_last
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
            await self._import_measurement_point(
                mp, readings, single_meter, full_rebuild=full_rebuild
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
            return datetime.fromtimestamp(resume_from, tz=UTC) - _RESUME_LOOKBACK
        # First run / full rebuild with no explicit date: go back to the start.
        earliest = await self.client.get_earliest_agreement_date(self.customer_number)
        if earliest:
            return earliest
        _LOGGER.warning("No agreement date for %s — using 2-year fallback", self.config_entry.title)
        return datetime.now(tz=UTC) - _FALLBACK_HISTORY

    async def _import_measurement_point(
        self,
        mp: MeasurementPoint,
        readings: list[EnergyReading],
        single_meter: bool,
        *,
        full_rebuild: bool,
    ) -> None:
        """Build and write every statistics stream for one measurement point.

        Always writes the total energy + cost streams. When the API supplies a
        zone dimension, per-zone energy + cost streams are written alongside the
        total (never instead of it, so existing history keeps working). Export
        rows go to a separate return stream and are never mixed into consumption.
        """
        mp_readings = [r for r in readings if r.measurement_point_id == mp.id]
        if not mp_readings and single_meter:
            # API omits the id on single-meter accounts — attribute all readings.
            mp_readings = list(readings)
        if not mp_readings:
            return

        mp_readings.sort(key=lambda r: r.period_anchor)

        imports = [r for r in mp_readings if r.direction == DIRECTION_IMPORT]
        exports = [r for r in mp_readings if r.direction == DIRECTION_EXPORT]

        zones = sorted({r.zone for r in imports if r.zone is not None})
        self.zones_seen[mp.id] = zones

        # Total import: sum the zones of each period back together.
        totals = self._sum_by_period(imports)
        await self._write_energy_and_cost(mp, None, totals, full_rebuild=full_rebuild)

        for zone in zones:
            per_zone = self._sum_by_period([r for r in imports if r.zone == zone])
            await self._write_energy_and_cost(mp, zone, per_zone, full_rebuild=full_rebuild)

        if exports:
            await self._write_return(mp, None, self._sum_by_period(exports),
                                     full_rebuild=full_rebuild)
            for zone in sorted({r.zone for r in exports if r.zone is not None}):
                per_zone = self._sum_by_period([r for r in exports if r.zone == zone])
                await self._write_return(mp, zone, per_zone, full_rebuild=full_rebuild)

    @staticmethod
    def _sum_by_period(readings: list[EnergyReading]) -> list[tuple[datetime, float]]:
        """Collapse readings to one (period_anchor, kWh) pair per period, in order."""
        totals: dict[datetime, float] = {}
        for reading in readings:
            anchor = reading.period_anchor
            totals[anchor] = totals.get(anchor, 0.0) + reading.value
        return sorted(totals.items())

    async def _write_energy_and_cost(
        self,
        mp: MeasurementPoint,
        zone: str | None,
        periods: list[tuple[datetime, float]],
        *,
        full_rebuild: bool,
    ) -> None:
        """Write the energy + cost pair for one zone (or the account total)."""
        if not periods:
            return

        suffix = f"_{zone}" if zone else ""
        energy_id = f"{DOMAIN}:{mp.id}_energy{suffix}"
        cost_id = f"{DOMAIN}:{mp.id}_cost{suffix}"
        price = self._price(zone)

        energy_sum, cost_sum, last_ts = 0.0, 0.0, None
        if not full_rebuild:
            if (energy_last := await self._last_stat_sum(energy_id)) is not None:
                energy_sum, last_ts = energy_last
            if (cost_last := await self._last_stat_sum(cost_id)) is not None:
                cost_sum = cost_last[0]

        energy_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []

        for anchor, value in periods:
            if last_ts is not None and anchor.timestamp() <= last_ts:
                continue  # already imported
            # A correction row must never drive the cumulative sum backwards.
            value = max(0.0, value)
            cost = value * price
            energy_sum += value
            cost_sum += cost
            energy_stats.append(StatisticData(start=anchor, state=value, sum=energy_sum))
            cost_stats.append(StatisticData(start=anchor, state=cost, sum=cost_sum))

        if not energy_stats:
            return  # nothing new for this stream

        self._append_current_month_anchor(energy_stats, energy_sum)
        self._append_current_month_anchor(cost_stats, cost_sum)

        label = zone_display_name(zone)
        energy_name = f"{mp.display_name} Energy" + (f" ({label})" if label else "")
        cost_name = f"{mp.display_name} Cost" + (f" ({label})" if label else "")

        self._add_external(energy_id, "energy", energy_name, energy_stats)
        self._add_external(cost_id, "cost", cost_name, cost_stats)

        _LOGGER.info(
            "Imported %d new points for %s%s (total %.1f kWh, %.2f PLN @ %.4f PLN/kWh)",
            len(energy_stats),
            mp.id,
            f" zone {label}" if label else "",
            energy_sum,
            cost_sum,
            price,
        )

    async def _write_return(
        self,
        mp: MeasurementPoint,
        zone: str | None,
        periods: list[tuple[datetime, float]],
        *,
        full_rebuild: bool,
    ) -> None:
        """Write the energy-returned-to-grid stream for a prosumer meter."""
        if not periods:
            return

        suffix = f"_{zone}" if zone else ""
        return_id = f"{DOMAIN}:{mp.id}_return{suffix}"

        return_sum, last_ts = 0.0, None
        if not full_rebuild and (last := await self._last_stat_sum(return_id)) is not None:
            return_sum, last_ts = last

        stats: list[StatisticData] = []
        for anchor, value in periods:
            if last_ts is not None and anchor.timestamp() <= last_ts:
                continue
            value = max(0.0, value)
            return_sum += value
            stats.append(StatisticData(start=anchor, state=value, sum=return_sum))

        if not stats:
            return

        self._append_current_month_anchor(stats, return_sum)

        label = zone_display_name(zone)
        name = f"{mp.display_name} Returned" + (f" ({label})" if label else "")
        self._add_external(return_id, "energy", name, stats)

    @staticmethod
    def _append_current_month_anchor(stats: list[StatisticData], running_sum: float) -> None:
        """Anchor the start of the current month at zero delta.

        Without it the Energy Dashboard extrapolates forward from the last real
        (end-of-month) point.
        """
        now = datetime.now(tz=UTC)
        current_month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
        if stats[-1]["start"] < current_month_start:
            stats.append(StatisticData(start=current_month_start, state=0.0, sum=running_sum))

    def _add_external(
        self,
        statistic_id: str,
        kind: str,
        name: str,
        stats: list[StatisticData],
    ) -> None:
        """Write one external statistics stream (energy or cost)."""
        unit: str
        unit_class: str | None
        if kind == "energy":
            unit = UnitOfEnergy.KILO_WATT_HOUR
            unit_class = "energy"
        else:
            unit = CURRENCY_PLN
            unit_class = None  # PLN has no HA unit converter

        metadata = StatisticMetaData(
            source=DOMAIN,
            statistic_id=statistic_id,
            name=name,
            unit_of_measurement=unit,
            unit_class=unit_class,
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
        )
        async_add_external_statistics(self.hass, metadata, stats)
