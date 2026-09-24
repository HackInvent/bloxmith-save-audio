# Save Audio

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->


Records an encoded audio stream to a chosen directory on explicit command. The block owns all file handling; the framework only provides transport and persistent listening.

## Version and declared compatibility

Block version: **0.1.0**, following the shared initial-version policy.
Declared and tested framework version: **BloxSmith 1.0.9**, in `centralized` and `zeromq_active` modes. These tests do not establish compatibility with other versions.

## Ports and wiring

- `audio_in` (1): `audio_stream` input, `audio/*`, mono or stereo Opus/AAC, `one` multiplicity, not required for data activations.
- `command_in` (2): `message` input, `application/json`, `one` multiplicity, required for ordinary activations.
- No outputs. Saved files and the latest state are reported in the block's runtime results.

Connect **both** `Microphone Stream.audio_out → audio_in` and `Microphone Stream.command_out → command_in`. An audio link alone does not trigger saving. For legacy nodes with only one input, recreate the block from the new model, restore its settings and reconnect its links. The implementation does not automatically migrate blueprints.

**OpenAI TTS Stream** uses the same two links and protocol: connect its `audio_out` and `command_out` to the matching inputs. Each text produces an `.ogg` file, finalized after the stop command and all pages have arrived. The shared format is **Opus in WebM or Ogg**: `.webm`/`.ogg` for the microphone and `.ogg` for TTS. AAC remains accepted for other existing compatible sources.

## Lifecycle

1. Active Runtime **Run** starts `listen_runtime` through `listen_on_run=True`, without Play and without creating a file.
2. JSON command `{"action":"start","stream_id":"session"}` arms a session.
3. Chunks for that session are written incrementally to a temporary file.
4. `{"action":"stop","stream_id":"session","frame_count":12,"byte_count":32000,"aborted":false}` announces completion. The block waits for the announced chunks, checks continuity and totals, then finalizes the file.
5. A stop with `aborted: true` abandons only that session, without saving an incomplete file or stopping the run.
6. The listener remains available for subsequent captures. Stopping the microphone or interrupting TTS does not stop the run.

`execute_runtime` validates commands and forwards them to `runtime_listener.send`; it no longer contains a blocking loop. Listener state stays in local variables, never on the shared block definition.

Each fresh data delivery is handled separately (`on_each_event`). When a runtime
context contains several deliveries, their JSON objects are validated individually
and forwarded in order, never parsed as concatenated text. Retained/consumed port
values are not replayed. This supports rapid start/stop events from **Audio Merge
Stream**, with one file per stream (up to four concurrent recordings), or the
single combined stream produced by **Audio Mixer**. Connect both output links.

## Directory and files

- `output_dir`: server-side path, absolute or relative to the runtime root; created when the first chunk of an armed session arrives.
- `filename_template`: 1–180 characters, with placeholders `{node_id}`, `{timestamp}`, `{stream_id}`, `{codec}`. Non-portable characters are replaced.
- `idle_finalize_sec`: **maximum wait for missing chunks after the stop command**, 0.25–30 seconds, default 1.5. The legacy key is retained for existing configurations, but inactivity alone no longer finalizes a file.

The container is detected from the first bytes (WebM, Ogg, WAV, FLAC, MP4/M4A, MP3), with a codec-based fallback. The block concatenates received bytes without transcoding or repairing them. Existing files are never overwritten: a numeric suffix is added.

During writing, the reserved final file may appear empty; data is held in a hidden `.*.part` file. After validation, flush/fsync and atomic replacement make the final file available. An empty capture creates no file.

## Synchronization, errors and shutdown

Audio and commands are independent. Before `start`, the block buffers at most **5 seconds, 128 chunks and 8 MiB**, dropping the oldest chunks when limits are exceeded. This absorbs short data-channel delays; it is not storage for deferred recording. Up to four sessions may start or finish concurrently.

A **stop with `aborted: true` cancels a stream; it is not a fatal error**. The block immediately closes and removes that session's temporary file and final-file reservation, drops its pending chunks and ignores late frames/commands. This also works before the first chunk or if `start` arrives after cancellation. State `save_audio.state: "cancelled"` identifies the affected `stream_id` and reports that the block is listening for the next streams. It does not mean a file was saved. Other active sessions and finalized files remain intact. A new capture or synthesis with a fresh ID can be recorded without restarting Run.

Missing or out-of-order chunks, inconsistent counters, a **non-aborted** stop without start, malformed commands and file errors remain visible fatal errors. A deletion failure during cancellation is also reported, not disguised as successful cancellation. Unfinished temporary files are cleaned up when the listener closes; restart the run after a fatal error. Finalized files remain intact.

**Runtime Stop/Cancel is not a data stop command**: it cancels unfinished sessions. To save normally, first stop the microphone and wait for the file-saved status. With TTS, wait for that status after synthesis; do not stop the Run to request saving. Interrupted synthesis sends `aborted: true` when possible: Save Audio abandons it, stays listening and does not present it as a complete file. Save Audio does not stop Speaker playback or interrupt any other block.

Results retain the last 20 files and cumulative counters, without audio bytes. Recent duplicate start/stop commands for completed sessions are ignored (128 IDs remembered). Use a fresh ID for every capture.

## Simulation and tests

One Shot Simulation creates neither a listener nor a file; block execution returns `skipped` when invoked. Ordinary messages retain their required-input rules.

From the private `bloxmith-blocs` test workspace, run `python3 -B tests/run_tests.py save_audio`. It checks both modes, the real browser/HTTP/WebSocket path without Play, explicit data wiring, chunks arriving before start or after stop, successive captures, collisions, cancellations and incomplete streams. Instance-scoped scenarios create blueprints in temporary directories through the framework manager, without developer data.

The TTS suite `F5.49_opus_interoperability.py` also checks fan-out of a real local Opus HTTP response, automatic TTS commands, byte-for-byte saved content and the final mono/stereo profile using FFprobe, without paid OpenAI calls.

`F5.55_cancelled_streams.py` reproduces the `aborted: true` crash and checks six successive cancellations, concurrent sessions, late commands/frames, cancellation before start or audio, cleanup errors and file-free simulation. Its real TTS → Save Audio mini-graph receives three data interruptions during a local Opus HTTP response, then saves the next response exactly in the **same run**. TTS fixtures are reused only in tests, never by the block implementation.

## Compatibility policy

[compatibility.json](compatibility.json) records HackInvent's verified BloxSmith versions and test evidence. Only the versions listed above have been verified, using the block-owned suites in a **bundled-block test installation**. This is not a certification of managed-package installation, every browser/OS, or live provider availability. Other framework versions are unverified, not necessarily incompatible.

The block-version badge follows `model.json`, not a published Git tag. `unversioned` means that no block release version is declared; no number is inferred from the framework version. The framework still uses `model.json` for its runtime/install contract; the tester-owned JSON does not replace it. Official integration tests run in the private `bloxmith-blocs` workspace. Test helpers and the proprietary framework are not bundled in this public block repository.

## Properties ergonomics

Modal and inspector styles are owned by this package and scoped to its exact
release. Forms adapt to narrow panels, checkboxes stay beside their labels, and
long values do not widen the inspector. Existing labels are associated with
controls; keyboard navigation complements the block’s own tab handlers.
These presentation helpers do not change port bindings, authored settings,
runtime behavior or the block’s original surface cleanup.
