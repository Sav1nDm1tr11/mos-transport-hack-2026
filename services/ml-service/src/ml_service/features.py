from __future__ import annotations

import math

import numpy as np
import pandas as pd
from transport_contracts import PredictionBatch, PredictionTarget

SPEED_CAP_KMH = 120.0


def _haversine_m(lon1, lat1, lon2, lat2):
    if any(pd.isna(value) for value in [lon1, lat1, lon2, lat2]):
        return np.nan

    lon1, lat1, lon2, lat2 = map(
        np.radians,
        [float(lon1), float(lat1), float(lon2), float(lat2)],
    )
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    )
    return float(2 * 6_371_000 * np.arcsin(np.sqrt(a)))


def _bearing_deg(lon1, lat1, lon2, lat2):
    if any(pd.isna(value) for value in [lon1, lat1, lon2, lat2]):
        return np.nan

    lon1, lat1, lon2, lat2 = map(
        np.radians,
        [float(lon1), float(lat1), float(lon2), float(lat2)],
    )
    dlon = lon2 - lon1
    y = np.sin(dlon) * np.cos(lat2)
    x = (
        np.cos(lat1) * np.sin(lat2)
        - np.sin(lat1) * np.cos(lat2) * np.cos(dlon)
    )
    return float((np.degrees(np.arctan2(y, x)) + 360) % 360)


def _angle_diff_deg(a, b):
    if pd.isna(a) or pd.isna(b):
        return np.nan
    return float((float(a) - float(b) + 180) % 360 - 180)


def _signed_log1p(value):
    if pd.isna(value):
        return np.nan
    return float(np.sign(value) * np.log1p(abs(float(value))))


def _safe_div(num, den):
    if pd.isna(num) or pd.isna(den) or float(den) == 0:
        return np.nan
    return float(num) / float(den)


def _grid(lon, lat, digits):
    if pd.isna(lon) or pd.isna(lat):
        return pd.NA
    values = pd.Series([float(lon), float(lat)]).round(digits).astype("string")
    return f"{values.iloc[0]}_{values.iloc[1]}"


class ColdFeatureBuilder:
    def __init__(self, batch: PredictionBatch) -> None:
        self.batch = batch
        self.as_of = pd.Timestamp(batch.as_of)
        self.schedule = self._prepare_schedule(batch)
        self.telemetry = self._prepare_telemetry(batch)

    @staticmethod
    def _prepare_schedule(batch: PredictionBatch) -> pd.DataFrame:
        rows = [
            {
                "stop_visit_id": visit.stop_visit_id,
                "tr_id": visit.tr_id,
                "time_begin": pd.Timestamp(visit.time_begin),
                "stop_lon": visit.lon,
                "stop_lat": visit.lat,
                "building_address": visit.building_address,
            }
            for visit in batch.schedule_context
        ]

        columns = [
            "stop_visit_id",
            "tr_id",
            "time_begin",
            "stop_lon",
            "stop_lat",
            "building_address",
        ]
        schedule = pd.DataFrame(rows, columns=columns)
        if schedule.empty:
            return schedule

        schedule["stop_key"] = (
            schedule.stop_lon.round(5).astype("string")
            + "_"
            + schedule.stop_lat.round(5).astype("string")
        )
        schedule = schedule.sort_values(
            ["tr_id", "time_begin"]
        ).reset_index(drop=True)

        by_vehicle = schedule.groupby("tr_id", sort=False)
        schedule["schedule_pos"] = by_vehicle.cumcount()
        schedule["schedule_rows"] = by_vehicle.tr_id.transform("size")
        schedule["schedule_progress"] = schedule.schedule_pos / (
            schedule.schedule_rows - 1
        ).replace(0, np.nan)

        schedule["planned_gap_prev_s"] = (
            by_vehicle.time_begin.diff().dt.total_seconds()
        )
        schedule["planned_gap_next_s"] = (
            by_vehicle.time_begin.shift(-1) - schedule.time_begin
        ).dt.total_seconds()

        schedule["prev_stop_lon"] = by_vehicle.stop_lon.shift(1)
        schedule["prev_stop_lat"] = by_vehicle.stop_lat.shift(1)
        schedule["next_stop_lon"] = by_vehicle.stop_lon.shift(-1)
        schedule["next_stop_lat"] = by_vehicle.stop_lat.shift(-1)

        schedule["prev_segment_m"] = [
            _haversine_m(a, b, c, d)
            for a, b, c, d in zip(
                schedule.prev_stop_lon,
                schedule.prev_stop_lat,
                schedule.stop_lon,
                schedule.stop_lat,
                strict=True,
            )
        ]
        schedule["next_segment_m"] = [
            _haversine_m(a, b, c, d)
            for a, b, c, d in zip(
                schedule.stop_lon,
                schedule.stop_lat,
                schedule.next_stop_lon,
                schedule.next_stop_lat,
                strict=True,
            )
        ]

        schedule["planned_speed_prev_kmh"] = (
            3.6
            * schedule.prev_segment_m
            / schedule.planned_gap_prev_s.replace(0, np.nan)
        )
        schedule["planned_speed_next_kmh"] = (
            3.6
            * schedule.next_segment_m
            / schedule.planned_gap_next_s.replace(0, np.nan)
        )

        schedule["stop_visit_count"] = schedule.groupby("stop_key").stop_key.transform(
            "size"
        )

        stop_time = schedule[
            ["stop_visit_id", "stop_key", "time_begin"]
        ].sort_values(["stop_key", "time_begin"])
        by_stop = stop_time.groupby("stop_key", sort=False)
        stop_time["stop_headway_prev_s"] = (
            by_stop.time_begin.diff().dt.total_seconds()
        )
        stop_time["stop_headway_next_s"] = (
            by_stop.time_begin.shift(-1) - stop_time.time_begin
        ).dt.total_seconds()

        return schedule.merge(
            stop_time[
                [
                    "stop_visit_id",
                    "stop_headway_prev_s",
                    "stop_headway_next_s",
                ]
            ],
            on="stop_visit_id",
            how="left",
        )

    @staticmethod
    def _prepare_telemetry(batch: PredictionBatch) -> pd.DataFrame:
        rows = [
            {
                "tr_id": event.tr_id,
                "event_time": pd.Timestamp(event.event_time),
                "receive_time": pd.Timestamp(event.receive_time),
                "lon": event.lon,
                "lat": event.lat,
                "speed": event.speed_kmh,
                "heading": event.heading_deg,
                "location_valid": event.location_valid,
            }
            for event in batch.telemetry
        ]

        columns = [
            "tr_id",
            "event_time",
            "receive_time",
            "lon",
            "lat",
            "speed",
            "heading",
            "location_valid",
        ]
        traffic = pd.DataFrame(rows, columns=columns)
        if traffic.empty:
            return traffic

        traffic["available_time"] = traffic[
            ["event_time", "receive_time"]
        ].max(axis=1)
        traffic["receive_lag_s"] = (
            traffic.receive_time - traffic.event_time
        ).dt.total_seconds()
        traffic["speed_outlier"] = traffic.speed > SPEED_CAP_KMH
        traffic["speed_clean"] = traffic.speed.clip(0, SPEED_CAP_KMH)
        traffic["valid_coords"] = (
            traffic.location_valid
            & traffic.lon.notna()
            & traffic.lat.notna()
        )

        return traffic.sort_values(
            ["tr_id", "available_time"]
        ).reset_index(drop=True)

    def build_row(self, target: PredictionTarget) -> dict[str, object]:
        row = self._base(target)
        self._add_latest_state(row, target)
        self._add_physics(row)
        self._add_schedule_path(row, target)
        self._add_cross_features(row)
        self._add_powers(row)
        return row

    def build(self, target: PredictionTarget, expected_features: list[str]) -> pd.DataFrame:
        row = self.build_row(target)

        frame = pd.DataFrame([row])
        for feature in expected_features:
            if feature not in frame.columns:
                frame[feature] = pd.NA if feature in {
                    "target_grid_4",
                    "target_grid_3",
                    "target_grid_2",
                    "current_grid_4",
                    "current_grid_3",
                    "current_grid_2",
                } else np.nan

        return frame[expected_features]

    def _target_schedule_row(self, target: PredictionTarget) -> pd.Series | None:
        if self.schedule.empty:
            return None
        matches = self.schedule.loc[
            self.schedule.stop_visit_id.astype(str) == str(target.target_stop_id)
        ]
        return None if matches.empty else matches.iloc[0]

    def _base(self, target: PredictionTarget) -> dict[str, object]:
        target_time = pd.Timestamp(target.target_time_begin)
        horizon_s = (target_time - self.as_of).total_seconds()
        horizon_min = horizon_s / 60

        minute_of_day = (
            self.as_of.hour * 60
            + self.as_of.minute
            + self.as_of.second / 60
            + self.as_of.microsecond / 60_000_000
        )
        target_minute_of_day = (
            target_time.hour * 60
            + target_time.minute
            + target_time.second / 60
            + target_time.microsecond / 60_000_000
        )

        row: dict[str, object] = {
            "horizon_s": horizon_s,
            "horizon_min": horizon_min,
            "minute_of_day": minute_of_day,
            "target_minute_of_day": target_minute_of_day,
            "time_sin": math.sin(2 * math.pi * minute_of_day / 1440),
            "time_cos": math.cos(2 * math.pi * minute_of_day / 1440),
            "target_time_sin": math.sin(
                2 * math.pi * target_minute_of_day / 1440
            ),
            "target_time_cos": math.cos(
                2 * math.pi * target_minute_of_day / 1440
            ),
            "hour": self.as_of.hour,
            "target_hour": target_time.hour,
            "cur_dev_s": target.cur_dev_s,
        }

        schedule_row = self._target_schedule_row(target)
        schedule_fields = [
            "stop_lon",
            "stop_lat",
            "schedule_pos",
            "schedule_rows",
            "schedule_progress",
            "planned_gap_prev_s",
            "planned_gap_next_s",
            "prev_segment_m",
            "next_segment_m",
            "planned_speed_prev_kmh",
            "planned_speed_next_kmh",
            "stop_visit_count",
            "stop_headway_prev_s",
            "stop_headway_next_s",
            "prev_stop_lon",
            "prev_stop_lat",
            "next_stop_lon",
            "next_stop_lat",
        ]
        for field in schedule_fields:
            row[field] = np.nan if schedule_row is None else schedule_row[field]

        cur_dev = target.cur_dev_s
        row["cur_dev_abs"] = np.nan if cur_dev is None else abs(cur_dev)
        row["cur_dev_sign"] = np.nan if cur_dev is None else float(np.sign(cur_dev))
        row["cur_dev_log"] = _signed_log1p(cur_dev)
        row["cur_dev_sqrt"] = (
            np.nan
            if cur_dev is None
            else float(np.sign(cur_dev) * np.sqrt(abs(cur_dev)))
        )
        row["cur_dev_sq"] = np.nan if cur_dev is None else float(cur_dev**2)
        row["cur_dev_per_min"] = _safe_div(cur_dev, horizon_min)

        row["target_grid_4"] = _grid(row["stop_lon"], row["stop_lat"], 4)
        row["target_grid_3"] = _grid(row["stop_lon"], row["stop_lat"], 3)
        row["target_grid_2"] = _grid(row["stop_lon"], row["stop_lat"], 2)

        return row

    def _add_latest_state(self, row: dict[str, object], target: PredictionTarget) -> None:
        if self.telemetry.empty:
            vehicle = self.telemetry
        else:
            vehicle = self.telemetry.loc[
                (self.telemetry.tr_id.astype(str) == str(target.tr_id))
                & (self.telemetry.available_time <= self.as_of)
            ]

        any_state = None if vehicle.empty else vehicle.iloc[-1]
        valid = vehicle.loc[vehicle.valid_coords] if not vehicle.empty else vehicle
        valid_state = None if valid.empty else valid.iloc[-1]

        row.update(
            {
                "last_lon_any": np.nan if any_state is None else any_state.lon,
                "last_lat_any": np.nan if any_state is None else any_state.lat,
                "last_speed_kmh": (
                    np.nan if any_state is None else any_state.speed_clean
                ),
                "last_heading_deg": (
                    np.nan if any_state is None else any_state.heading
                ),
                "last_location_valid": (
                    np.nan if any_state is None else int(any_state.location_valid)
                ),
                "last_receive_lag_s": (
                    np.nan if any_state is None else any_state.receive_lag_s
                ),
                "last_speed_outlier": (
                    np.nan if any_state is None else int(any_state.speed_outlier)
                ),
                "packet_age_s": (
                    np.nan
                    if any_state is None
                    else (self.as_of - any_state.available_time).total_seconds()
                ),
                "event_age_s": (
                    np.nan
                    if any_state is None
                    else (self.as_of - any_state.event_time).total_seconds()
                ),
                "receive_age_s": (
                    np.nan
                    if any_state is None
                    else (self.as_of - any_state.receive_time).total_seconds()
                ),
                "last_valid_lon": (
                    np.nan if valid_state is None else valid_state.lon
                ),
                "last_valid_lat": (
                    np.nan if valid_state is None else valid_state.lat
                ),
                "last_valid_speed_kmh": (
                    np.nan if valid_state is None else valid_state.speed_clean
                ),
                "last_valid_heading_deg": (
                    np.nan if valid_state is None else valid_state.heading
                ),
                "valid_age_s": (
                    np.nan
                    if valid_state is None
                    else (self.as_of - valid_state.available_time).total_seconds()
                ),
            }
        )

    @staticmethod
    def _add_physics(row: dict[str, object]) -> None:
        row["distance_to_target_m"] = _haversine_m(
            row["last_valid_lon"],
            row["last_valid_lat"],
            row["stop_lon"],
            row["stop_lat"],
        )
        row["bearing_to_target_deg"] = _bearing_deg(
            row["last_valid_lon"],
            row["last_valid_lat"],
            row["stop_lon"],
            row["stop_lat"],
        )
        row["heading_error_deg"] = _angle_diff_deg(
            row["last_valid_heading_deg"],
            row["bearing_to_target_deg"],
        )
        row["heading_error_abs_deg"] = (
            np.nan
            if pd.isna(row["heading_error_deg"])
            else abs(row["heading_error_deg"])
        )
        row["heading_alignment"] = (
            np.nan
            if pd.isna(row["heading_error_deg"])
            else math.cos(math.radians(row["heading_error_deg"]))
        )

        for name, value in [
            ("last_heading", row["last_valid_heading_deg"]),
            ("bearing", row["bearing_to_target_deg"]),
        ]:
            row[f"{name}_sin"] = (
                np.nan if pd.isna(value) else math.sin(math.radians(value))
            )
            row[f"{name}_cos"] = (
                np.nan if pd.isna(value) else math.cos(math.radians(value))
            )

        row["projected_speed_kmh"] = (
            np.nan
            if pd.isna(row["last_valid_speed_kmh"])
            or pd.isna(row["heading_alignment"])
            else row["last_valid_speed_kmh"] * row["heading_alignment"]
        )
        row["required_speed_kmh"] = _safe_div(
            3.6 * row["distance_to_target_m"]
            if not pd.isna(row["distance_to_target_m"])
            else np.nan,
            row["horizon_s"],
        )
        row["speed_margin_kmh"] = (
            np.nan
            if pd.isna(row["last_valid_speed_kmh"])
            or pd.isna(row["required_speed_kmh"])
            else row["last_valid_speed_kmh"] - row["required_speed_kmh"]
        )
        row["projected_speed_margin_kmh"] = (
            np.nan
            if pd.isna(row["projected_speed_kmh"])
            or pd.isna(row["required_speed_kmh"])
            else row["projected_speed_kmh"] - row["required_speed_kmh"]
        )
        row["speed_to_required_ratio"] = _safe_div(
            row["last_valid_speed_kmh"], row["required_speed_kmh"]
        )
        row["eta_last_speed_s"] = _safe_div(
            3.6 * row["distance_to_target_m"]
            if not pd.isna(row["distance_to_target_m"])
            else np.nan,
            row["last_valid_speed_kmh"],
        )
        projected_speed = row["projected_speed_kmh"]
        row["eta_projected_speed_s"] = (
            _safe_div(3.6 * row["distance_to_target_m"], projected_speed)
            if not pd.isna(projected_speed) and projected_speed > 0
            else np.nan
        )
        row["eta_margin_last_speed_s"] = (
            np.nan
            if pd.isna(row["eta_last_speed_s"])
            else row["horizon_s"] - row["eta_last_speed_s"]
        )
        row["eta_margin_projected_s"] = (
            np.nan
            if pd.isna(row["eta_projected_speed_s"])
            else row["horizon_s"] - row["eta_projected_speed_s"]
        )
        row["distance_prev_stop_m"] = _haversine_m(
            row["last_valid_lon"],
            row["last_valid_lat"],
            row["prev_stop_lon"],
            row["prev_stop_lat"],
        )
        row["distance_next_stop_m"] = _haversine_m(
            row["last_valid_lon"],
            row["last_valid_lat"],
            row["next_stop_lon"],
            row["next_stop_lat"],
        )

        row["current_grid_4"] = _grid(
            row["last_valid_lon"], row["last_valid_lat"], 4
        )
        row["current_grid_3"] = _grid(
            row["last_valid_lon"], row["last_valid_lat"], 3
        )
        row["current_grid_2"] = _grid(
            row["last_valid_lon"], row["last_valid_lat"], 2
        )

    def _add_schedule_path(
        self, row: dict[str, object], target: PredictionTarget
    ) -> None:
        if self.schedule.empty:
            future = self.schedule
        else:
            target_time = pd.Timestamp(target.target_time_begin)
            vehicle_schedule = (
                self.schedule.loc[
                    self.schedule.tr_id.astype(str) == str(target.tr_id)
                ]
                .sort_values("time_begin")
                .reset_index(drop=True)
            )
            future = vehicle_schedule.loc[
                (vehicle_schedule.time_begin > self.as_of)
                & (vehicle_schedule.time_begin <= target_time)
            ]

        row["scheduled_stops_to_target"] = len(future)
        row["first_future_stop_lon"] = np.nan
        row["first_future_stop_lat"] = np.nan
        row["scheduled_path_m"] = np.nan
        row["future_gap_mean_s"] = np.nan
        row["future_gap_max_s"] = np.nan

        if len(future):
            row["first_future_stop_lon"] = future.stop_lon.iloc[0]
            row["first_future_stop_lat"] = future.stop_lat.iloc[0]

            if len(future) > 1:
                path = [
                    _haversine_m(a, b, c, d)
                    for a, b, c, d in zip(
                        future.stop_lon.iloc[:-1],
                        future.stop_lat.iloc[:-1],
                        future.stop_lon.iloc[1:],
                        future.stop_lat.iloc[1:],
                        strict=True,
                    )
                ]
                row["scheduled_path_m"] = (
                    np.nan if any(pd.isna(value) for value in path) else float(sum(path))
                )
            else:
                row["scheduled_path_m"] = 0.0

            gaps = future.time_begin.diff().dt.total_seconds().dropna()
            if len(gaps):
                row["future_gap_mean_s"] = float(gaps.mean())
                row["future_gap_max_s"] = float(gaps.max())

        row["distance_to_first_future_stop_m"] = _haversine_m(
            row["last_valid_lon"],
            row["last_valid_lat"],
            row["first_future_stop_lon"],
            row["first_future_stop_lat"],
        )
        row["route_proxy_m"] = (
            np.nan
            if pd.isna(row["distance_to_first_future_stop_m"])
            or pd.isna(row["scheduled_path_m"])
            else row["distance_to_first_future_stop_m"] + row["scheduled_path_m"]
        )
        row["route_proxy_required_speed_kmh"] = _safe_div(
            3.6 * row["route_proxy_m"]
            if not pd.isna(row["route_proxy_m"])
            else np.nan,
            row["horizon_s"],
        )
        row["straight_to_route_ratio"] = _safe_div(
            row["distance_to_target_m"], row["route_proxy_m"]
        )

    @staticmethod
    def _add_cross_features(row: dict[str, object]) -> None:
        valid_age = row["valid_age_s"]
        packet_age = row["packet_age_s"]
        row["valid_age_gt_30s"] = int(not pd.isna(valid_age) and valid_age > 30)
        row["valid_age_gt_90s"] = int(not pd.isna(valid_age) and valid_age > 90)
        row["valid_age_gt_5m"] = int(not pd.isna(valid_age) and valid_age > 300)
        row["packet_age_gt_30s"] = int(
            not pd.isna(packet_age) and packet_age > 30
        )

        cur_dev = row["cur_dev_s"]
        row["cur_dev_x_horizon"] = (
            np.nan if pd.isna(cur_dev) else cur_dev * row["horizon_min"]
        )
        row["cur_dev_x_required_speed"] = (
            np.nan
            if pd.isna(cur_dev) or pd.isna(row["required_speed_kmh"])
            else cur_dev * row["required_speed_kmh"]
        )
        row["cur_dev_x_distance_km"] = (
            np.nan
            if pd.isna(cur_dev) or pd.isna(row["distance_to_target_m"])
            else cur_dev * row["distance_to_target_m"] / 1000
        )
        row["cur_dev_per_stop"] = _safe_div(
            cur_dev, row["scheduled_stops_to_target"]
        )
        row["cur_dev_per_km"] = _safe_div(
            cur_dev,
            row["route_proxy_m"] / 1000
            if not pd.isna(row["route_proxy_m"])
            else np.nan,
        )
        row["planned_seconds_per_stop"] = _safe_div(
            row["horizon_s"], row["scheduled_stops_to_target"]
        )
        row["route_m_per_stop"] = _safe_div(
            row["route_proxy_m"], row["scheduled_stops_to_target"]
        )
        row["scheduled_stops_per_min"] = _safe_div(
            row["scheduled_stops_to_target"], row["horizon_min"]
        )
        row["cur_dev_plus_eta_margin"] = (
            np.nan
            if pd.isna(cur_dev) or pd.isna(row["eta_margin_last_speed_s"])
            else cur_dev + row["eta_margin_last_speed_s"]
        )
        row["planned_vs_straight_distance_m"] = (
            np.nan
            if pd.isna(row["route_proxy_m"])
            or pd.isna(row["distance_to_target_m"])
            else row["route_proxy_m"] - row["distance_to_target_m"]
        )
        row["route_straightness"] = _safe_div(
            row["distance_to_target_m"], row["route_proxy_m"]
        )
        row["cur_dev_x_hour_sin"] = (
            np.nan if pd.isna(cur_dev) else cur_dev * row["target_time_sin"]
        )
        row["cur_dev_x_hour_cos"] = (
            np.nan if pd.isna(cur_dev) else cur_dev * row["target_time_cos"]
        )
        row["distance_x_valid_age"] = (
            np.nan
            if pd.isna(row["distance_to_target_m"])
            or pd.isna(row["valid_age_s"])
            else row["distance_to_target_m"] * row["valid_age_s"]
        )

        for digits in [4, 3, 2]:
            row[f"same_grid_{digits}"] = int(
                not pd.isna(row[f"current_grid_{digits}"])
                and not pd.isna(row[f"target_grid_{digits}"])
                and row[f"current_grid_{digits}"] == row[f"target_grid_{digits}"]
            )

    @staticmethod
    def _add_powers(row: dict[str, object]) -> None:
        for feature in [
            "distance_to_target_m",
            "required_speed_kmh",
            "valid_age_s",
            "packet_age_s",
            "route_proxy_m",
        ]:
            value = row[feature]
            row[f"{feature}_sqrt"] = (
                np.nan if pd.isna(value) else math.sqrt(max(float(value), 0))
            )

        for feature in [
            "last_valid_speed_kmh",
            "required_speed_kmh",
        ]:
            value = row[feature]
            row[f"{feature}_sq"] = (
                np.nan if pd.isna(value) else float(value) ** 2
            )
