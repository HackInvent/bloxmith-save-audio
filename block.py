# -----------------------------------------------------------------------------
# Role: Persists one graph-wired continuous audio input as files per stream.
# File Name: block.py
# Author: OpenAI Codex
# Created Date: 2026-09-04
# -----------------------------------------------------------------------------

from __future__ import annotations

from collections.abc import Mapping
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from string import Formatter
from typing import Any, BinaryIO
import os
import json
import re
import time
import uuid

from bloxsmith_app.block_api import (
    BlockDefinition,
    BlockRuntimeContext,
    BlockRuntimeListenerContext,
    BlockRuntimePreparation,
    BlockRuntimePreparationContext,
    BlockRuntimeResult,
    RuntimeAudioFrame,
    RuntimeAudioStreamClient,
    RuntimeAudioStreamError,
    RuntimeListenerError,
    TEXT_PLAIN,
    render_inspector_template,
    render_node_card_template,
    render_path_browser_control,
)


DEFAULT_OUTPUT_DIR = "exports/audio-streams"
DEFAULT_FILENAME_TEMPLATE = "audio_{timestamp}_{stream_id}"
DEFAULT_IDLE_FINALIZE_SEC = 1.5
MIN_IDLE_FINALIZE_SEC = 0.25
MAX_IDLE_FINALIZE_SEC = 30.0
ALLOWED_FILENAME_FIELDS = frozenset({"node_id", "timestamp", "stream_id", "codec"})


class SaveAudioBlockError(ValueError):
    """Raised when Save Audio cannot validate or persist one stream safely."""


@dataclass(slots=True)
class _OpenAudioRecording:
    """Track one temporary file until its explicit stop counts have been received."""

    stream_id: str
    codec: str
    sample_rate_hz: int
    channels: int
    final_path: Path
    temporary_path: Path
    handle: BinaryIO
    started_at: str
    last_activity: float
    first_sequence: int
    last_sequence: int
    frames: int = 0
    bytes_written: int = 0

    def append(self, frame: RuntimeAudioFrame, *, now: float) -> bool:
        """Append one compatible frame and report whether a sequence gap occurred.

        Args:
            frame: Runtime audio frame belonging to this stream.
            now: Monotonic receipt time retained for recording diagnostics.
        """

        if (
            frame.stream_id != self.stream_id
            or frame.codec != self.codec
            or frame.sample_rate_hz != self.sample_rate_hz
            or frame.channels != self.channels
        ):
            raise SaveAudioBlockError("The audio metadata changed within a single stream_id.")
        sequence_gap = self.frames > 0 and frame.sequence != self.last_sequence + 1
        self.handle.write(frame.payload)
        self.frames += 1
        self.bytes_written += len(frame.payload)
        self.last_sequence = frame.sequence
        self.last_activity = now
        return sequence_gap


# Functional behavior:
# FB1 - Consume binary frames only from the fixed graph-wired audio_in port in Active Runtime.
# FB2 - Concatenate ordered MediaRecorder chunks into one atomically finalized file per stream_id.
# FB3 - Infer a useful container extension and build collision-safe names in the configured directory.
# FB4 - Finalize exact stop totals; cancel aborted streams without stopping Run; reject damaged audio.
# FB5 - Validate path/template/timing settings and expose filesystem or transport failures explicitly.
# FB6 - Declare separate audio/data inputs, listen on Run and no-op cleanly in centralized simulation.
# FB7 - Render block-owned node-card, modal, inspector, directory browser, and UI actions.
class SaveAudioBlock(BlockDefinition):
    """Persist each incoming continuous audio session as one project-side file."""

    kind = "save_audio"

    def render_node_card(
        self,
        *,
        node: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render the destination and the latest worker-owned recording status."""

        runtime = (payload or {}).get("runtime") or node.get("runtimeUi") or {}
        result = runtime.get("result") or runtime
        status = str(result.get("last_message") or "Run: listening · start: record · stop: save")
        config = self._config(node.get("config"))
        return render_node_card_template(
            block=self,
            node=node,
            node_classes=["save-audio-node"],
            replacements={
                "title": str(node.get("title") or self.default_title()),
                "output_dir": str(config["output_dir"]),
                "idle_finalize_sec": self._format_seconds(config["idle_finalize_sec"]),
                "recording_status": status[:500],
            },
        )

    def render_modal(
        self,
        *,
        node: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render directory, naming, idle, ports, and runtime information."""

        config = self._config(node.get("config"))
        template = (self.directory / "block_modal.html").read_text(encoding="utf-8")
        template = template.replace(
            "{{ path_browser_html }}",
            self._path_browser(config, input_id=f"{node.get('id') or 'save-audio'}ModalOutputDir"),
        )
        for key, value in self._ui_replacements(config).items():
            template = template.replace(f"{{{{ {key} }}}}", value)
        html = self._render_generic_modal_template(template=template, node=node, payload=payload or {})
        return {
            "html": html,
            "context": {"node_id": str(node.get("id") or ""), "node_kind": self.kind, **config},
        }

    def render_inspector_panel(
        self,
        *,
        node: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render the block-owned Save Audio settings inspector."""

        config = self._config(node.get("config"))
        template = (self.directory / "inspector_panel.html").read_text(encoding="utf-8")
        template = template.replace(
            "{{ path_browser_html }}",
            self._path_browser(config, input_id=f"{node.get('id') or 'save-audio'}InspectorOutputDir"),
        )
        html = render_inspector_template(
            template=template,
            node={**node, "type": self.kind, "kind": self.kind},
            payload=payload,
            replacements=self._ui_replacements(config),
            show_duplicate=True,
        )
        return {
            "html": html,
            "context": {"node_id": str(node.get("id") or ""), "full_panel": True, **config},
        }

    def handle_ui_action(
        self,
        *,
        node: dict[str, Any],
        action: str,
        values: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate block-owned settings edits and return a node config patch.

        Args:
            node: Serialized Save Audio node.
            action: Modal or inspector action name.
            values: Output directory, filename template, and idle timeout.
            payload: Optional generic UI request context.
        """

        if action not in {"modal_update_save_audio", "inspector_update_save_audio"}:
            return super().handle_ui_action(node=node, action=action, values=values, payload=payload)
        candidate = dict(node.get("config") or {})
        candidate.update(
            {
                "output_dir": values.get("output_dir"),
                "filename_template": values.get("filename_template"),
                "idle_finalize_sec": values.get("idle_finalize_sec"),
            }
        )
        try:
            config = self._config(candidate)
        except SaveAudioBlockError as exc:
            return {"error": str(exc)}
        return {
            "node_patch": {"config": config},
            "rerender_inspector": False,
        }

    def prepare_runtime(self, context: BlockRuntimePreparationContext) -> BlockRuntimePreparation:
        """Validate fixed ports and opt into one listener after Run service readiness."""
        self._validate_port_contract(context)
        self._config(context.config)
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode == "zeromq_active")

    def execute_runtime(self, context: BlockRuntimeContext) -> BlockRuntimeResult:
        """Forward explicit data commands; the listener alone owns audio and files."""
        try:
            self._validate_port_contract(context)
            self._config(context.config)
            if context.runtime_mode != "zeromq_active":
                return BlockRuntimeResult(
                    status="skipped", last_message="Recording is available in Active Runtime only.",
                    metadata={"save_audio": {"saved_files": [], "state": "simulation"}},
                )
            raw = context.input_value("command_in")
            if raw is None or raw == "":
                return BlockRuntimeResult(status="skipped", last_message="Listening on command_in.")
            command = self._validate_command(raw)
            sender = context.services.get("runtime_listener")
            if sender is None:
                raise SaveAudioBlockError("The Save Audio listener is not loaded: Stop, then Run.")
            sender.send(command)
            return BlockRuntimeResult(
                last_message=f"Command {command['action']} forwarded to the audio listener.",
                logs=[f"[save-audio-command] {context.node_id}: {command['action']}."],
            )
        except (ValueError, RuntimeListenerError) as exc:
            return self._failure(context, str(exc))

    @staticmethod
    def _validate_command(raw: Any) -> dict[str, Any]:
        """Validate stream-scoped start/stop JSON, including final integrity counts."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError as exc:
                raise SaveAudioBlockError("command_in attend un objet JSON start/stop.") from exc
        if (not isinstance(raw, Mapping) or not isinstance(raw.get("action"), str)
                or raw["action"] not in {"start", "stop"}):
            raise SaveAudioBlockError("command_in expects a start or stop action.")
        stream_id = raw.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id.strip() or len(stream_id) > 128:
            raise SaveAudioBlockError("The command must identify a valid stream_id.")
        command = {"action": raw["action"], "stream_id": stream_id}
        if command["action"] == "stop":
            for key in ("frame_count", "byte_count"):
                value = raw.get(key)
                if type(value) is not int or not 0 <= value <= 9_007_199_254_740_991:
                    raise SaveAudioBlockError(f"Invalid final counter: {key}.")
                command[key] = value
            if type(raw.get("aborted", False)) is not bool:
                raise SaveAudioBlockError("The aborted field must be a boolean.")
            command["aborted"] = raw.get("aborted", False)
        return command

    def listen_runtime(self, context: BlockRuntimeListenerContext) -> None:
        """Listen from Run, record armed streams and finalize only on data stop.

        A bounded five-second pre-start buffer handles independent transport order.
        Stop totals include the final MediaRecorder chunk. idle_finalize_sec bounds
        post-stop draining, not silence. State stays local, never on shared self.
        An aborted producer stop discards only its stream and retires late data/audio;
        cancellation is visible but nonfatal, including before start or the first frame.
        Runtime Stop cancels partial files and preserves completed recordings.
        """
        sessions: dict[str, dict[str, Any]] = {}
        pending: deque[tuple[float, RuntimeAudioFrame]] = deque()
        retired: deque[str] = deque(maxlen=128)
        pending_bytes = 0
        saved_files: list[dict[str, Any]] = []
        frames_received = 0
        bytes_written = 0

        def emit(state: str, message: str, *, stream_id: str = "") -> None:
            """Publish progress and optional affected stream identity through the worker, never audio."""
            context.emit_result(BlockRuntimeResult(
                last_message=message, worker_received=message,
                logs=[f"[save-audio] {context.node_id}: {message}"],
                metadata={"save_audio": {
                    "state": state, "saved_files": saved_files[-20:],
                    "frames_received": frames_received, "bytes_written": bytes_written,
                    **({"stream_id": stream_id} if stream_id else {}),
                }},
            ))

        def append(session: dict[str, Any], frame: RuntimeAudioFrame, now: float) -> None:
            """Write one frame, validating format and per-session sequence continuity."""
            nonlocal frames_received, bytes_written
            first = session["recording"] is None
            if first:
                session["recording"] = self._open_recording(
                    output_dir=output_dir, node_id=context.node_id, config=config, frame=frame, now=now,
                )
            if session["recording"].append(frame, now=now):
                raise SaveAudioBlockError("Incomplete stream: audio frames lost or reordered.")
            frames_received += 1
            bytes_written += len(frame.payload)
            if first:
                emit("recording", "Audio recording in progress.")

        try:
            config = self._config(context.config)
            output_dir = self._resolve_output_dir(context.root_dir, config["output_dir"])
            client = context.services.get("runtime_audio_streams")
            if not isinstance(client, RuntimeAudioStreamClient) or not client.available:
                raise SaveAudioBlockError("Wire audio_in to a compatible active audio output.")
            while not context.stop_requested():
                now = time.monotonic()
                while pending and now - pending[0][0] > 5.0:
                    pending_bytes -= len(pending.popleft()[1].payload)
                received = context.receive_command(timeout_sec=0)
                if received is not None:
                    command = self._validate_command(received.payload)
                    stream_id = command["stream_id"]
                    if stream_id not in retired:
                        if command["action"] == "stop" and command["aborted"]:
                            # Producers such as TTS may stop intentionally during barge-in. Retire
                            # only that stream, even if start/audio are late, and keep the listener.
                            session = sessions.get(stream_id)
                            if session and session["recording"] is not None:
                                self._abort_recording(session["recording"], strict=True)
                            sessions.pop(stream_id, None)
                            remaining = deque()
                            for arrived, frame in pending:
                                if frame.stream_id == stream_id:
                                    pending_bytes -= len(frame.payload)
                                else:
                                    remaining.append((arrived, frame))
                            pending = remaining
                            retired.append(stream_id)
                            emit("cancelled", "Audio stream cancelled by the source; recording dropped. "
                                 "Listening for the next streams.", stream_id=stream_id)
                        elif command["action"] == "start" and stream_id not in sessions:
                            if len(sessions) >= 4:
                                raise SaveAudioBlockError("Too many concurrent audio sessions (maximum 4).")
                            sessions[stream_id] = {"recording": None, "stop": None, "deadline": None}
                            emit("armed", "Start received; waiting for the audio stream.")
                            # Audio may precede start on the independent message link.
                            remaining = deque()
                            for arrived, frame in pending:
                                if frame.stream_id == stream_id:
                                    append(sessions[stream_id], frame, now)
                                    pending_bytes -= len(frame.payload)
                                else:
                                    remaining.append((arrived, frame))
                            pending = remaining
                        elif command["action"] == "stop":
                            if stream_id not in sessions:
                                raise SaveAudioBlockError("Stop received without a start for this stream.")
                            session = sessions[stream_id]
                            if session["stop"] is not None and session["stop"] != command:
                                raise SaveAudioBlockError("Conflicting stop counters for the same stream.")
                            if session["stop"] is None:
                                session["stop"] = command
                                session["deadline"] = now + config["idle_finalize_sec"]

                frame = client.receive_port("audio_in", timeout_sec=0.05)
                now = time.monotonic()
                if frame is not None and frame.stream_id not in retired:
                    if frame.stream_id in sessions:
                        append(sessions[frame.stream_id], frame, now)
                    else:
                        pending.append((now, frame))
                        pending_bytes += len(frame.payload)
                        # A forgotten data edge must never cause unbounded audio buffering.
                        while len(pending) > 128 or pending_bytes > 8 * 1024 * 1024:
                            pending_bytes -= len(pending.popleft()[1].payload)

                for stream_id, session in list(sessions.items()):
                    stop = session["stop"]
                    if stop is None:
                        continue
                    recording = session["recording"]
                    count = recording.frames if recording else 0
                    size = recording.bytes_written if recording else 0
                    if count > stop["frame_count"] or size > stop["byte_count"]:
                        raise SaveAudioBlockError("Stream inconsistent with the stop counters.")
                    if count == stop["frame_count"] and size == stop["byte_count"]:
                        if recording:
                            saved_files.append(self._finalize_recording(recording, context.root_dir))
                            saved_files[:] = saved_files[-20:]
                            session["recording"] = None
                            emit("saved", f"File saved: {saved_files[-1]['path']}")
                        else:
                            emit("idle", "Empty capture: no file created.")
                        del sessions[stream_id]
                        retired.append(stream_id)
                    elif now >= session["deadline"]:
                        raise SaveAudioBlockError(
                            f"Incomplete stream after stop: {count}/{stop['frame_count']} frames, "
                            f"{size}/{stop['byte_count']} bytes received."
                        )
        except (ValueError, OSError, RuntimeAudioStreamError) as exc:
            if not context.stop_requested():
                result = self._failure(context, str(exc))
                result.metadata = {"save_audio": {"state": "error", "saved_files": saved_files[-20:]}}
                context.emit_result(result)
                # Wait for supervision to revoke the listener after the failed result.
                while not context.stop_requested():
                    context.receive_command(timeout_sec=0.05)
        finally:
            for session in sessions.values():
                if session["recording"] is not None:
                    self._abort_recording(session["recording"])

    def _config(self, raw_config: Mapping[str, Any] | None) -> dict[str, Any]:
        """Normalize and validate all settings before any filesystem side effect."""

        source = raw_config if isinstance(raw_config, Mapping) else {}
        output_dir = str(source.get("output_dir") or DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR
        filename_template = str(source.get("filename_template") or DEFAULT_FILENAME_TEMPLATE).strip()
        if len(output_dir) > 1_024:
            raise SaveAudioBlockError("The output directory exceeds 1,024 characters.")
        if not filename_template or len(filename_template) > 180:
            raise SaveAudioBlockError("The name template must contain between 1 and 180 characters.")
        self._validate_filename_template(filename_template)
        try:
            idle_finalize_sec = float(source.get("idle_finalize_sec", DEFAULT_IDLE_FINALIZE_SEC))
        except (TypeError, ValueError):
            idle_finalize_sec = DEFAULT_IDLE_FINALIZE_SEC
        idle_finalize_sec = max(MIN_IDLE_FINALIZE_SEC, min(MAX_IDLE_FINALIZE_SEC, idle_finalize_sec))
        return {
            "output_dir": output_dir,
            "filename_template": filename_template,
            "idle_finalize_sec": idle_finalize_sec,
        }

    @staticmethod
    def _validate_filename_template(filename_template: str) -> None:
        """Allow only simple documented placeholders in the configured file stem."""

        try:
            fields = tuple(Formatter().parse(filename_template))
        except ValueError as exc:
            raise SaveAudioBlockError("The file name template contains invalid braces.") from exc
        for _literal, field_name, format_spec, conversion in fields:
            if field_name is None:
                continue
            if field_name not in ALLOWED_FILENAME_FIELDS or format_spec or conversion:
                allowed = ", ".join(sorted(ALLOWED_FILENAME_FIELDS))
                raise SaveAudioBlockError(f"Invalid name placeholder. Allowed values: {allowed}.")

    @staticmethod
    def _resolve_output_dir(root_dir: Path, configured_dir: str) -> Path:
        """Resolve a user-selected directory relative to the runtime root when needed."""

        path = Path(str(configured_dir or DEFAULT_OUTPUT_DIR)).expanduser()
        resolved = path.resolve() if path.is_absolute() else (root_dir.resolve() / path).resolve()
        if resolved.exists() and not resolved.is_dir():
            raise SaveAudioBlockError(f"The output directory points to a file: {resolved}")
        return resolved

    def _open_recording(
        self,
        *,
        output_dir: Path,
        node_id: str,
        config: Mapping[str, Any],
        frame: RuntimeAudioFrame,
        now: float,
    ) -> _OpenAudioRecording:
        """Create a collision-safe temporary file for the first frame of a stream."""

        output_dir.mkdir(parents=True, exist_ok=True)
        extension = self._audio_extension(frame.payload, frame.codec)
        timestamp = datetime.fromtimestamp(frame.timestamp_ms / 1_000, tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        raw_stem = str(config["filename_template"]).format_map(
            {
                "node_id": node_id,
                "timestamp": timestamp,
                "stream_id": frame.stream_id,
                "codec": frame.codec,
            }
        )
        stem = self._safe_filename_stem(raw_stem)
        final_path = self._reserve_unique_target(output_dir, stem, extension)
        temporary_path = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.part")
        try:
            handle = temporary_path.open("xb")
        except OSError as exc:
            final_path.unlink(missing_ok=True)
            raise SaveAudioBlockError(f"Unable to create the temporary audio file: {exc}") from exc
        return _OpenAudioRecording(
            stream_id=frame.stream_id,
            codec=frame.codec,
            sample_rate_hz=frame.sample_rate_hz,
            channels=frame.channels,
            final_path=final_path,
            temporary_path=temporary_path,
            handle=handle,
            started_at=datetime.now(timezone.utc).isoformat(),
            last_activity=now,
            first_sequence=frame.sequence,
            last_sequence=frame.sequence - 1,
        )

    def _finalize_recording(self, recording: _OpenAudioRecording, root_dir: Path) -> dict[str, Any]:
        """Flush and atomically promote one completed temporary recording."""

        try:
            recording.handle.flush()
            os.fsync(recording.handle.fileno())
            recording.handle.close()
            os.replace(recording.temporary_path, recording.final_path)
        except OSError as exc:
            self._abort_recording(recording)
            raise SaveAudioBlockError(f"The audio file could not be finalized: {exc}") from exc
        return {
            "path": self._display_path(recording.final_path, root_dir),
            "absolute_path": str(recording.final_path),
            "stream_id": recording.stream_id,
            "codec": recording.codec,
            "sample_rate_hz": recording.sample_rate_hz,
            "channels": recording.channels,
            "frames": recording.frames,
            "bytes": recording.bytes_written,
            "first_sequence": recording.first_sequence,
            "last_sequence": recording.last_sequence,
            "started_at": recording.started_at,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _abort_recording(recording: _OpenAudioRecording, *, strict: bool = False) -> None:
        """Discard an unfinished recording and its reserved target, never a completed file.

        Explicit producer cancellation uses strict cleanup so filesystem errors stay visible.
        Shutdown/failure cleanup remains best-effort; all three operations are attempted either way.
        """

        errors = []
        try:
            recording.handle.close()
        except OSError as exc:
            errors.append(exc)
        try:
            recording.temporary_path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(exc)
        try:
            recording.final_path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(exc)
        if strict and errors:
            raise SaveAudioBlockError(f"The cancelled recording could not be cleaned up: {errors[0]}") from errors[0]

    @staticmethod
    def _audio_extension(payload: bytes, codec: str) -> str:
        """Infer the encoded container from its first bytes with codec fallbacks."""

        if payload.startswith(b"\x1aE\xdf\xa3"):
            return ".webm"
        if payload.startswith(b"OggS"):
            return ".ogg"
        if payload.startswith(b"RIFF") and payload[8:12] == b"WAVE":
            return ".wav"
        if payload.startswith(b"fLaC"):
            return ".flac"
        if len(payload) >= 12 and payload[4:8] == b"ftyp":
            return ".m4a"
        if payload.startswith(b"ID3") or (len(payload) >= 2 and payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0):
            return ".mp3"
        return {"opus": ".webm", "aac": ".m4a", "pcm_s16le": ".pcm"}.get(codec, ".audio")

    @staticmethod
    def _safe_filename_stem(raw_stem: str) -> str:
        """Collapse separators and unsafe characters into one portable file stem."""

        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw_stem or "")).strip(" ._-")
        stem = stem[:180].rstrip(" ._-")
        return stem or "audio_stream"

    @staticmethod
    def _reserve_unique_target(output_dir: Path, stem: str, extension: str) -> Path:
        """Reserve a new target atomically so concurrent blocks cannot overwrite it."""

        for index in range(10_000):
            suffix = "" if index == 0 else f"_{index}"
            candidate = output_dir / f"{stem}{suffix}{extension}"
            try:
                candidate.open("xb").close()
            except FileExistsError:
                continue
            except OSError as exc:
                raise SaveAudioBlockError(f"Unable to reserve the audio file: {exc}") from exc
            else:
                return candidate
        raise SaveAudioBlockError("Unable to choose a unique audio name after 10,000 attempts.")

    @staticmethod
    def _display_path(path: Path, root_dir: Path) -> str:
        """Prefer a runtime-root-relative path while preserving external directories."""

        try:
            return str(path.relative_to(root_dir.resolve()))
        except ValueError:
            return str(path)

    def _path_browser(self, config: Mapping[str, Any], *, input_id: str) -> str:
        """Render the public shared directory browser for this block-owned surface."""

        return render_path_browser_control(
            input_id=input_id,
            label="Output directory",
            value=str(config["output_dir"]),
            placeholder=DEFAULT_OUTPUT_DIR,
            input_attrs="data-save-audio-output-dir",
            select_mode="directory",
            status="Choose the folder that will receive one file per audio session.",
            use_current_label="Save into this folder",
        )

    @staticmethod
    def _ui_replacements(config: Mapping[str, Any]) -> dict[str, str]:
        """Return escaped settings used by modal and inspector templates."""

        return {
            "filename_template": escape(str(config["filename_template"]), quote=True),
            "idle_finalize_sec": SaveAudioBlock._format_seconds(config["idle_finalize_sec"]),
        }

    @staticmethod
    def _format_seconds(value: Any) -> str:
        """Render a normalized timeout without unnecessary trailing zeros."""

        return f"{float(value):g}"

    @staticmethod
    def _saved_log(node_id: str, metadata: Mapping[str, Any], *, reason: str) -> str:
        """Build one bounded completion log without including binary payloads."""

        return (
            f"[save-audio] {node_id}: {metadata.get('path')} finalized after {reason} "
            f"({metadata.get('frames')} trame(s), {metadata.get('bytes')} octets)."
        )

    @staticmethod
    def _validate_port_contract(context: Any) -> None:
        """Reject graph nodes that alter the fixed Save Audio sink shape."""

        inputs = tuple(getattr(context, "input_ports", ()) or ())
        outputs = tuple(getattr(context, "output_ports", ()) or ())
        if len(inputs) != 2 or outputs:
            raise ValueError("Save Audio requires audio_stream audio_in and message command_in; recreate legacy nodes.")
        ports = {getattr(port, "id", 0): port for port in inputs}
        audio_input = ports.get(1)
        if int(getattr(audio_input, "id", 0) or 0) != 1 or str(getattr(audio_input, "name", "") or "") != "audio_in":
            raise ValueError("Save Audio input must remain port 1 named audio_in.")
        if str(getattr(audio_input, "transport", "") or "") != "audio_stream":
            raise ValueError("Save Audio audio_in must use transport audio_stream.")
        if bool(getattr(audio_input, "required", False)):
            raise ValueError("Save Audio audio_in must remain non-required.")
        requirement = str(getattr(audio_input, "execution_requirement", "") or "")
        if requirement != "not_required_for_execution":
            raise ValueError("Save Audio audio_in must not participate in message execution readiness.")
        command = ports.get(2)
        if getattr(command, "name", "") != "command_in" or getattr(command, "transport", "message") != "message":
            raise ValueError("Save Audio command_in must be message port 2.")
        if getattr(command, "multiplicity", "") != "one" or not getattr(command, "required", False):
            raise ValueError("Save Audio command_in must be required with multiplicity one.")

    @staticmethod
    def _failure(context: BlockRuntimeContext | BlockRuntimeListenerContext, message: str) -> BlockRuntimeResult:
        """Return one explicit Save Audio runtime failure result."""

        return BlockRuntimeResult(
            status="failed",
            outputs=[],
            logs=[f"[save-audio-error] {context.node_id}: {message}"],
            error=message,
            exit_code=1,
            last_message=message,
            content_type=TEXT_PLAIN,
            worker_received="-",
        )
