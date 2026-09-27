from __future__ import annotations

import math

import numpy as np
import pandas as pd
from transport_contracts import PredictionBatch, PredictionTarget

from .features import ColdFeatureBuilder, _safe_div, _signed_log1p

WINDOWS_MINUTES = (1, 3, 5, 10, 15, 30)
NEIGHBOUR_RADII_M = (300, 500, 1000, 2000)
CATEGORICAL_FALLBACKS = {
    "stop_key",
    "target_address",
    "target_grid_4",
    "target_grid_3",
    "target_grid_2",
    "last_location_valid",
    "last_is_hist",
    "last_speed_outlier",
    "last_alt_outlier",
    "current_grid_4",
    "current_grid_3",
    "current_grid_2",
}


def _haversine_array_m(lon1, lat1, lon2, lat2):
    lon1 = np.asarray(lon1, dtype=float)
    lat1 = np.asarray(lat1, dtype=float)
    lon2 = np.asarray(lon2, dtype=float)
    lat2 = np.asarray(lat2, dtype=float)

    lon1_r = np.radians(lon1)
    lat1_r = np.radians(lat1)
    lon2_r = np.radians(lon2)
    lat2_r = np.radians(lat2)

    dlon = lon2_r - lon1_r
    dlat = lat2_r - lat1_r
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2) ** 2
    )
    return 2 * 6_371_000 * np.arcsin(np.sqrt(a))


def _linear_slope_per_min(seconds, values):
    seconds = np.asarray(seconds, dtype=float)
    values = np.asarray(values, dtype=float)
    mask = np.isfinite(values)
    if mask.sum() < 2:
        return np.nan

    x = seconds[mask] / 60
    y = values[mask]
    x = x - x.mean()
    denominator = np.dot(x, x)
    return float(np.dot(x, y - y.mean()) / denominator) if denominator > 0 else 0.0


class FullFeatureBuilder:
    """Best-effort online reconstruction of the full CatBoost feature vector.

    Features that need source fields absent from the live contract (altitude,
    gps_time, is_hist_data, manual_fill) or fitted offline transforms (PCA) stay
    missing. CatBoost handles the resulting numeric NaNs and explicit missing
    categorical values. The service routes here only after enough history has
    accumulated; cold-start RF remains the fallback.
    """

    def __init__(self, batch: PredictionBatch) -> None:
        self.batch = batch
        self.cold = ColdFeatureBuilder(batch)
        self.as_of = self.cold.as_of
        self.schedule = self.cold.schedule
        self.telemetry = self.cold.telemetry

    def build(self, target: PredictionTarget, expected_features: list[str]) -> pd.DataFrame:
        row = self.cold.build_row(target)
        self._add_schedule_metadata(row, target)
        self._add_unavailable_source_fields(row)

        for minutes in WINDOWS_MINUTES:
            self._add_window(row, target, minutes)

        self._add_neighbours(row, target)
        self._add_full_cross_features(row)
        self._add_full_powers(row)
        self._add_distribution_transforms(row, expected_features)

        for feature in expected_features:
            if feature not in row:
                row[feature] = pd.NA if feature in CATEGORICAL_FALLBACKS else np.nan

        return pd.DataFrame([{feature: row[feature] for feature in expected_features}])

    def history_stats(self, target: PredictionTarget, minutes: int) -> tuple[int, float]:
        vehicle = self._vehicle_history(target, minutes)
        if vehicle.empty:
            return 0, 0.0
        valid = vehicle.loc[vehicle.valid_coords]
        if valid.empty:
            return 0, 0.0
        span = (
            valid.available_time.iloc[-1] - valid.available_time.iloc[0]
        ).total_seconds() / 60
        return int(len(valid)), float(max(span, 0.0))

    def _vehicle_history(self, target: PredictionTarget, minutes: int) -> pd.DataFrame:
        if self.telemetry.empty:
            return self.telemetry
        start = self.as_of - pd.Timedelta(minutes=minutes)
        return self.telemetry.loc[
            (self.telemetry.tr_id.astype(str) == str(target.tr_id))
            & (self.telemetry.available_time >= start)
            & (self.telemetry.available_time <= self.as_of)
        ].sort_values("available_time")

    def _add_schedule_metadata(self, row: dict[str, object], target: PredictionTarget) -> None:
        schedule_row = self.cold._target_schedule_row(target)
        row["manual_fill"] = np.nan
        if schedule_row is None:
            row["stop_key"] = pd.NA
            row["target_address"] = pd.NA
            row["address_missing"] = 1
            return

        row["stop_key"] = schedule_row.get("stop_key", pd.NA)
        address = schedule_row.get("building_address", None)
        row["target_address"] = pd.NA if pd.isna(address) else str(address)
        row["address_missing"] = int(pd.isna(address))

    @staticmethod
    def _add_unavailable_source_fields(row: dict[str, object]) -> None:
        for feature in [
            "last_alt_m",
            "last_is_hist",
            "last_gps_event_lag_s",
            "last_alt_outlier",
            "last_valid_alt_m",
        ]:
            row[feature] = np.nan

    def _add_window(
        self,
        row: dict[str, object],
        target: PredictionTarget,
        minutes: int,
    ) -> None:
        prefix = f"w{minutes}m_"
        vehicle = self._vehicle_history(target, minutes)
        row[prefix + "n"] = len(vehicle)
        if vehicle.empty:
            return

        available = vehicle.available_time.astype("int64").to_numpy()
        event = vehicle.event_time.astype("int64").to_numpy()
        speed = vehicle.speed_clean.to_numpy(dtype=float)
        valid = vehicle.valid_coords.to_numpy(dtype=bool)
        receive_lag = vehicle.receive_lag_s.to_numpy(dtype=float)
        heading = vehicle.heading.to_numpy(dtype=float)
        speed_outlier = vehicle.speed_outlier.to_numpy(dtype=bool)
        lon = vehicle.lon.to_numpy(dtype=float)
        lat = vehicle.lat.to_numpy(dtype=float)

        span_s = max((available[-1] - available[0]) / 1e9, 0.0)
        elapsed_s = (available - available[0]) / 1e9

        row[prefix + "span_s"] = span_s
        row[prefix + "packets_per_min"] = len(vehicle) / minutes
        row[prefix + "valid_share"] = float(valid.mean())
        row[prefix + "speed_outlier_share"] = float(speed_outlier.mean())
        row[prefix + "hist_share"] = np.nan
        row[prefix + "alt_outlier_share"] = np.nan

        speed_ok = speed[np.isfinite(speed)]
        if len(speed_ok):
            q10, q25, q50, q75, q90 = np.quantile(
                speed_ok, [0.10, 0.25, 0.50, 0.75, 0.90]
            )
            row[prefix + "speed_mean"] = float(speed_ok.mean())
            row[prefix + "speed_median"] = float(q50)
            row[prefix + "speed_std"] = float(speed_ok.std())
            row[prefix + "speed_min"] = float(speed_ok.min())
            row[prefix + "speed_max"] = float(speed_ok.max())
            row[prefix + "speed_q10"] = float(q10)
            row[prefix + "speed_q90"] = float(q90)
            row[prefix + "speed_iqr"] = float(q75 - q25)
            row[prefix + "stopped_share"] = float((speed_ok <= 1).mean())
            row[prefix + "slow_share"] = float((speed_ok <= 5).mean())
            row[prefix + "fast_share"] = float((speed_ok >= 40).mean())
            row[prefix + "speed_first"] = float(speed_ok[0])
            row[prefix + "speed_last"] = float(speed_ok[-1])
            row[prefix + "speed_delta"] = float(speed_ok[-1] - speed_ok[0])
            row[prefix + "speed_slope"] = _linear_slope_per_min(elapsed_s, speed)
            if len(speed_ok) > 1:
                delta_speed = np.diff(speed_ok)
                row[prefix + "speed_change_abs_mean"] = float(
                    np.abs(delta_speed).mean()
                )
                row[prefix + "stop_transitions"] = int(
                    ((speed_ok[:-1] > 1) & (speed_ok[1:] <= 1)).sum()
                )

        valid_speed = speed[valid & np.isfinite(speed)]
        if len(valid_speed):
            row[prefix + "valid_speed_mean"] = float(valid_speed.mean())
            row[prefix + "valid_speed_median"] = float(np.median(valid_speed))

        receive_lag = receive_lag[np.isfinite(receive_lag)]
        if len(receive_lag):
            row[prefix + "receive_lag_median"] = float(np.median(receive_lag))
            row[prefix + "receive_lag_p95"] = float(np.quantile(receive_lag, 0.95))
            row[prefix + "receive_lag_max"] = float(receive_lag.max())
            row[prefix + "receive_lag_negative_share"] = float(
                (receive_lag < 0).mean()
            )

        if len(event) > 1:
            gaps = np.diff(event) / 1e9
            row[prefix + "event_gap_median"] = float(np.median(gaps))
            row[prefix + "event_gap_p95"] = float(np.quantile(gaps, 0.95))
            row[prefix + "event_gap_max"] = float(gaps.max())
            row[prefix + "event_time_backwards_share"] = float((gaps < 0).mean())

        heading = heading[np.isfinite(heading)]
        if len(heading):
            radians = np.radians(heading)
            row[prefix + "heading_resultant"] = float(
                np.hypot(np.sin(radians).mean(), np.cos(radians).mean())
            )

        valid_idx = np.flatnonzero(
            valid & np.isfinite(lon) & np.isfinite(lat)
        )
        if not len(valid_idx):
            return

        valid_lon = lon[valid_idx]
        valid_lat = lat[valid_idx]
        valid_time = available[valid_idx]
        row[prefix + "valid_n"] = len(valid_idx)

        if np.isfinite(row.get("stop_lon", np.nan)) and np.isfinite(
            row.get("stop_lat", np.nan)
        ):
            distance = _haversine_array_m(
                valid_lon,
                valid_lat,
                float(row["stop_lon"]),
                float(row["stop_lat"]),
            )
            row[prefix + "target_distance_first_m"] = float(distance[0])
            row[prefix + "target_distance_last_m"] = float(distance[-1])
            row[prefix + "target_progress_m"] = float(distance[0] - distance[-1])

        if len(valid_idx) <= 1:
            return

        steps = _haversine_array_m(
            valid_lon[:-1], valid_lat[:-1], valid_lon[1:], valid_lat[1:]
        )
        path_m = float(np.nansum(steps))
        displacement_m = float(
            _haversine_array_m(
                valid_lon[0], valid_lat[0], valid_lon[-1], valid_lat[-1]
            )
        )
        valid_span_s = (valid_time[-1] - valid_time[0]) / 1e9

        row[prefix + "path_m"] = path_m
        row[prefix + "displacement_m"] = displacement_m
        row[prefix + "tortuosity"] = path_m / max(displacement_m, 1.0)
        row[prefix + "step_median"] = float(np.nanmedian(steps))
        row[prefix + "step_p95"] = float(np.nanquantile(steps, 0.95))

        if valid_span_s > 0:
            row[prefix + "trajectory_speed_kmh"] = 3.6 * path_m / valid_span_s
            progress_m = row.get(prefix + "target_progress_m", np.nan)
            row[prefix + "target_progress_kmh"] = (
                np.nan if pd.isna(progress_m) else 3.6 * progress_m / valid_span_s
            )

    def _add_neighbours(self, row: dict[str, object], target: PredictionTarget) -> None:
        if self.telemetry.empty:
            row["snapshot_vehicle_n"] = 0
            return

        valid = self.telemetry.loc[
            self.telemetry.valid_coords & (self.telemetry.available_time <= self.as_of)
        ].sort_values(["tr_id", "available_time"])
        if valid.empty:
            row["snapshot_vehicle_n"] = 0
            return

        snapshot = valid.groupby("tr_id", sort=False).tail(1)
        row["snapshot_vehicle_n"] = len(snapshot)
        city_speed = snapshot.speed_clean.dropna().to_numpy(dtype=float)
        if len(city_speed):
            row["city_speed_mean"] = float(city_speed.mean())
            row["city_speed_median"] = float(np.median(city_speed))
            row["city_slow_share"] = float((city_speed <= 5).mean())

        other = snapshot.tr_id.astype(str).ne(str(target.tr_id)).to_numpy()
        speed = snapshot.speed_clean.to_numpy(dtype=float)
        lon = snapshot.lon.to_numpy(dtype=float)
        lat = snapshot.lat.to_numpy(dtype=float)

        current_lon = row.get("last_valid_lon", np.nan)
        current_lat = row.get("last_valid_lat", np.nan)
        target_lon = row.get("stop_lon", np.nan)
        target_lat = row.get("stop_lat", np.nan)

        distance_current = (
            _haversine_array_m(lon, lat, current_lon, current_lat)
            if np.isfinite(current_lon) and np.isfinite(current_lat)
            else np.full(len(snapshot), np.nan)
        )
        distance_target = (
            _haversine_array_m(lon, lat, target_lon, target_lat)
            if np.isfinite(target_lon) and np.isfinite(target_lat)
            else np.full(len(snapshot), np.nan)
        )

        for radius in NEIGHBOUR_RADII_M:
            for name, distance in (
                ("local", distance_current),
                ("target_area", distance_target),
            ):
                mask = other & np.isfinite(distance) & (distance <= radius)
                row[f"{name}_vehicles_{radius}m"] = int(mask.sum())
                nearby_speed = speed[mask]
                nearby_speed = nearby_speed[np.isfinite(nearby_speed)]
                if len(nearby_speed):
                    row[f"{name}_speed_mean_{radius}m"] = float(nearby_speed.mean())
                    row[f"{name}_speed_median_{radius}m"] = float(
                        np.median(nearby_speed)
                    )
                    row[f"{name}_slow_share_{radius}m"] = float(
                        (nearby_speed <= 5).mean()
                    )

    @staticmethod
    def _add_full_cross_features(row: dict[str, object]) -> None:
        for short, long in ((1, 5), (3, 15), (5, 15), (5, 30), (10, 30)):
            for name, source in (
                ("speed_mean", "speed_mean"),
                ("slow_share", "slow_share"),
                ("valid_share", "valid_share"),
                ("progress", "target_progress_kmh"),
            ):
                short_value = row.get(f"w{short}m_{source}", np.nan)
                long_value = row.get(f"w{long}m_{source}", np.nan)
                row[f"{name}_{short}m_minus_{long}m"] = (
                    np.nan
                    if pd.isna(short_value) or pd.isna(long_value)
                    else float(short_value) - float(long_value)
                )

        required = row.get("required_speed_kmh", np.nan)
        w5_speed = row.get("w5m_speed_mean", np.nan)
        w15_speed = row.get("w15m_speed_mean", np.nan)
        w5_progress = row.get("w5m_target_progress_kmh", np.nan)
        cur_dev = row.get("cur_dev_s", np.nan)

        row["speed_recent_vs_required"] = (
            np.nan if pd.isna(w5_speed) or pd.isna(required) else w5_speed - required
        )
        row["progress_recent_vs_required"] = (
            np.nan if pd.isna(w5_progress) or pd.isna(required) else w5_progress - required
        )
        row["speed_15m_vs_required"] = (
            np.nan if pd.isna(w15_speed) or pd.isna(required) else w15_speed - required
        )
        row["route_speed_margin_kmh"] = (
            np.nan
            if pd.isna(w5_speed) or pd.isna(row.get("route_proxy_required_speed_kmh", np.nan))
            else w5_speed - row["route_proxy_required_speed_kmh"]
        )
        row["required_vs_w5_speed_ratio"] = _safe_div(required, w5_speed)
        row["route_required_vs_w5_speed_ratio"] = _safe_div(
            row.get("route_proxy_required_speed_kmh", np.nan), w5_speed
        )
        row["cur_dev_x_w5_speed"] = (
            np.nan if pd.isna(cur_dev) or pd.isna(w5_speed) else cur_dev * w5_speed
        )
        row["cur_dev_x_w15_valid_share"] = (
            np.nan
            if pd.isna(cur_dev) or pd.isna(row.get("w15m_valid_share", np.nan))
            else cur_dev * row["w15m_valid_share"]
        )
        row["cur_dev_x_route_speed_margin"] = (
            np.nan
            if pd.isna(cur_dev) or pd.isna(row["route_speed_margin_kmh"])
            else cur_dev * row["route_speed_margin_kmh"]
        )

        for minutes in WINDOWS_MINUTES:
            row[f"w{minutes}m_progress_fraction"] = _safe_div(
                row.get(f"w{minutes}m_target_progress_m", np.nan),
                row.get(f"w{minutes}m_target_distance_first_m", np.nan),
            )
            row[f"w{minutes}m_path_to_route_ratio"] = _safe_div(
                row.get(f"w{minutes}m_path_m", np.nan),
                row.get("route_proxy_m", np.nan),
            )

    @staticmethod
    def _add_full_powers(row: dict[str, object]) -> None:
        for feature in ("w5m_speed_mean", "w15m_speed_mean"):
            value = row.get(feature, np.nan)
            row[f"{feature}_sq"] = np.nan if pd.isna(value) else float(value) ** 2

    @staticmethod
    def _add_distribution_transforms(
        row: dict[str, object], expected_features: list[str]
    ) -> None:
        for feature in expected_features:
            if feature in row:
                continue
            if feature.endswith("_signed_log1p"):
                source = feature[: -len("_signed_log1p")]
                if source in row:
                    row[feature] = _signed_log1p(row[source])
            elif feature.endswith("_log1p"):
                source = feature[: -len("_log1p")]
                if source in row:
                    value = row[source]
                    row[feature] = (
                        np.nan
                        if pd.isna(value)
                        else float(np.log1p(max(float(value), 0.0)))
                    )
