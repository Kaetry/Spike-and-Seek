"""Spike-and-Seek: public-facing synchronization calculator.

The main-window layout is defined in GUI_GRID below.
Sync-value generation and source-to-target timestamp conversion use the
user-selected session directory. 
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import tkinter as tk
import wave
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

import pandas as pd


SYNC_VALUES_FILENAME = "sync_values.csv"
VIDEO_DURATIONS_FILENAME = "video_durations.csv"
AUDIO_EXTENSIONS = {".wav", ".flac"}
VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".ogv", ".ts", ".vob", ".webm", ".wmv",
}
# Windows virtual-key codes remain the same when the active keyboard layout changes.
WINDOWS_CLIPBOARD_KEYCODES = {
    0x43: "<<Copy>>",
    0x56: "<<Paste>>",
}


@dataclass(frozen=True)
class StreamLandmark:
    datastream_name: str
    file_index: int
    event_landmark_time: float
    event_landmark_time_abs: float
    wav_path: Path


class SyncGenerationError(ValueError):
    """Raised when the TextGrid or session media cannot produce sync values."""


class TimestampCalculationError(ValueError):
    """Raised when source/target timestamp inputs or data are invalid."""


def _target_timestamp_range_error(
    timestamp: float, datastream_name: str, total_duration: float
) -> str:
    return (
        f"The calculated target timestamp ({timestamp:.0f}s) is outside the "
        f"valid range for target datastream '{datastream_name}' "
        f"(0-{total_duration:.0f}s)."
    )


# Main-window layout: element -> grid row, column and columnspan.
# Rows follow the workflow top to bottom; clearing the window from an element
# destroys every widget on that element's row and below.
GUI_GRID: dict[str, dict[str, int]] = {
    element: {"row": row, "column": column, "columnspan": columnspan}
    for element, (row, column, columnspan) in {
        "data_dir_label": (0, 0, 1),
        "data_dir_button": (0, 1, 1),
        "data_dir_subframe": (1, 0, 2),
        "filename_label": (2, 0, 2),
        "filename_entry": (3, 0, 1),
        "filename_subframe": (4, 0, 2),
        "target_selection_subframe": (5, 0, 2),
        "input_timestamp_label": (6, 0, 2),
        "input_timestamp_field": (7, 0, 1),
        "input_timestamp_button": (7, 1, 1),
        "input_timestamp_error": (9, 0, 2),
        "result_label": (9, 0, 2),
        "result_field_default": (10, 0, 2),
        "result_C_N_label": (10, 0, 1),
        "result_C_N_field": (10, 1, 1),
        "result_C_time_label": (11, 0, 1),
        "result_C_time_field": (11, 1, 1),
        "open_vlc_button": (12, 1, 1),
    }.items()
}


# Shared button style and grid options: every button fills its grid cell and uses
# the same inner padding, font and outer spacing. The VLC toggle is a tk.Button
# because themed ttk buttons on Windows cannot be recoloured.
BUTTON_PADDING = (10, 5)
BUTTON_FONT = ("Helvetica", 10)
BUTTON_GRID: dict[str, Any] = {"sticky": "ew", "padx": 8, "pady": 5}


_MEDIA_FILE_CACHE: dict[Path, list[Path]] = {}


def clear_media_file_cache() -> None:
    """Force the next list_media_files call to rescan the disk."""
    _MEDIA_FILE_CACHE.clear()


def list_media_files(directory: Path) -> list[Path]:
    root = directory.resolve()
    if root in _MEDIA_FILE_CACHE:
        return list(_MEDIA_FILE_CACHE[root])
    media_files = []
    for candidate in directory.rglob("*"):
        extension = candidate.suffix.lower()
        if (
            not candidate.is_file()
            or (
                extension not in AUDIO_EXTENSIONS
                and extension not in VIDEO_EXTENSIONS
            )
        ):
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        media_files.append(resolved)
    media_files.sort(key=lambda path: str(path).casefold())
    _MEDIA_FILE_CACHE[root] = media_files
    return list(media_files)


def list_video_files(directory: Path) -> list[Path]:
    return [
        path for path in list_media_files(directory)
        if path.suffix.lower() in VIDEO_EXTENSIONS
    ]


def find_video_wav_counterpart(
    video_path: Path, media_files: list[Path] | None = None
) -> Path | None:
    # With a scanned media list, no extra directory listing is needed.
    siblings = (
        [path for path in media_files if path.parent == video_path.parent]
        if media_files is not None
        else [path for path in video_path.parent.iterdir() if path.is_file()]
    )
    wav_files = [
        path for path in siblings
        if path.suffix.casefold() == ".wav"
        and path.stem.casefold() == video_path.stem.casefold()
    ]
    if len(wav_files) > 1:
        raise SyncGenerationError(
            f"More than one WAV file matches video '{video_path.name}'."
        )
    return wav_files[0] if wav_files else None


def missing_video_wavs(directory: Path) -> list[Path]:
    media_files = list_media_files(directory)
    return [
        video_path
        for video_path in media_files
        if video_path.suffix.lower() in VIDEO_EXTENSIONS
        and find_video_wav_counterpart(video_path, media_files) is None
    ]


def audio_duration_seconds(audio_path: Path) -> float:
    """Read a duration from the file header only, via a Praat LongSound."""
    try:
        import parselmouth as pm
    except ImportError as error:
        raise SyncGenerationError(
            f"Reading the duration of '{audio_path.name}' requires "
            "praat-parselmouth."
        ) from error
    try:
        long_sound = pm.praat.call("Open long sound file", str(audio_path))
        return float(pm.praat.call(long_sound, "Get total duration"))
    except (OSError, ValueError, pm.PraatError) as error:
        raise SyncGenerationError(
            f"Could not read audio duration for '{audio_path.name}': {error}"
        ) from error


def wav_duration_seconds(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            if frame_rate <= 0:
                raise SyncGenerationError(
                    f"WAV file has an invalid sample rate: {wav_path.name}"
                )
            duration = wav_file.getnframes() / frame_rate
    except (EOFError, wave.Error):
        # The wave module rejects e.g. 32-bit float and RF64 files that
        # Praat reads fine.
        duration = audio_duration_seconds(wav_path)
    except OSError as error:
        raise SyncGenerationError(
            f"Could not read WAV file duration for '{wav_path.name}': {error}"
        ) from error

    if duration <= 0:
        raise SyncGenerationError(f"WAV file is empty: {wav_path.name}")
    return float(duration)


def video_duration_seconds(video_path: Path) -> float:
    try:
        from moviepy.video.io.VideoFileClip import VideoFileClip
    except (ImportError, OSError) as error:
        raise SyncGenerationError(
            f"Reading video duration for '{video_path.name}' requires MoviePy."
        ) from error

    try:
        video = VideoFileClip(str(video_path))
        try:
            duration = float(video.duration)
        finally:
            video.close()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SyncGenerationError(
            f"Could not read video duration for '{video_path.name}': {error}"
        ) from error

    if not math.isfinite(duration) or duration <= 0:
        raise SyncGenerationError(
            f"Video has an invalid duration: {video_path.name}"
        )
    return duration


def stream_name_and_index(file_stem: str) -> tuple[str, int]:
    indexed = re.fullmatch(r"(.+)_([0-9]+)", file_stem)
    if indexed:
        return indexed.group(1), int(indexed.group(2))
    return file_stem, 1


def resolve_stream_names_and_indexes(
    file_paths: list[Path],
) -> dict[Path, tuple[str, int]]:
    parsed = {
        path: stream_name_and_index(path.stem)
        for path in file_paths
    }
    stems_by_stream: dict[str, set[str]] = {}
    for path, (stream_name, _) in parsed.items():
        stems_by_stream.setdefault(stream_name.casefold(), set()).add(
            path.stem.casefold()
        )

    resolved = {}
    for path, (stream_name, file_index) in parsed.items():
        if (
            file_index != 1
            and len(stems_by_stream[stream_name.casefold()]) == 1
        ):
            resolved[path] = (path.stem, 1)
        else:
            resolved[path] = (stream_name, file_index)
    return resolved


def create_video_duration_table(directory: Path) -> pd.DataFrame:
    root = directory.resolve()
    video_files = list_video_files(root)
    video_streams = resolve_stream_names_and_indexes(video_files)
    rows = []

    for video_path in video_files:
        try:
            relative_video_path = video_path.relative_to(root).as_posix()
        except ValueError as error:
            raise SyncGenerationError(
                f"Video is outside the selected directory: {video_path}"
            ) from error

        datastream_name, file_index = video_streams[video_path]
        rows.append(
            {
                "video_file": relative_video_path,
                "datastream_name": datastream_name,
                "file_index": file_index,
                "duration_sec": video_duration_seconds(video_path),
            }
        )

    return pd.DataFrame(
        rows,
        columns=["video_file", "datastream_name", "file_index", "duration_sec"],
    )


def load_optional_sync_streams(directory: Path) -> tuple[list[str], str | None]:
    sync_path = directory / SYNC_VALUES_FILENAME
    if not sync_path.is_file():
        return [], None

    sync_df = pd.read_csv(sync_path)
    stream_column = (
        "datastream_name"
        if "datastream_name" in sync_df.columns
        else "target_stream"
    )
    if stream_column not in sync_df.columns:
        return [], (
            f"{SYNC_VALUES_FILENAME} has neither a 'datastream_name' nor a "
            "'target_stream' column. Dynamic stream matching is unavailable."
        )

    stream_names = (
        sync_df[stream_column]
        .dropna()
        .astype(str)
        .str.strip()
    )
    return sorted(set(name for name in stream_names if name), key=str.casefold), None


def read_sync_landmarks(
    textgrid_path: Path,
    directory: Path,
    *,
    exclude_video_streams_missing_wavs: bool = False,
) -> dict[str, StreamLandmark]:
    try:
        import parselmouth as pm
        import tgt
    except ImportError as error:
        raise SyncGenerationError(
            "TextGrid synchronization requires tgt and praat-parselmouth."
        ) from error

    try:
        textgrid = tgt.read_textgrid(str(textgrid_path))
        sync_tier = textgrid.get_tier_by_name("sync")
    except (OSError, ValueError, KeyError) as error:
        raise SyncGenerationError(
            f"Could not read the TextGrid 'sync' tier: {error}"
        ) from error

    points = getattr(sync_tier, "points", None)
    intervals = getattr(sync_tier, "intervals", None)
    if points is not None:
        sync_entries = points
    elif intervals is not None:
        sync_entries = intervals
    else:
        raise SyncGenerationError(
            "The TextGrid 'sync' tier is not a point or interval tier."
        )

    excluded_stream_names: set[str] = set()
    excluded_video_stems: set[str] = set()
    if exclude_video_streams_missing_wavs:
        video_files = list_video_files(directory)
        video_streams = resolve_stream_names_and_indexes(video_files)
        missing_video_files = missing_video_wavs(directory)
        excluded_stream_names = {
            video_streams[video_path][0].casefold()
            for video_path in missing_video_files
        }
        excluded_video_stems = {
            video_path.stem.casefold()
            for video_path in video_files
            if video_streams[video_path][0].casefold() in excluded_stream_names
        }

    available_wav_files = [
        path for path in list_media_files(directory)
        if path.suffix.lower() == ".wav"
        and path.stem.casefold() not in excluded_video_stems
    ]
    wav_by_stem: dict[str, list[Path]] = {}
    for wav_path in available_wav_files:
        wav_by_stem.setdefault(wav_path.stem.casefold(), []).append(wav_path)

    matched_entries = []
    for entry in sync_entries:
        label = str(entry.text).strip()
        if not label or label.casefold() in excluded_video_stems:
            continue
        candidates = wav_by_stem.get(label.casefold(), [])
        if not candidates:
            raise SyncGenerationError(
                f"TextGrid sync label '{label}' does not match a WAV filename stem."
            )
        if len(candidates) != 1:
            raise SyncGenerationError(
                f"TextGrid sync label '{label}' matches multiple WAV files."
            )
        matched_entries.append((entry, label, candidates[0]))

    referenced_stream_bases = {
        stream_name_and_index(wav_path.stem)[0].casefold()
        for _, _, wav_path in matched_entries
    }
    wav_files = [
        path for path in available_wav_files
        if stream_name_and_index(path.stem)[0].casefold()
        in referenced_stream_bases
    ]
    wav_streams = resolve_stream_names_and_indexes(wav_files)
    # Use one spelling per stream so 'Cam_1' and 'cam_2' group together.
    canonical_names: dict[str, str] = {}
    wav_streams = {
        path: (canonical_names.setdefault(name.casefold(), name), index)
        for path, (name, index) in wav_streams.items()
    }
    required_indexes: dict[str, int] = {}
    for _, _, wav_path in matched_entries:
        datastream_name, file_index = wav_streams[wav_path]
        stream_key = datastream_name.casefold()
        required_indexes[stream_key] = max(
            required_indexes.get(stream_key, 0), file_index
        )

    stream_files: dict[str, dict[int, Path]] = {}
    for wav_path in wav_files:
        datastream_name, file_index = wav_streams[wav_path]
        if file_index > required_indexes[datastream_name.casefold()]:
            continue
        indexed_files = stream_files.setdefault(datastream_name, {})
        if file_index in indexed_files:
            raise SyncGenerationError(
                f"Multiple WAV files identify stream '{datastream_name}' "
                f"file index {file_index}."
            )
        indexed_files[file_index] = wav_path

    durations: dict[Path, float] = {}
    for files in stream_files.values():
        file_indexes = sorted(files)
        expected_indexes = list(range(1, file_indexes[-1] + 1))
        if file_indexes != expected_indexes:
            raise SyncGenerationError(
                f"WAV file indices must be consecutive from 1; found {file_indexes}."
            )
        for wav_path in files.values():
            durations[wav_path] = wav_duration_seconds(wav_path)

    landmarks: dict[str, StreamLandmark] = {}
    for entry, label, wav_path in matched_entries:
        datastream_name, file_index = wav_streams[wav_path]
        if datastream_name in landmarks:
            raise SyncGenerationError(
                f"The sync tier has more than one event interval for "
                f"datastream '{datastream_name}'."
            )

        if points is not None:
            local_time = float(entry.time)
            if local_time < 0 or local_time > durations[wav_path]:
                raise SyncGenerationError(
                    f"TextGrid point '{label}' has an invalid time "
                    f"({local_time:.3f}s) for {wav_path.name} "
                    f"({durations[wav_path]:.3f}s)."
                )
        else:
            start = float(entry.start_time)
            end = float(entry.end_time)
            local_time = (start + end) / 2.0
            if start < 0 or end <= start or end > durations[wav_path]:
                raise SyncGenerationError(
                    f"TextGrid interval '{label}' has invalid times "
                    f"({start:.3f}-{end:.3f}s) for {wav_path.name} "
                    f"({durations[wav_path]:.3f}s)."
                )

        prior_duration = sum(
            durations[prior_path]
            for prior_index, prior_path in stream_files[datastream_name].items()
            if prior_index < file_index
        )
        landmarks[datastream_name] = StreamLandmark(
            datastream_name=datastream_name,
            file_index=file_index,
            event_landmark_time=local_time,
            event_landmark_time_abs=local_time + prior_duration,
            wav_path=wav_path,
        )

    if len(landmarks) < 2:
        raise SyncGenerationError(
            "The TextGrid sync tier must annotate the same event in at least "
            "two datastreams."
        )
    return landmarks


def build_sync_value_rows(
    landmarks: dict[str, StreamLandmark],
    get_cc_lag: Callable[[StreamLandmark, StreamLandmark], float],
) -> list[dict[str, object]]:
    datastream_names = sorted(landmarks, key=str.casefold)
    rows = []
    pair_lags: dict[tuple[str, str], float] = {}
    for datastream_name in datastream_names:
        event = landmarks[datastream_name]
        row: dict[str, object] = {
            "datastream_name": datastream_name,
            "event_landmark_time": event.event_landmark_time,
            "event_landmark_time_abs": event.event_landmark_time_abs,
        }
        for other_name in datastream_names:
            if other_name == datastream_name:
                row[f"cc_lag_{other_name}"] = None
                row[f"sync_value_{other_name}"] = None
                continue
            other_event = landmarks[other_name]
            # lag(B, A) == -lag(A, B): correlate each pair only once.
            if (other_name, datastream_name) in pair_lags:
                cc_lag = -pair_lags[(other_name, datastream_name)]
            else:
                cc_lag = float(get_cc_lag(event, other_event))
                pair_lags[(datastream_name, other_name)] = cc_lag
            row[f"cc_lag_{other_name}"] = cc_lag
            # Praat's lag is (event position in other) - (event position in
            # this stream) relative to the landmarks, so it is subtracted.
            row[f"sync_value_{other_name}"] = (
                event.event_landmark_time_abs
                - other_event.event_landmark_time_abs
                - cc_lag
            )
        rows.append(row)
    return rows


def parse_video_timecode(value: str) -> float:
    match = re.fullmatch(
        r"([0-9]+):([0-5][0-9]):([0-5][0-9](?:\.[0-9]+)?)",
        value.strip(),
    )
    if match is None:
        raise TimestampCalculationError(
            "Enter a video time as HH:MM:SS or HH:MM:SS.sss."
        )
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_video_timecode(seconds: float) -> str:
    total_milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    remaining_seconds = remainder / 1000
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:06.3f}"


def read_video_stream_durations(
    directory: Path, datastream_name: str
) -> dict[int, float]:
    duration_path = directory / VIDEO_DURATIONS_FILENAME
    if not duration_path.is_file():
        raise TimestampCalculationError(
            f"{VIDEO_DURATIONS_FILENAME} is required for video timestamp conversion."
        )

    try:
        durations_df = pd.read_csv(duration_path)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as error:
        raise TimestampCalculationError(
            f"Could not read {VIDEO_DURATIONS_FILENAME}: {error}"
        ) from error

    required_columns = {"datastream_name", "file_index", "duration_sec"}
    missing_columns = required_columns.difference(durations_df.columns)
    if missing_columns:
        raise TimestampCalculationError(
            f"{VIDEO_DURATIONS_FILENAME} is missing required columns: "
            f"{', '.join(sorted(missing_columns))}."
        )

    stream_rows = durations_df.loc[
        durations_df["datastream_name"]
        .astype(str)
        .str.strip()
        .str.casefold()
        == datastream_name.casefold()
    ]
    if stream_rows.empty:
        raise TimestampCalculationError(
            f"No video durations were found for datastream '{datastream_name}'."
        )

    durations: dict[int, float] = {}
    for _, row in stream_rows.iterrows():
        raw_index = pd.to_numeric(row["file_index"], errors="coerce")
        raw_duration = pd.to_numeric(row["duration_sec"], errors="coerce")
        if (
            pd.isna(raw_index)
            or not math.isfinite(float(raw_index))
            or int(raw_index) != float(raw_index)
            or int(raw_index) < 1
            or pd.isna(raw_duration)
            or not math.isfinite(float(raw_duration))
            or float(raw_duration) <= 0
        ):
            raise TimestampCalculationError(
                f"{VIDEO_DURATIONS_FILENAME} contains an invalid duration row "
                f"for datastream '{datastream_name}'."
            )
        file_index = int(raw_index)
        if file_index in durations:
            raise TimestampCalculationError(
                f"{VIDEO_DURATIONS_FILENAME} contains multiple rows for "
                f"'{datastream_name}' file index {file_index}."
            )
        durations[file_index] = float(raw_duration)

    indexes = sorted(durations)
    if indexes != list(range(1, indexes[-1] + 1)):
        raise TimestampCalculationError(
            f"Video file indexes for '{datastream_name}' must be consecutive "
            f"from 1; found {indexes}."
        )
    return durations


def read_audio_stream_durations(
    directory: Path, datastream_name: str
) -> dict[int, float]:
    audio_files = [
        path for path in list_media_files(directory)
        if path.suffix.lower() in AUDIO_EXTENSIONS
    ]
    audio_streams = resolve_stream_names_and_indexes(audio_files)
    stream_files = [
        (path, file_index)
        for path, (stream_name, file_index) in audio_streams.items()
        if stream_name.casefold() == datastream_name.casefold()
    ]
    if not stream_files:
        raise TimestampCalculationError(
            f"No audio files were found for datastream '{datastream_name}'."
        )

    durations: dict[int, float] = {}
    for audio_path, file_index in stream_files:
        if file_index in durations:
            raise TimestampCalculationError(
                f"Multiple audio files identify datastream '{datastream_name}' "
                f"file index {file_index}."
            )
        read_duration = (
            wav_duration_seconds
            if audio_path.suffix.lower() == ".wav"
            else audio_duration_seconds
        )
        try:
            duration = read_duration(audio_path)
        except SyncGenerationError as error:
            raise TimestampCalculationError(str(error)) from error

        if not math.isfinite(duration) or duration <= 0:
            raise TimestampCalculationError(
                f"Audio file has an invalid duration: {audio_path.name}."
            )
        durations[file_index] = duration

    indexes = sorted(durations)
    if indexes != list(range(1, indexes[-1] + 1)):
        raise TimestampCalculationError(
            f"Audio file indexes for '{datastream_name}' must be consecutive "
            f"from 1; found {indexes}."
        )
    return durations


def validate_audio_timestamp(
    timestamp: float, durations: dict[int, float], datastream_name: str
) -> None:
    total_duration = sum(durations.values())
    if (
        not math.isfinite(timestamp)
        or timestamp < 0
        or timestamp > total_duration
    ):
        raise TimestampCalculationError(
            _target_timestamp_range_error(
                timestamp, datastream_name, total_duration
            )
        )


def convert_source_timestamp(
    sync_df: pd.DataFrame,
    source_stream: str,
    target_stream: str,
    source_timestamp: float,
    target_duration: float | None = None,
) -> float:
    if source_stream.casefold() == target_stream.casefold():
        return source_timestamp

    sync_column = f"sync_value_{target_stream}"
    if "datastream_name" not in sync_df.columns:
        raise TimestampCalculationError(
            "Timestamp conversion requires pairwise sync values with a "
            "'datastream_name' column."
        )
    if sync_column not in sync_df.columns:
        raise TimestampCalculationError(
            f"No pairwise sync value was found from '{source_stream}' to "
            f"'{target_stream}'."
        )

    source_rows = sync_df.loc[
        sync_df["datastream_name"]
        .astype(str)
        .str.strip()
        .str.casefold()
        == source_stream.casefold()
    ]
    if len(source_rows) != 1:
        raise TimestampCalculationError(
            f"Expected one sync-values row for source datastream "
            f"'{source_stream}', found {len(source_rows)}."
        )

    raw_sync_value = pd.to_numeric(source_rows.iloc[0][sync_column], errors="coerce")
    if pd.isna(raw_sync_value) or not math.isfinite(float(raw_sync_value)):
        raise TimestampCalculationError(
            f"The sync value from '{source_stream}' to '{target_stream}' is empty "
            "or invalid."
        )

    target_timestamp = source_timestamp - float(raw_sync_value)
    if (
        not math.isfinite(target_timestamp)
        or target_timestamp < 0
        or (
            target_duration is not None
            and target_timestamp > target_duration
        )
    ):
        if target_duration is not None:
            raise TimestampCalculationError(
                _target_timestamp_range_error(
                    target_timestamp, target_stream, target_duration
                )
            )
        raise TimestampCalculationError(
            "The calculated target timestamp is negative or invalid. Check the "
            "input and selected datastreams."
        )
    return target_timestamp


def locate_video_timestamp(
    timestamp: float, durations: dict[int, float], datastream_name: str
) -> tuple[int, float]:
    elapsed_before_file = 0.0
    ordered_indexes = sorted(durations)
    total_duration = sum(durations.values())
    validate_audio_timestamp(timestamp, durations, datastream_name)

    for position, file_index in enumerate(ordered_indexes):
        duration = durations[file_index]
        file_end = elapsed_before_file + duration
        if timestamp < file_end or (
            position == len(ordered_indexes) - 1 and timestamp <= file_end
        ):
            return file_index, timestamp - elapsed_before_file
        elapsed_before_file = file_end

    raise TimestampCalculationError(
        _target_timestamp_range_error(timestamp, datastream_name, total_duration)
    )


def resolve_target_video_path(
    directory: Path, datastream_name: str, file_index: int
) -> Path:
    video_streams = resolve_stream_names_and_indexes(list_video_files(directory))
    matches = [
        path
        for path, (stream_name, index) in video_streams.items()
        if stream_name.casefold() == datastream_name.casefold()
        and index == file_index
    ]
    if not matches:
        raise TimestampCalculationError(
            f"No video file was found for datastream '{datastream_name}', "
            f"file index {file_index}."
        )
    if len(matches) > 1:
        raise TimestampCalculationError(
            f"Multiple video files match datastream '{datastream_name}', "
            f"file index {file_index}; playback would be ambiguous."
        )
    return matches[0]


def cross_correlation_lag(
    first: StreamLandmark,
    second: StreamLandmark,
    sound_cache: dict[Path, Any] | None = None,
) -> float:
    try:
        import numpy as np
        import parselmouth as pm
    except ImportError as error:
        raise SyncGenerationError(
            "Cross-correlation requires numpy and praat-parselmouth."
        ) from error

    if sound_cache is None:
        sound_cache = {}
    def segment_window(sounds_and_landmarks) -> tuple[float, float]:
        # Both segments must keep the landmark at the same offset from the
        # segment start; otherwise the offset difference leaks into the lag.
        room_before = min(landmark for _, landmark in sounds_and_landmarks)
        room_after = min(
            float(pm.praat.call(sound, "Get total duration")) - landmark
            for sound, landmark in sounds_and_landmarks
        )
        # Centre the 20 s window on the landmark where possible. Near a file
        # start or end, keep as much audio on the short side as both files
        # allow and extend the other side to make up the 20 s.
        before = max(min(10.0, room_before), 0.0)
        after = max(min(10.0, room_after), 0.0)
        after = max(min(20.0 - before, room_after), 0.0)
        before = max(min(20.0 - after, room_before), 0.0)
        if before + after <= 0:
            raise SyncGenerationError(
                "Cannot extract a correlation segment around the landmarks."
            )
        return before, after

    def cut_segment(sound, landmark_time: float, before: float, after: float):
        # A LongSound reads only the requested part from disk.
        return pm.praat.call(
            sound,
            "Extract part",
            landmark_time - before,
            landmark_time + after,
            "no",
        ).convert_to_mono()

    try:
        for wav_path in (first.wav_path, second.wav_path):
            if wav_path not in sound_cache:
                sound_cache[wav_path] = pm.praat.call(
                    "Open long sound file", str(wav_path)
                )
        sound_a = sound_cache[first.wav_path]
        sound_b = sound_cache[second.wav_path]
        before, after = segment_window(
            [
                (sound_a, first.event_landmark_time),
                (sound_b, second.event_landmark_time),
            ]
        )
        segment_a = cut_segment(
            sound_a, first.event_landmark_time, before, after
        )
        segment_b = cut_segment(
            sound_b, second.event_landmark_time, before, after
        )
        if segment_a.sampling_frequency != segment_b.sampling_frequency:
            target_fs = min(segment_a.sampling_frequency, segment_b.sampling_frequency)
            segment_a = segment_a.resample(target_fs, 75)
            segment_b = segment_b.resample(target_fs, 75)

        correlation = segment_a.cross_correlate(segment_b)
        correlation.values = np.abs(correlation.values)
        return float(pm.praat.call(
            correlation, "Get time of maximum", 0, 0, "parabolic"
        ))
    except (OSError, ValueError, pm.PraatError) as error:
        raise SyncGenerationError(
            f"Cross-correlation failed for '{first.datastream_name}' and "
            f"'{second.datastream_name}': {error}"
        ) from error


def create_sync_values(
    textgrid_path: Path,
    directory: Path,
    *,
    exclude_video_streams_missing_wavs: bool = False,
) -> pd.DataFrame:
    landmarks = read_sync_landmarks(
        textgrid_path,
        directory,
        exclude_video_streams_missing_wavs=exclude_video_streams_missing_wavs,
    )
    sound_cache: dict[Path, Any] = {}
    rows = build_sync_value_rows(
        landmarks,
        lambda first, second: cross_correlation_lag(
            first, second, sound_cache
        ),
    )
    return pd.DataFrame(rows)


class SpikeAndSeek:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.data_directory: Path | None = None
        self.stream_names: list[str] = []
        self.video_stream_names: set[str] = set()
        self.source_stream_var: tk.StringVar | None = None
        self.target_stream_var: tk.StringVar | None = None
        self.source_selector: ttk.Combobox | None = None
        self.target_selector: ttk.Combobox | None = None
        self.timestamp_entry: ttk.Entry | None = None
        self.source_file_index_entry: ttk.Entry | None = None
        self.vlc_api: Any | None = None
        self.vlc_instance: Any | None = None
        self.vlc_media: Any | None = None
        self.vlc_player: Any | None = None
        self.vlc_scale = 0.55
        self.vlc_target_video_path: Path | None = None
        self.vlc_target_time: float | None = None
        self.vlc_playback_button: tk.Button | None = None
        self.vlc_scale_frame: ttk.Frame | None = None
        self.vlc_scale_entry: ttk.Entry | None = None
        self.directory_report: ttk.Entry | None = None
        self.status_label: ttk.Label | None = None
        self.detail_frame: ttk.LabelFrame | None = None
        self.sync_load_warning: str | None = None
        self.sync_creation_window: tk.Toplevel | None = None
        self.sync_textgrid_path: Path | None = None
        self.sync_window_status: ttk.Label | None = None
        self.sync_window_missing_list: tk.Listbox | None = None
        self.sync_extract_button: ttk.Button | None = None
        self.sync_skip_button: ttk.Button | None = None
        self.sync_textgrid_report: ttk.Label | None = None
        self.sync_run_button: ttk.Button | None = None
        self.skip_wav_generation = False

        self._build_window()

    def _build_window(self) -> None:
        self.root.title("\U0001F440 Spike-and-Seek")
        self.root.geometry("450x600+1+1")
        ttk.Style(self.root).configure(
            "TButton", padding=BUTTON_PADDING, font=BUTTON_FONT
        )
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=3)
        self.root.bind("<Escape>", self.reset_selection)
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        tk.Label(
            self.root,
            text="Choose data directory",
            font=("Helvetica", 11, "bold"),
            pady=5,
        ).grid(**self._cell("data_dir_label"))
        ttk.Button(
            self.root,
            text="Browse",
            command=self.browse_data_directory,
        ).grid(**self._cell("data_dir_button"), **BUTTON_GRID)

    def _calculation_status(self, directory: Path) -> str:
        durations_exist = (directory / VIDEO_DURATIONS_FILENAME).is_file()
        if not durations_exist:
            return (
                f"Sync values are ready. {VIDEO_DURATIONS_FILENAME} is not present; "
                "calculations involving video streams require this file."
            )
        return (
            "Sync values and video durations are ready. Choose source and target "
            "datastreams to calculate a timestamp."
        )

    def browse_data_directory(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            title="Choose one session folder",
            mustexist=True,
        )
        if not selected:
            return

        directory = Path(selected).resolve()
        clear_media_file_cache()
        files = list_media_files(directory)
        self.data_directory = directory
        video_files = [
            path for path in files if path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        video_streams = resolve_stream_names_and_indexes(video_files)
        self.video_stream_names = {
            stream_name.casefold()
            for stream_name, _ in video_streams.values()
        }
        try:
            self.stream_names, stream_warning = load_optional_sync_streams(directory)
        except (
            OSError,
            ValueError,
            pd.errors.ParserError,
            pd.errors.EmptyDataError,
        ) as error:
            self.stream_names = []
            stream_warning = f"Could not read optional sync data: {error}"
        self.sync_load_warning = stream_warning

        self._render_session_flow()

        if not files:
            messagebox.showwarning(
                "No media files found",
                "No supported media files were found in the selected folder "
                "or its subfolders.",
                parent=self.root,
            )

    def open_sync_creation_window(self) -> None:
        if self.data_directory is None:
            return
        if (
            self.sync_creation_window is not None
            and self.sync_creation_window.winfo_exists()
        ):
            self.sync_creation_window.lift()
            self.sync_creation_window.focus_force()
            return

        clear_media_file_cache()
        window = tk.Toplevel(self.root)
        self.sync_creation_window = window
        self.sync_textgrid_path = None
        self.skip_wav_generation = False
        window.title("Create sync_values.csv")
        window.geometry("560x600")
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self.close_sync_creation_window)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)

        ttk.Label(
            window,
            text="Prepare WAV files and annotate the shared event",
            font=("Helvetica", 11, "bold"),
            wraplength=520,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        ttk.Label(
            window,
            text=f"Session folder: {self.data_directory}",
            wraplength=520,
        ).grid(row=1, column=0, sticky="w", padx=12, pady=4)

        self.sync_window_status = ttk.Label(
            window, text="", wraplength=520, justify="left"
        )
        self.sync_window_status.grid(
            row=2, column=0, sticky="nsew", padx=12, pady=6
        )
        ttk.Label(
            window,
            text="Select videos to generate WAVs for (all selected by default):",
        ).grid(row=3, column=0, sticky="w", padx=12, pady=(4, 0))
        self.sync_window_missing_list = tk.Listbox(
            window, height=6, exportselection=False, selectmode=tk.EXTENDED
        )
        self.sync_window_missing_list.grid(
            row=4, column=0, sticky="nsew", padx=12, pady=4
        )
        window.grid_rowconfigure(4, weight=1)

        action_frame = ttk.Frame(window)
        action_frame.grid(row=5, column=0, sticky="ew", padx=12, pady=5)
        action_frame.grid_columnconfigure((0, 1), weight=1, uniform="actions")
        self.sync_extract_button = ttk.Button(
            action_frame,
            text="Generate selected WAVs",
            command=self.generate_missing_wavs,
        )
        self.sync_extract_button.grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )

        self.sync_skip_button = ttk.Button(
            action_frame,
            text="Skip WAV generation",
            command=self.skip_missing_wav_generation,
        )
        self.sync_skip_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ttk.Button(
            window,
            text="Choose TextGrid annotation",
            command=self.choose_sync_textgrid,
        ).grid(row=6, column=0, sticky="ew", padx=12, pady=5)
        self.sync_textgrid_report = ttk.Label(
            window, text="No TextGrid selected", wraplength=520
        )
        self.sync_textgrid_report.grid(
            row=7, column=0, sticky="w", padx=12, pady=4
        )
        self.sync_run_button = ttk.Button(
            window,
            text="Synchronize Datastreams",
            command=self.synchronize_datastreams,
            state=tk.DISABLED,
        )
        self.sync_run_button.grid(row=8, column=0, sticky="ew", padx=12, pady=5)

        self._refresh_sync_creation_window()
        window.grab_set()

    def close_sync_creation_window(self) -> None:
        if self.sync_creation_window is not None:
            self.sync_creation_window.destroy()
        self.sync_creation_window = None
        self.sync_textgrid_path = None
        self.sync_window_status = None
        self.sync_window_missing_list = None
        self.sync_extract_button = None
        self.sync_skip_button = None
        self.sync_textgrid_report = None
        self.sync_run_button = None
        self.skip_wav_generation = False

    def _refresh_sync_creation_window(self) -> None:
        if self.data_directory is None or self.sync_creation_window is None:
            return
        try:
            missing = missing_video_wavs(self.data_directory)
        except SyncGenerationError as error:
            if self.sync_window_status is not None:
                self.sync_window_status.configure(
                    text=str(error),
                    foreground="#b00020",
                )
            for button in (
                self.sync_extract_button,
                self.sync_skip_button,
                self.sync_run_button,
            ):
                if button is not None:
                    button.configure(state=tk.DISABLED)
            return
        if self.sync_window_missing_list is not None:
            selected_paths = {
                self.sync_window_missing_list.get(index)
                for index in self.sync_window_missing_list.curselection()
            }
            select_all = self.sync_window_missing_list.size() == 0
            self.sync_window_missing_list.delete(0, tk.END)
            for index, video_path in enumerate(missing):
                relative_path = str(video_path.relative_to(self.data_directory))
                self.sync_window_missing_list.insert(tk.END, relative_path)
                if select_all or relative_path in selected_paths:
                    self.sync_window_missing_list.selection_set(index)

        if self.sync_window_status is not None:
            if missing:
                if self.skip_wav_generation:
                    status = (
                        "WAV generation skipped. Video datastreams with one "
                        "or more missing WAV soundtracks will be omitted."
                    )
                else:
                    status = (
                        f"{len(missing)} video file(s) have no matching WAV soundtrack. "
                        "Generate the WAV soundtracks or skip generation;  unpicked videos will be omitted."
                    )
                self.sync_window_status.configure(
                    text=status,
                    foreground="#b00020",
                )
            elif list_video_files(self.data_directory):
                self.sync_window_status.configure(
                    text="Every video file has a matching WAV soundtrack.",
                    foreground="#167d31",
                )
            else:
                self.sync_window_status.configure(
                    text="No video files found. WAV soundtrack checks are complete.",
                    foreground="#167d31",
                )

        if self.sync_extract_button is not None:
            self.sync_extract_button.configure(
                state=tk.NORMAL if missing else tk.DISABLED
            )
        if self.sync_skip_button is not None:
            self.sync_skip_button.configure(
                state=(
                    tk.NORMAL
                    if missing and not self.skip_wav_generation
                    else tk.DISABLED
                )
            )
        if self.sync_run_button is not None:
            can_run = (
                (not missing or self.skip_wav_generation)
                and self.sync_textgrid_path is not None
                and self.sync_textgrid_path.is_file()
            )
            self.sync_run_button.configure(
                state=tk.NORMAL if can_run else tk.DISABLED
            )

    def skip_missing_wav_generation(self) -> None:
        if self.data_directory is None or self.sync_creation_window is None:
            return
        self.skip_wav_generation = True
        self._refresh_sync_creation_window()

    def generate_missing_wavs(self) -> None:
        if self.data_directory is None or self.sync_creation_window is None:
            return
        if self.sync_window_missing_list is None:
            return
        selected_paths = {
            self.sync_window_missing_list.get(index)
            for index in self.sync_window_missing_list.curselection()
        }
        if not selected_paths:
            messagebox.showwarning(
                "No videos selected",
                "Select one or more videos from the list to generate WAVs.",
                parent=self.sync_creation_window,
            )
            return

        self.skip_wav_generation = False
        try:
            from moviepy.video.io.VideoFileClip import VideoFileClip
        except ImportError as error:
            messagebox.showerror(
                "WAV extraction unavailable",
                "Install MoviePy to extract WAV soundtracks.",
                parent=self.sync_creation_window,
            )
            return

        clear_media_file_cache()
        failed: list[str] = []
        try:
            missing = [
                video_path
                for video_path in missing_video_wavs(self.data_directory)
                if str(video_path.relative_to(self.data_directory)) in selected_paths
            ]
        except SyncGenerationError as error:
            messagebox.showerror(
                "Could not check WAV soundtracks",
                str(error),
                parent=self.sync_creation_window,
            )
            return
        if not missing:
            self._refresh_sync_creation_window()
            messagebox.showwarning(
                "No missing WAV soundtracks for the selected videos.",
                parent=self.sync_creation_window,
            )
            return
        for index, video_path in enumerate(missing, start=1):
            if self.sync_window_status is not None:
                self.sync_window_status.configure(
                    text=f"Extracting WAV {index} of {len(missing)}: {video_path.name}"
                )
            self.sync_creation_window.update_idletasks()
            output_path = video_path.with_suffix(".wav")
            try:
                video = VideoFileClip(str(video_path))
                try:
                    if video.audio is None:
                        failed.append(f"{video_path.name}: no audio track")
                        continue
                    video.audio.write_audiofile(
                        str(output_path), codec="pcm_s16le", logger=None
                    )
                finally:
                    video.close()
            except (OSError, RuntimeError, ValueError) as error:
                failed.append(f"{video_path.name}: {error}")
                # Do not leave a truncated WAV that would pass as a soundtrack.
                try:
                    output_path.unlink(missing_ok=True)
                except OSError:
                    pass

        clear_media_file_cache()
        self._refresh_sync_creation_window()
        if failed:
            messagebox.showwarning(
                "Some WAV files could not be generated",
                "\n".join(failed),
                parent=self.sync_creation_window,
            )

    def choose_sync_textgrid(self) -> None:
        if self.data_directory is None or self.sync_creation_window is None:
            return
        from tkinter import filedialog

        selected = filedialog.askopenfilename(
            parent=self.sync_creation_window,
            title="Choose the sync annotation TextGrid",
            initialdir=str(self.data_directory),
            filetypes=[("Praat TextGrid", "*.TextGrid *.textgrid"), ("All files", "*.*")],
        )
        if not selected:
            return

        textgrid_path = Path(selected).resolve()
        try:
            textgrid_path.relative_to(self.data_directory)
        except ValueError:
            messagebox.showerror(
                "TextGrid outside session folder",
                "Choose a TextGrid inside the selected data directory.",
                parent=self.sync_creation_window,
            )
            return

        self.sync_textgrid_path = textgrid_path
        if self.sync_textgrid_report is not None:
            self.sync_textgrid_report.configure(
                text=f"TextGrid: {textgrid_path.relative_to(self.data_directory)}"
            )
        clear_media_file_cache()
        self._refresh_sync_creation_window()

    def synchronize_datastreams(self) -> None:
        if self.data_directory is None or self.sync_creation_window is None:
            return
        if self.sync_textgrid_path is None:
            return

        clear_media_file_cache()
        output_path = self.data_directory / SYNC_VALUES_FILENAME
        if output_path.exists():
            messagebox.showwarning(
                "Sync values already exist",
                f"Will not overwrite existing file:\n{output_path}",
                parent=self.sync_creation_window,
            )
            self.close_sync_creation_window()
            self._render_session_flow()
            return

        try:
            missing = missing_video_wavs(self.data_directory)
        except SyncGenerationError as error:
            messagebox.showerror(
                "Could not check WAV soundtracks",
                str(error),
                parent=self.sync_creation_window,
            )
            return
        if missing and not self.skip_wav_generation:
            messagebox.showwarning(
                "Missing WAV soundtracks",
                "Generate or provide all video WAV soundtracks, or choose "
                "Skip WAV generation to omit affected video datastreams.",
                parent=self.sync_creation_window,
            )
            self._refresh_sync_creation_window()
            return

        if self.sync_window_status is not None:
            self.sync_window_status.configure(
                text=f"Assessing video durations and saving "
                f"{VIDEO_DURATIONS_FILENAME}."
            )
        self.sync_creation_window.update_idletasks()
        duration_path = self.data_directory / VIDEO_DURATIONS_FILENAME
        try:
            duration_table = create_video_duration_table(self.data_directory)
            duration_table.to_csv(duration_path, index=False)
        except (OSError, ValueError) as error:
            messagebox.showerror(
                "Could not store video durations",
                f"Could not create {VIDEO_DURATIONS_FILENAME} in the selected "
                f"data directory:\n{duration_path}\n\n{error}",
                parent=self.sync_creation_window,
            )
            return

        if self.sync_window_status is not None:
            self.sync_window_status.configure(
                text="Matching annotated events and cross-correlating every stream pair."
            )
        self.sync_creation_window.update_idletasks()
        if self.skip_wav_generation:
            video_streams = resolve_stream_names_and_indexes(
                list_video_files(self.data_directory)
            )
            omitted_video_streams = sorted(
                {video_streams[video_path][0] for video_path in missing},
                key=str.casefold,
            )
        else:
            omitted_video_streams = []
        try:
            sync_df = create_sync_values(
                self.sync_textgrid_path,
                self.data_directory,
                exclude_video_streams_missing_wavs=self.skip_wav_generation,
            )
            sync_df.to_csv(output_path, index=False)
        except (
            OSError, ValueError, KeyError, RuntimeError, MemoryError
        ) as error:
            messagebox.showerror(
                "Could not synchronize datastreams",
                str(error),
                parent=self.sync_creation_window,
            )
            return

        self.stream_names, self.sync_load_warning = load_optional_sync_streams(
            self.data_directory
        )
        if self.sync_load_warning or not self.stream_names:
            messagebox.showerror(
                "Generated sync values are invalid",
                self.sync_load_warning or "No datastream names were generated.",
                parent=self.sync_creation_window,
            )
            return

        if omitted_video_streams:
            messagebox.showwarning(
                "Synchronization complete with omitted video streams",
                f"Created {output_path.name}.\n\n"
                "These video datastreams were omitted because one or more "
                f"video files lack WAV soundtracks: {', '.join(omitted_video_streams)}",
                parent=self.sync_creation_window,
            )
        else:
            messagebox.showinfo(
                "Synchronization complete",
                f"Created {output_path.name}.",
                parent=self.sync_creation_window,
            )
        self.close_sync_creation_window()
        self._render_session_flow()

    def on_stream_selection(self, _event=None) -> None:
        self._clear_details()
        if self.detail_frame is None:
            return

        source_name = (
            self.source_stream_var.get() if self.source_stream_var is not None else ""
        )
        target_name = (
            self.target_stream_var.get() if self.target_stream_var is not None else ""
        )
        ttk.Label(
            self.detail_frame,
            text=f"Source datastream: {source_name or 'Not selected'}",
        ).grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Label(
            self.detail_frame,
            text=f"Target datastream: {target_name or 'Not selected'}",
        ).grid(row=1, column=0, sticky="w", padx=6, pady=3)

        self._render_timestamp_input()

    def _clear_timestamp_flow(self) -> None:
        self._clear_calculation_result("input_timestamp_label")
        self.timestamp_entry = None
        self.source_file_index_entry = None

    def _render_timestamp_input(self) -> None:
        self._clear_timestamp_flow()
        if (
            self.data_directory is None
            or self.source_stream_var is None
            or self.target_stream_var is None
        ):
            return

        source_stream = self.source_stream_var.get().strip()
        target_stream = self.target_stream_var.get().strip()
        if not source_stream or not target_stream:
            return

        source_is_video = source_stream.casefold() in self.video_stream_names
        prompt = (
            "Provide video file index and timestamp (HH:MM:SS[.sss])"
            if source_is_video
            else "Provide audio timestamp (seconds from stream start)"
        )
        ttk.Label(
            self.root,
            text=prompt,
            foreground="green",
            padding=10,
        ).grid(**self._cell("input_timestamp_label"), sticky="w")

        input_frame = ttk.Frame(self.root)
        input_frame.grid(
            **self._cell("input_timestamp_field"), sticky="ew", padx=8
        )
        if source_is_video:
            ttk.Label(input_frame, text="File index:").grid(
                row=0, column=0, sticky="w", padx=(0, 4)
            )
            self.source_file_index_entry = ttk.Entry(input_frame, width=5)
            self.source_file_index_entry.grid(row=0, column=1, padx=(0, 8))
            self._bind_clipboard_shortcuts(self.source_file_index_entry)
            ttk.Label(input_frame, text="Time:").grid(
                row=0, column=2, sticky="w", padx=(0, 4)
            )
            self.timestamp_entry = ttk.Entry(input_frame, width=14)
            self.timestamp_entry.grid(row=0, column=3, sticky="ew")
            self._bind_clipboard_shortcuts(self.timestamp_entry)
            input_frame.grid_columnconfigure(3, weight=1)
            self.source_file_index_entry.bind(
                "<Return>", self._calculate_timestamp
            )
        else:
            self.timestamp_entry = ttk.Entry(input_frame, width=18)
            self.timestamp_entry.grid(row=0, column=0, sticky="ew")
            self._bind_clipboard_shortcuts(self.timestamp_entry)
            input_frame.grid_columnconfigure(0, weight=1)

        self.timestamp_entry.bind("<Return>", self._calculate_timestamp)
        button_frame = ttk.Frame(self.root)
        button_frame.grid(**self._cell("input_timestamp_button"), **BUTTON_GRID)
        button_frame.grid_columnconfigure((0, 1), weight=1, uniform="actions")
        ttk.Button(
            button_frame,
            text="Calculate",
            command=self._calculate_timestamp,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            button_frame,
            text="Clear",
            command=self._clear_timestamp_values,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _bind_clipboard_shortcuts(self, entry: ttk.Entry) -> None:
        entry.bind(
            "<Control-KeyPress>",
            self._handle_layout_independent_clipboard_shortcut,
            add="+",
        )

    def _handle_layout_independent_clipboard_shortcut(
        self, event: tk.Event
    ) -> str | None:
        virtual_event = WINDOWS_CLIPBOARD_KEYCODES.get(event.keycode)
        if virtual_event is None:
            virtual_event = {
                "c": "<<Copy>>",
                "v": "<<Paste>>",
            }.get(event.keysym.casefold())
        if virtual_event is None:
            return None

        event.widget.event_generate(virtual_event)
        return "break"

    def _clear_calculation_result(self, first_element: str = "result_label") -> None:
        """Close VLC and destroy main-window widgets from this element's row down."""
        self._close_vlc_playback()
        self.vlc_target_video_path = None
        self.vlc_target_time = None
        first_row = GUI_GRID[first_element]["row"]
        for widget in self.root.grid_slaves():
            if int(widget.grid_info()["row"]) >= first_row:
                widget.destroy()
        self.vlc_playback_button = None
        self.vlc_scale_frame = None
        self.vlc_scale_entry = None

    def _on_closing(self) -> None:
        cleanup_errors = self._release_vlc_resources()
        if cleanup_errors:
            print("VLC cleanup warning: " + "; ".join(cleanup_errors))
        self.root.destroy()

    def _clear_timestamp_values(self) -> None:
        if self.source_file_index_entry is not None:
            self.source_file_index_entry.delete(0, tk.END)
        if self.timestamp_entry is not None:
            self.timestamp_entry.delete(0, tk.END)
        self._clear_calculation_result()

    def _show_timestamp_error(self, message: str) -> None:
        self._clear_calculation_result()
        ttk.Label(
            self.root,
            text=message,
            foreground="#b00020",
            wraplength=420,
            justify="left",
        ).grid(
            **self._cell("input_timestamp_error"), sticky="w", padx=8, pady=5
        )

    def _cell(self, element: str) -> dict[str, int]:
        return dict(GUI_GRID[element])

    def _readonly_entry(self, element: str, text: str) -> None:
        field = ttk.Entry(self.root, width=18)
        field.insert(0, text)
        field.configure(state="readonly")
        field.grid(**self._cell(element), sticky="ew", padx=8)
        self._bind_clipboard_shortcuts(field)

    def _show_timestamp_result(
        self,
        target_stream: str,
        target_timestamp: float,
        target_video_location: tuple[int, float] | None,
        target_video_path: Path | None,
    ) -> None:
        if target_video_location is not None and target_video_path is None:
            raise TimestampCalculationError(
                f"Could not resolve a video file for '{target_stream}'."
            )
        ttk.Label(
            self.root,
            text=f"Calculated timestamp in {target_stream}:",
            foreground="blue",
            padding=8,
        ).grid(**self._cell("result_label"), sticky="w")

        if target_video_location is None:
            self._readonly_entry(
                "result_field_default", f"{target_timestamp:.3f}"
            )
            return

        _, local_time = target_video_location
        for prefix, label, value in (
            ("result_C_N", "Target filename:", target_video_path.name),
            (
                "result_C_time",
                "Target file time (HH:MM:SS):",
                format_video_timecode(local_time),
            ),
        ):
            ttk.Label(self.root, text=label).grid(
                **self._cell(f"{prefix}_label"), sticky="w", padx=8
            )
            self._readonly_entry(f"{prefix}_field", value)

        self.vlc_target_video_path = target_video_path
        self.vlc_target_time = local_time
        self._show_vlc_controls()

    def _show_vlc_controls(self) -> None:
        cell = self._cell("open_vlc_button")
        self.vlc_playback_button = tk.Button(
            self.root,
            command=self._toggle_vlc_playback,
            font=BUTTON_FONT,
            padx=BUTTON_PADDING[0],
            pady=BUTTON_PADDING[1],
            foreground="white",
            activeforeground="white",
        )
        self._set_vlc_playback_button_state(playing=False)
        self.vlc_playback_button.grid(**cell, **BUTTON_GRID)
        ttk.Label(
            self.root,
            text="Playback will start 2 sec before the event",
        ).grid(
            row=cell["row"] + 1,
            column=cell["column"],
            columnspan=cell["columnspan"],
            sticky="w",
            padx=8,
        )
        scale_column = (
            0 if cell["column"] > 0 else cell["column"] + cell["columnspan"]
        )
        ttk.Button(
            self.root,
            text="Change VLC window scale",
            command=self._toggle_vlc_scale_editor,
        ).grid(row=cell["row"], column=scale_column, **BUTTON_GRID)

    def _set_vlc_playback_button_state(self, *, playing: bool) -> None:
        if self.vlc_playback_button is None:
            return
        if playing:
            self.vlc_playback_button.configure(
                text="Hide VLC",
                background="#ef6c00",
                activebackground="#e65100",
            )
        else:
            self.vlc_playback_button.configure(
                text="Go to VLC",
                background="#2e7d32",
                activebackground="#1b5e20",
            )

    def _toggle_vlc_scale_editor(self) -> None:
        if self.vlc_scale_frame is not None:
            self.vlc_scale_frame.destroy()
            self.vlc_scale_frame = None
            self.vlc_scale_entry = None
            return

        button_row = GUI_GRID["open_vlc_button"]["row"]
        self.vlc_scale_frame = ttk.Frame(self.root)
        self.vlc_scale_frame.grid(
            row=button_row + 2,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=8,
            pady=4,
        )
        self.vlc_scale_frame.grid_columnconfigure(1, weight=1)
        ttk.Label(self.vlc_scale_frame, text="Scale:").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.vlc_scale_entry = ttk.Entry(self.vlc_scale_frame, width=12)
        self.vlc_scale_entry.insert(0, str(self.vlc_scale))
        self.vlc_scale_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.vlc_scale_entry.bind("<Return>", self._apply_vlc_scale)
        ttk.Button(
            self.vlc_scale_frame,
            text="Set scale",
            command=self._apply_vlc_scale,
        ).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(
            self.vlc_scale_frame,
            text="Cancel",
            command=self._toggle_vlc_scale_editor,
        ).grid(row=0, column=3)

    def _apply_vlc_scale(self, _event=None) -> None:
        if self.vlc_scale_entry is None:
            return
        try:
            new_scale = float(
                self.vlc_scale_entry.get().strip().replace(",", ".")
            )
        except ValueError:
            messagebox.showerror(
                "Invalid VLC scale",
                "Enter a positive number, for example 0.5.",
                parent=self.root,
            )
            return
        if not math.isfinite(new_scale) or new_scale <= 0:
            messagebox.showerror(
                "Invalid VLC scale",
                "VLC scale must be a finite number greater than zero.",
                parent=self.root,
            )
            return

        playback_was_active = self.vlc_player is not None
        self.vlc_scale = new_scale
        target_path = self.vlc_target_video_path
        target_time = self.vlc_target_time
        if self.vlc_scale_frame is not None:
            self.vlc_scale_frame.destroy()
            self.vlc_scale_frame = None
            self.vlc_scale_entry = None
        if playback_was_active and target_path is not None and target_time is not None:
            self._launch_vlc(target_path, target_time)

    def _toggle_vlc_playback(self) -> None:
        if self.vlc_player is not None:
            self._hide_vlc_playback()
            return
        if self.vlc_target_video_path is None or self.vlc_target_time is None:
            messagebox.showerror(
                "No video result",
                "Calculate a target video timestamp before opening VLC.",
                parent=self.root,
            )
            return
        self._launch_vlc(self.vlc_target_video_path, self.vlc_target_time)

    def _hide_vlc_playback(self) -> None:
        cleanup_errors = self._release_vlc_resources()
        self._set_vlc_playback_button_state(playing=False)
        if cleanup_errors:
            messagebox.showwarning(
                "VLC cleanup warning",
                "\n".join(cleanup_errors),
                parent=self.root,
            )

    def _release_vlc_resources(self) -> list[str]:
        cleanup_errors = []
        vlc_errors = (OSError, RuntimeError)
        if self.vlc_api is not None:
            vlc_errors += (self.vlc_api.VLCException,)

        resources = (
            ("stop player", self.vlc_player, "stop"),
            ("release player", self.vlc_player, "release"),
            ("release media", self.vlc_media, "release"),
            ("release VLC instance", self.vlc_instance, "release"),
        )
        for description, resource, action in resources:
            if resource is None:
                continue
            try:
                getattr(resource, action)()
            except vlc_errors as error:
                cleanup_errors.append(f"Could not {description}: {error}")

        self.vlc_player = None
        self.vlc_media = None
        self.vlc_instance = None
        return cleanup_errors

    def _close_vlc_playback(self) -> None:
        cleanup_errors = self._release_vlc_resources()
        self._set_vlc_playback_button_state(playing=False)
        for error in cleanup_errors:
            print(f"VLC cleanup warning: {error}")

    def _launch_vlc(self, video_path: Path, target_time: float) -> None:
        try:
            import vlc
        except (ImportError, OSError, NotImplementedError) as error:
            messagebox.showwarning(
                "VLC not installed",
                "VLC media player (or its Python bindings) was not found, so "
                "the video cannot be opened.\n\n"
                "Install VLC from https://www.videolan.org/vlc/ and the "
                "python-vlc package, then try again.\n\n"
                f"Details: {error}",
                parent=self.root,
            )
            return

        if not video_path.is_file():
            messagebox.showerror(
                "Video file not found",
                f"The calculated video file no longer exists:\n{video_path}",
                parent=self.root,
            )
            return

        self._close_vlc_playback()
        self.vlc_api = vlc
        self.root.update_idletasks()
        vlc_x = self.root.winfo_x() + self.root.winfo_width() + 20
        vlc_y = self.root.winfo_y()
        start_time = max(0.0, target_time - 2.0)
        configured_args = [
            "--video-x", str(vlc_x),
            "--video-y", str(vlc_y),
            "--aout", "directsound",
            "--avcodec-hw", "none",
            "--vout", "directdraw",
            "--no-video-deco",
            "--file-caching", "3000",
            "--network-caching", "3000",
            "--quiet",
        ]
        attempts = (
            ("configured VLC", configured_args),
            ("fallback VLC", ["--aout", "directsound"]),
        )
        failures = []
        for attempt_name, arguments in attempts:
            try:
                instance = vlc.Instance(arguments)
                if instance is None:
                    raise RuntimeError("VLC did not create an instance.")
                self.vlc_instance = instance
                media = instance.media_new(str(video_path))
                if media is None:
                    raise RuntimeError("VLC could not create the target media.")
                self.vlc_media = media
                media.add_option(f"start-time={start_time:.3f}")
                player = instance.media_player_new()
                if player is None:
                    raise RuntimeError("VLC could not create a media player.")
                self.vlc_player = player
                player.set_media(media)
                player.video_set_scale(self.vlc_scale)
                if player.play() == -1:
                    raise RuntimeError("VLC could not start playback.")
                self._set_vlc_playback_button_state(playing=True)
                return
            except (vlc.VLCException, OSError, RuntimeError) as error:
                failures.append(f"{attempt_name}: {error}")
                failures.extend(self._release_vlc_resources())

        self._set_vlc_playback_button_state(playing=False)
        messagebox.showerror(
            "VLC playback failed",
            "Could not open the target video in VLC.\n\n"
            + "\n".join(failures),
            parent=self.root,
        )

    def _calculate_timestamp(self, _event=None) -> None:
        if (
            self.data_directory is None
            or self.source_stream_var is None
            or self.target_stream_var is None
            or self.timestamp_entry is None
        ):
            return

        source_stream = self.source_stream_var.get().strip()
        target_stream = self.target_stream_var.get().strip()
        if not source_stream or not target_stream:
            return

        try:
            if source_stream.casefold() in self.video_stream_names:
                if self.source_file_index_entry is None:
                    raise TimestampCalculationError(
                        "Enter the source video file index."
                    )
                try:
                    source_file_index = int(
                        self.source_file_index_entry.get().strip()
                    )
                except ValueError as error:
                    raise TimestampCalculationError(
                        "Enter a positive integer source video file index."
                    ) from error
                if source_file_index < 1:
                    raise TimestampCalculationError(
                        "The source video file index must be at least 1."
                    )
                source_local_time = parse_video_timecode(
                    self.timestamp_entry.get()
                )
                source_durations = read_video_stream_durations(
                    self.data_directory, source_stream
                )
                if source_file_index not in source_durations:
                    raise TimestampCalculationError(
                        f"Video file index {source_file_index} was not found for "
                        f"'{source_stream}'."
                    )
                source_file_duration = source_durations[source_file_index]
                if source_local_time > source_file_duration:
                    raise TimestampCalculationError(
                        f"The source time {format_video_timecode(source_local_time)} "
                        f"exceeds file {source_file_index}'s duration "
                        f"({source_file_duration:.3f}s)."
                    )
                source_timestamp = source_local_time + sum(
                    duration
                    for file_index, duration in source_durations.items()
                    if file_index < source_file_index
                )
            else:
                try:
                    source_timestamp = float(self.timestamp_entry.get().strip())
                except ValueError as error:
                    raise TimestampCalculationError(
                        "Enter the source audio timestamp in seconds."
                    ) from error
                if not math.isfinite(source_timestamp) or source_timestamp < 0:
                    raise TimestampCalculationError(
                        "The source audio timestamp must be a finite, non-negative "
                        "number of seconds."
                    )

            sync_path = self.data_directory / SYNC_VALUES_FILENAME
            sync_df = pd.read_csv(sync_path)
            target_is_video = (
                target_stream.casefold() in self.video_stream_names
            )
            target_durations = (
                read_video_stream_durations(self.data_directory, target_stream)
                if target_is_video
                else read_audio_stream_durations(
                    self.data_directory, target_stream
                )
            )
            target_timestamp = convert_source_timestamp(
                sync_df,
                source_stream,
                target_stream,
                source_timestamp,
                target_duration=sum(target_durations.values()),
            )

            target_video_location = None
            target_video_path = None
            if target_is_video:
                target_video_location = locate_video_timestamp(
                    target_timestamp, target_durations, target_stream
                )
                target_file_index, _ = target_video_location
                target_video_path = resolve_target_video_path(
                    self.data_directory, target_stream, target_file_index
                )
            else:
                validate_audio_timestamp(
                    target_timestamp, target_durations, target_stream
                )
            self._clear_calculation_result()
            self._show_timestamp_result(
                target_stream,
                target_timestamp,
                target_video_location,
                target_video_path,
            )
        except TimestampCalculationError as error:
            self._show_timestamp_error(str(error))
        except (
            OSError,
            ValueError,
            KeyError,
            pd.errors.ParserError,
            pd.errors.EmptyDataError,
        ) as error:
            self._show_timestamp_error(f"Could not calculate timestamp: {error}")

    def _clear_details(self) -> None:
        if self.detail_frame is not None:
            for widget in self.detail_frame.winfo_children():
                widget.destroy()

    def _open_data_directory(self) -> None:
        if self.data_directory is None:
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(self.data_directory)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.data_directory)])
            else:
                subprocess.Popen(["xdg-open", str(self.data_directory)])
        except OSError as error:
            messagebox.showerror(
                "Cannot open directory",
                f"Could not open the data directory:\n{error}",
                parent=self.root,
            )

    def _render_session_flow(self) -> None:
        self._clear_calculation_result("data_dir_subframe")
        self.directory_report = None
        self.source_stream_var = None
        self.target_stream_var = None
        self.source_selector = None
        self.target_selector = None
        self.timestamp_entry = None
        self.source_file_index_entry = None
        self.status_label = None
        self.detail_frame = None

        if self.data_directory is None:
            return

        directory_frame = tk.LabelFrame(self.root, padx=10, pady=10)
        directory_frame.grid(**self._cell("data_dir_subframe"), sticky="ew")
        ttk.Label(directory_frame, text="Data directory:").grid(
            row=0, column=0, sticky="w"
        )
        self.directory_report = ttk.Entry(directory_frame)
        self.directory_report.insert(0, str(self.data_directory))
        self.directory_report.configure(state="readonly")
        self._bind_clipboard_shortcuts(self.directory_report)
        self.directory_report.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        directory_frame.grid_columnconfigure(1, weight=1)
        ttk.Button(
            directory_frame,
            text="Open in file explorer",
            command=self._open_data_directory,
        ).grid(row=0, column=2, padx=(8, 0))

        source_prompt_row = GUI_GRID["filename_label"]["row"]
        if not (self.data_directory / SYNC_VALUES_FILENAME).is_file():
            tk.Label(
                self.root,
                text=f"{SYNC_VALUES_FILENAME} reference table was not found.",
                fg="#b00020",
                font=("Helvetica", 11, "bold"),
                pady=8,
            ).grid(row=source_prompt_row, column=0, columnspan=2)
            tk.Label(
                self.root,
                text="Datastreams are not synchronized. Unable to proceed.",
                wraplength=420,
                justify="left",
            ).grid(
                row=GUI_GRID["filename_entry"]["row"],
                column=0,
                columnspan=2,
                sticky="w",
                padx=8,
            )
            ttk.Button(
                self.root,
                text="Create Synchronization Table",
                command=self.open_sync_creation_window,
            ).grid(
                row=GUI_GRID["filename_subframe"]["row"],
                column=0,
                columnspan=2,
                **BUTTON_GRID,
            )
            return

        if self.sync_load_warning or not self.stream_names:
            warning = self.sync_load_warning or (
                f"{SYNC_VALUES_FILENAME} contains no datastream names."
            )
            tk.Label(
                self.root,
                text=warning,
                fg="#b00020",
                wraplength=420,
                justify="left",
                pady=8,
            ).grid(row=source_prompt_row, column=0, columnspan=2)
            return

        tk.Label(
            self.root,
            text="Choose datastreams:",
            height=2,
            font=("Helvetica", 11, "bold"),
            pady=5,
        ).grid(**self._cell("filename_label"))

        selection_frame = ttk.Frame(self.root)
        selection_frame.grid(
            row=GUI_GRID["filename_entry"]["row"],
            column=GUI_GRID["filename_entry"]["column"],
            columnspan=2,
            sticky="ew",
            padx=8,
            pady=4,
        )
        selection_frame.grid_columnconfigure(1, weight=1)
        self.source_stream_var = tk.StringVar(master=self.root)
        self.target_stream_var = tk.StringVar(master=self.root)
        for row, label, variable in (
            (0, "Event spotted in:", self.source_stream_var),
            (1, "Check against:", self.target_stream_var),
        ):
            ttk.Label(selection_frame, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=3
            )
            selector = ttk.Combobox(
                selection_frame,
                textvariable=variable,
                values=self.stream_names,
                state="readonly",
            )
            selector.grid(row=row, column=1, sticky="ew", pady=3)
            selector.bind("<<ComboboxSelected>>", self.on_stream_selection)
            if row == 0:
                self.source_selector = selector
            else:
                self.target_selector = selector

        detail_title = ttk.Label(
            self.root,
            text="Datastream selections",
            font=("Helvetica", 11, "bold"),
        )
        detail_frame = ttk.LabelFrame(self.root, labelwidget=detail_title)
        detail_frame.grid(
            **self._cell("filename_subframe"), sticky="ew", padx=8, pady=4
        )
        self.detail_frame = detail_frame

        self.status_label = ttk.Label(
            self.root,
            text=self._calculation_status(self.data_directory),
            wraplength=420,
            justify="left",
        )
        self.status_label.grid(
            **self._cell("target_selection_subframe"),
            sticky="w",
            padx=8,
            pady=6,
        )

    def reset_selection(self, _event=None) -> None:
        if self.data_directory is not None:
            clear_media_file_cache()
            self._render_session_flow()


def main() -> None:
    root = tk.Tk()
    SpikeAndSeek(root)
    root.mainloop()


if __name__ == "__main__":
    main()
