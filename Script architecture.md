## Script architecture

> **Disclaimer**: This text is generated with GitHub CoPilot.


The implementation is in [`Spike_and_Seek.py`](./Spike_and_Seek.py). It is a
Tkinter desktop application with three main responsibilities:

1. Discover session media and identify datastreams and indexed file chunks.
2. Generate pairwise synchronization values from a shared event annotated in
   a Praat TextGrid.
3. Convert a timestamp from a source datastream to a target datastream, with
   optional VLC playback for video results.

The code is divided into media and duration helpers, synchronization helpers,
timestamp-conversion helpers, and the `SpikeAndSeek` GUI controller.

### Application flow

1. **Select a session folder.** The app scans the folder and its subfolders for
   supported audio and video files, identifies video streams, and loads
   datastream names from `sync_values.csv` if it exists. The entry point is
   [`main`](./Spike_and_Seek.py#L2157); folder selection and initial state
   setup are handled by
   [`browse_data_directory`](./Spike_and_Seek.py#L969).
2. **Create synchronization data if needed.** The synchronization window
   checks for missing video WAV counterparts and selects all missing videos
   initially. Users can choose which WAVs to generate, repeat extraction for
   another selection, or skip the remaining WAVs and omit affected video
   datastreams from synchronization. After choosing a TextGrid inside the
   session folder, the app writes `video_durations.csv` and then
   `sync_values.csv`. It does not overwrite an existing `sync_values.csv`.
   This flow is coordinated by
   [`synchronize_datastreams`](./Spike_and_Seek.py#L1322).
3. **Choose streams and enter a time.** Audio sources take a timestamp in
   seconds. Video sources take a file index and an `HH:MM:SS[.sss]` time.
   [`_calculate_timestamp`](./Spike_and_Seek.py#L1888) converts the source time
   to the stream's cumulative timeline, applies the pairwise sync value, and
   validates the result against the target stream's duration.
4. **Display or play the result.** Audio targets are displayed as seconds.
   Video targets are resolved to a file and a local time within that file;
   [`_show_timestamp_result`](./Spike_and_Seek.py#L1601) displays them and
   offers VLC playback.

### Function groups

#### Media discovery, streams, and durations

- [`list_media_files`](./Spike_and_Seek.py#L100) recursively scans supported
  extensions, sorts and caches the results, and ignores resolved paths outside
  the selected directory. [`clear_media_file_cache`](./Spike_and_Seek.py#L95)
  invalidates that cache when the directory contents may have changed.
- [`list_video_files`](./Spike_and_Seek.py#L126),
  [`find_video_wav_counterpart`](./Spike_and_Seek.py#L133), and
  [`missing_video_wavs`](./Spike_and_Seek.py#L154) filter videos and find
  same-folder WAVs with case-insensitive matching stems.
- [`stream_name_and_index`](./Spike_and_Seek.py#L231) parses a trailing
  `_N` chunk index. [`resolve_stream_names_and_indexes`](./Spike_and_Seek.py#L238)
  groups chunks and treats a lone numeric-suffixed file other than `_1` as an
  unindexed stream at index 1.
- [`wav_duration_seconds`](./Spike_and_Seek.py#L182) reads standard WAV
  headers, falling back to Praat for WAV formats unsupported by Python's
  `wave` module. [`audio_duration_seconds`](./Spike_and_Seek.py#L164) reads
  other audio durations with Praat LongSound.
- [`video_duration_seconds`](./Spike_and_Seek.py#L205) reads each video's
  duration from its video metadata using MoviePy. [`create_video_duration_table`](./Spike_and_Seek.py#L263)
  writes one row per video to `video_durations.csv`, with its relative
  `video_file` path, datastream name, file index, and video duration. WAVs
  remain the audio source for synchronization and cross-correlation.
- [`load_optional_sync_streams`](./Spike_and_Seek.py#L293) reads stream names
  from `datastream_name`, or falls back to `target_stream`, in the sync CSV.

#### Synchronization generation

- [`read_sync_landmarks`](./Spike_and_Seek.py#L319) reads the TextGrid `sync`
  tier. Point tiers use each point's time; interval tiers use each interval's
  midpoint. Nonempty labels identify WAV filename stems. Each landmark is
  converted to an absolute stream time by adding the durations of earlier
  indexed files.
- [`cross_correlation_lag`](./Spike_and_Seek.py#L794) opens audio as Praat
  LongSound, extracts up to 20 seconds around each landmark using matching
  windows, converts the segments to mono, and resamples them to a common
  sample rate when needed. It finds the peak absolute cross-correlation.
- [`build_sync_value_rows`](./Spike_and_Seek.py#L490) calculates each stream
  pair once, assigns the reverse direction the opposite lag, and constructs
  `cc_lag_*` and `sync_value_*` columns. The sync value is the difference
  between absolute landmark times minus the measured lag.
- [`create_sync_values`](./Spike_and_Seek.py#L877) connects landmark reading
  and pairwise correlation, returning the rows as a DataFrame.

#### Timestamp conversion

- [`parse_video_timecode`](./Spike_and_Seek.py#L528) parses
  `HH:MM:SS[.fraction]`; [`format_video_timecode`](./Spike_and_Seek.py#L541)
  formats seconds to milliseconds.
- [`read_video_stream_durations`](./Spike_and_Seek.py#L549) reads video chunk
  durations from `video_durations.csv`.
  [`read_audio_stream_durations`](./Spike_and_Seek.py#L619) discovers audio
  chunks and measures their durations.
- [`convert_source_timestamp`](./Spike_and_Seek.py#L685) finds the source
  stream's sync row and applies `target time = source time - sync value`.
  [`validate_audio_timestamp`](./Spike_and_Seek.py#L669) checks a time against
  the total stream duration.
- [`locate_video_timestamp`](./Spike_and_Seek.py#L749) maps a cumulative
  target time to a video chunk and its local time.
  [`resolve_target_video_path`](./Spike_and_Seek.py#L771) finds the matching
  video file and rejects ambiguous matches.

#### GUI and VLC

The `SpikeAndSeek` class builds and updates the Tkinter interface. Its
directory and session flow is handled by
[`_render_session_flow`](./Spike_and_Seek.py#L2013); stream selections and
timestamp input are managed by
[`on_stream_selection`](./Spike_and_Seek.py#L1438) and
[`_render_timestamp_input`](./Spike_and_Seek.py#L1465). The sync-creation
window and WAV/TextGrid actions are handled by
[`open_sync_creation_window`](./Spike_and_Seek.py#L1013),
[`generate_missing_wavs`](./Spike_and_Seek.py#L1204), and
[`choose_sync_textgrid`](./Spike_and_Seek.py#L1289).

VLC helpers display playback controls, apply a configurable positive scale,
start up to two seconds before the calculated target time, try a fallback VLC
configuration if launch fails, and release player/media resources when
playback is hidden or the app closes.

### Main checks and failure handling

- **Media matching:** Only supported extensions are scanned. A video with
  multiple matching sibling WAVs is rejected. Missing WAVs can be extracted
  using MoviePy or skipped. If extraction is skipped, a video datastream with
  any missing WAV counterpart is omitted from synchronization.
- **TextGrid and landmarks:** The `sync` tier must be a point or interval
  tier. Every nonempty, non-excluded label must match exactly one WAV stem.
  The selected TextGrid must be inside the session folder.
- **Stream continuity:** Indexed WAV chunks used for landmarks must be
  consecutive from index 1. Duplicate stream indexes and multiple event
  annotations for one datastream are rejected. The sync tier must cover at
  least two datastreams.
- **Landmark bounds and audio:** Points and intervals must fit within the
  matched WAV's duration; interval end must be after its start. Audio
  durations must be finite and positive, and correlation requires a usable
  segment around both landmarks.
- **CSV integrity:** Video duration data must contain the required columns,
  finite positive durations, positive integer indexes, no duplicate indexes,
  and consecutive indexes from 1. Sync conversion requires a unique source
  row and a finite numeric pairwise sync value.
- **Timestamp ranges:** Input timecodes and file indexes must be valid; a
  source video time cannot exceed its chunk duration. Calculated target times
  must be finite, nonnegative, and no greater than the target stream's total
  duration.
- **Playback and UI errors:** Errors are shown through dialogs or inline
  messages. Failed WAV extraction reports the affected files and attempts to
  remove incomplete output. VLC playback checks that the target file exists,
  retries with fallback options, and reports cleanup failures.

Praat/parselmouth and `tgt` are used for TextGrid synchronization and audio
analysis. MoviePy reads video durations and extracts selected WAV
counterparts. VLC playback additionally requires the Python bindings and a
compatible VLC/libVLC installation.