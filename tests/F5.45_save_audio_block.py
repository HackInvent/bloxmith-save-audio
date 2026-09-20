#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Role: Verifies Save Audio persistence from direct and browser-ingress streams.
# File Name: F5.45_save_audio_block.py
# Author: OpenAI Codex
# Created Date: 2026-09-04
# -----------------------------------------------------------------------------

"""F5.45 - Save Audio contract, atomic files, UI, and both runtimes."""

# Test cases:
# - FB1/FB2 - Consume audio_in and concatenate ordered chunks per stream_id.
# - FB3 - Detect containers, sanitize names, and preserve existing targets.
# - FB4 - Finalize on data stop after draining, reject incomplete streams, and cancel partial files at runtime Stop.
# - FB5 - Validate settings and surface transport/filesystem failures.
# - FB6 - Enforce explicit audio/data inputs, centralized no-op, and listening without Play.
# - FB7 - Render and serve the block-owned directory browser, UI assets, and actions.

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import urlopen
import base64
import json
import os
import socket
import sys
import tempfile
import time


ROOT_DIR = Path(__file__).resolve().parents[3]
TESTS_DIR = ROOT_DIR / "tests"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from blocs.microphone_stream.block import MicrophoneStreamBlock  # noqa: E402
from blocs.save_audio.block import SaveAudioBlock, SaveAudioBlockError  # noqa: E402
from bloxsmith_app.active_runtime.listener import RuntimeListenerHost
from bloxsmith_app.block_runtime import BlockRuntimeContext, BlockRuntimePreparation, BlockRuntimePreparationContext  # noqa: E402
from bloxsmith_app.runtime_audio_streams import (  # noqa: E402
    RuntimeAudioStreamBinding,
    RuntimeAudioStreamPortRoute,
    RuntimeAudioStreamService,
)
from ui_smoke_common import (  # noqa: E402
    create_project_api,
    create_run_api,
    expect,
    get_run_api,
    graph_payload,
    http_json,
    isolated_server,
    play_run_api,
    stop_run_api,
    wait_for_run_predicate,
    wait_for_run_terminal,
)
from urllib.parse import quote
from block_test_packages import install_test_package, release_key, surface_payload


WEBM_HEADER = b"\x1aE\xdf\xa3webm-header"


def audio_input() -> SimpleNamespace:
    """Build the fixed input shape exposed to direct runtime contexts."""

    return SimpleNamespace(
        id=1,
        name="audio_in",
        transport="audio_stream",
        required=False,
        execution_requirement="not_required_for_execution",
        multiplicity="one",
        audio_stream=SimpleNamespace(codecs=("opus", "aac"), sample_rates_hz=(), channels=(1, 2)),
    )


def save_context(
    root_dir: Path,
    *,
    runtime_mode: str,
    services: dict | None = None,
    config: dict | None = None,
) -> BlockRuntimeContext:
    """Build one direct Save Audio runtime context."""

    return BlockRuntimeContext(
        run_id="run-save-audio-unit",
        node_id="save-audio-unit",
        kind="save_audio",
        title="Save Audio",
        config={
            "output_dir": "exports/audio-test",
            "filename_template": "capture_{stream_id}",
            "idle_finalize_sec": 0.25,
            **(config or {}),
        },
        input_ports=(audio_input(), SimpleNamespace(id=2, name="command_in", transport="message", required=True,
                     execution_requirement="required_for_execution", multiplicity="one")),
        output_ports=(),
        runtime_mode=runtime_mode,
        services=services,
        root_dir=root_dir,
    )


def audio_document(output_dir: str = "exports/save-audio-e2e") -> dict:
    """Return a graph wiring Microphone Stream to Save Audio."""

    microphone = MicrophoneStreamBlock().build_node_payload(
        node_id="microphone-1",
        title="Microphone Stream",
        position={"x": 80, "y": 120},
    )
    save = SaveAudioBlock().build_node_payload(
        node_id="save-audio-1",
        title="Save Audio",
        position={"x": 420, "y": 120},
        config_overrides={
            "output_dir": output_dir,
            "filename_template": "browser_{stream_id}",
            "idle_finalize_sec": 0.25,
        },
    )
    return graph_payload(
        "F5.45 Save Audio",
        [microphone, save],
        [
            {
                "id": "edge-microphone-save",
                "from": {"node": "microphone-1", "port": 1},
                "to": {"node": "save-audio-1", "port": 1},
                "kind": "data",
            },
            {"id": "edge-microphone-command", "from": {"node": "microphone-1", "port": 2},
             "to": {"node": "save-audio-1", "port": 2}, "kind": "data"},
        ],
    )


def test_model_preparation_validation_and_simulation() -> None:
    """Validate FB5/FB6 release declarations, model invariants, settings and simulation."""

    block = SaveAudioBlock()
    expect(block.model["version"] == "0.1.0", "Save Audio declares the shared initial release version.")
    expect(block.model["tested_with_bloxsmith"] == (ROOT_DIR / "VERSION").read_text().strip(),
           "The tested BloxSmith version must match the actual framework release.")
    expect(block.model["bloxsmith_compatibility"] == [block.model["tested_with_bloxsmith"]],
           "Do not claim compatibility with untested framework versions.")
    inputs = block.model["ports"]["inputs"]
    expect(len(inputs) == 2 and block.model["ports"]["outputs"] == [], "Save Audio must expose audio and data inputs, no output.")
    port = inputs[0]
    expect(port["name"] == "audio_in" and port["transport"] == "audio_stream", "The input must be audio_in/audio_stream.")
    expect(port["multiplicity"] == "one", "A continuous audio input must have multiplicity one.")
    expect(port["required"] is False, "Audio arrival must not be a required message input.")
    expect(port["execution_requirement"] == "not_required_for_execution", "Audio must not drive classic readiness.")
    expect(block.model["runtime"]["supports_runtime_audio_streams"] is True, "The generic audio runner capability is required.")

    with tempfile.TemporaryDirectory() as temporary:
        root_dir = Path(temporary)
        context = save_context(root_dir, runtime_mode="centralized")
        preparation = block.prepare_runtime(context)
        expect(not preparation.keep_alive, "Simulation must not retain an audio receiver.")
        result = block.execute_runtime(context)
        expect(result.status == "skipped", "Save Audio simulation must no-op explicitly.")
        expect(not (root_dir / "exports/audio-test").exists(), "Simulation must not create a destination directory.")

    node = block.build_node_payload(node_id="save-audio-config")
    applied = block.handle_ui_action(
        node=node,
        action="inspector_update_save_audio",
        values={
            "output_dir": "exports/records",
            "filename_template": "take_{node_id}_{codec}",
            "idle_finalize_sec": 99,
        },
    )
    patch = applied["node_patch"]["config"]
    expect(patch["idle_finalize_sec"] == 30.0, "Idle finalization must be bounded to 30 seconds.")
    expect(patch["output_dir"] == "exports/records", "The selected directory must be preserved.")
    invalid = block.handle_ui_action(
        node=node,
        action="modal_update_save_audio",
        values={"output_dir": "exports", "filename_template": "take_{unknown}", "idle_finalize_sec": 1},
    )
    expect("placeholder" in str(invalid.get("error") or "").lower(), "Unknown filename placeholders must fail before writing.")
    try:
        block._config({"filename_template": "{"})
    except SaveAudioBlockError:
        pass
    else:
        raise AssertionError("Malformed braces must be rejected before any side effect.")

    invalid_context = save_context(ROOT_DIR, runtime_mode="centralized")
    invalid_context.input_ports = (
        SimpleNamespace(
            id=1,
            name="audio_in",
            transport="message",
            required=False,
            execution_requirement="not_required_for_execution",
        ),
        invalid_context.input_ports[1],
    )
    failed = block.execute_runtime(invalid_context)
    expect(failed.status == "failed" and "audio_stream" in failed.error, "A mutated message input must be rejected.")


def wait_until(predicate, message, timeout=3):
    """Wait only for asynchronous observations, within a short test deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


@contextmanager
def active_recorder(root_dir):
    """Use the real audio service and supervised listener without Play."""
    block = SaveAudioBlock()
    topic = "graph-port/save-audio-test"
    service = RuntimeAudioStreamService(run_id="run-save-audio-unit")
    service.register_preparations(
        {"save-audio-unit": BlockRuntimePreparation()},
        additional_bindings={"save-audio-unit": (RuntimeAudioStreamBinding(
            topic=topic, subscribe=True, codecs=("opus", "aac"), channels=(1, 2),
        ),)},
    )
    service.open()
    receiver = service.client_for("save-audio-unit", port_routes=(RuntimeAudioStreamPortRoute(
        port_id=1, port_name="audio_in", direction="input", topic=topic,
        codecs=("opus", "aac"), channels=(1, 2),
    ),))
    publisher = service.external_client("test-browser", (
        RuntimeAudioStreamBinding(topic=topic, publish=True, codecs=("opus", "aac"), channels=(1, 2)),
    ))
    context = save_context(root_dir, runtime_mode="zeromq_active", services={"runtime_audio_streams": receiver})
    expect(block.prepare_runtime(context).listen_on_run, "Save Audio must listen from Run.")
    gate = Event()
    gate.set()
    host = RuntimeListenerHost(
        context=BlockRuntimePreparationContext.from_context(context),
        services=context.services, hook=block.listen_runtime,
        stop_event=Event(), ready_gate=gate, root_dir=root_dir,
    )
    context.services["runtime_listener"] = host.client
    results = []

    def command(action, stream_id, **fields):
        """Test normal execute_runtime forwarding without a blocking receive loop."""
        context.input_attribute("command_in").update({"action": action, "stream_id": stream_id, **fields})
        started = time.monotonic()
        result = block.execute_runtime(context)
        expect(result.status == "success", f"Command forwarding failed: {result}")
        expect(time.monotonic() - started < 0.2, "Data activations must stay nonblocking.")

    def frame(stream_id, payload, **fields):
        """Publish encoded chunks through the actual binary service."""
        publisher.publish(topic, payload, codec="opus", sample_rate_hz=48000,
                          channels=1, stream_id=stream_id, **fields)

    def observed(state):
        """Drain asynchronous results while retaining evidence across polls."""
        results.extend(host.pop_results())
        return any(item.metadata.get("save_audio", {}).get("state") == state for item in results)

    host.start()
    try:
        yield SimpleNamespace(command=command, frame=frame, observed=observed, results=results, host=host)
    finally:
        host.request_stop()
        service.close()
        host.close()
        expect(not host._thread.is_alive(), "Runtime Stop must join the recording listener.")


def test_direct_active_recordings() -> None:
    """FB1/FB2/FB3/FB4: listen on Run, reorder control/audio, drain stop and reuse."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "exports/audio-test"
        with active_recorder(root) as recorder:
            recorder.frame("stream-a", WEBM_HEADER)
            time.sleep(0.1)
            expect(not target.exists(), "Audio alone must not create a file or directory.")
            recorder.command("start", "stream-a")
            wait_until(lambda: recorder.observed("recording"), "Start must consume buffered first audio.")
            time.sleep(0.35)
            expect(list(target.glob(".*.part")), "Inactivity without stop must not finalize the recording.")
            recorder.command("stop", "stream-a", frame_count=2, byte_count=len(WEBM_HEADER + b"-body-a"))
            time.sleep(0.05)
            recorder.frame("stream-a", b"-body-a")
            wait_until(lambda: recorder.observed("saved"), "Stop must drain late audio and save.")
            saved = target / "capture_stream-a.webm"
            expect(saved.read_bytes() == WEBM_HEADER + b"-body-a", "Chunks must remain exact and ordered.")
            recorder.command("stop", "stream-a", frame_count=2, byte_count=len(WEBM_HEADER + b"-body-a"))
            recorder.command("start", "stream-b")
            existing = target / "capture_stream-b.ogg"
            existing.write_bytes(b"existing")
            recorder.frame("stream-b", b"OggS-body")
            recorder.command("stop", "stream-b", frame_count=1, byte_count=len(b"OggS-body"))
            wait_until(lambda: (target / "capture_stream-b_1.ogg").exists()
                       and (target / "capture_stream-b_1.ogg").read_bytes() == b"OggS-body",
                       "The same listener must save a second collision-safe recording.")
            expect(existing.read_bytes() == b"existing", "Never overwrite existing files.")
            expect(not list(target.glob(".*.part")), "Successful stops must finalize every temporary file.")
            recorder.command("start", "empty")
            recorder.command("stop", "empty", frame_count=0, byte_count=0)
            wait_until(lambda: recorder.observed("idle"), "An empty capture must not create an empty file.")
            recorder.command("start", "cancelled")
            recorder.frame("cancelled", WEBM_HEADER)
            wait_until(lambda: bool(list(target.glob(".*cancelled*.part"))), "Cancellation fixture must be recording.")
        expect(not list(target.glob("*cancelled*")), "Runtime Stop must cancel unfinished sessions.")
        expect(saved.read_bytes() == WEBM_HEADER + b"-body-a", "Runtime Stop must preserve saved files.")


def test_incomplete_audio_and_validation() -> None:
    """FB4/FB5: malformed commands and non-aborted incomplete streams remain fatal."""
    block = SaveAudioBlock()
    for raw in ({}, {"action": []}, {"action": "start"}, {"action": "stop", "stream_id": "s", "frame_count": True, "byte_count": 1},
                {"action": "stop", "stream_id": "s", "frame_count": 1, "byte_count": 1, "aborted": "no"}):
        try:
            block._validate_command(raw)
        except SaveAudioBlockError:
            pass
        else:
            raise AssertionError(f"Malformed command accepted: {raw}")
    for mode in ("missing", "gap"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with active_recorder(root) as recorder:
                recorder.command("start", "broken")
                recorder.frame("broken", WEBM_HEADER)
                if mode == "gap":
                    recorder.frame("broken", b"gap", sequence=3)
                else:
                    recorder.command("stop", "broken", frame_count=2,
                                     byte_count=len(WEBM_HEADER) + 4, aborted=False)
                wait_until(lambda: recorder.observed("error"), "Broken capture must produce a visible error.")
                expect(any(item.status == "failed" for item in recorder.results), "Incomplete files cannot report success.")
            expect(not list((root / "exports/audio-test").glob("*")), "Failed recordings must leave no final or temporary file.")


def test_owned_ui_api() -> None:
    """Validate FB7 rendering, path browser, assets, and durable config action."""

    block = SaveAudioBlock()
    node = block.build_node_payload(node_id="save-audio-ui")
    expect("exports/audio-streams" in block.render_node_card(node=node)["html"], "Card must show the target directory.")
    live = block.render_node_card(node=node, payload={"runtime": {"result": {"last_message": "File <saved>"}}})
    expect("File &lt;saved&gt;" in live["html"], "Card must display and escape the live recording status.")
    expect("data-path-browser" in block.render_modal(node=node)["html"], "Modal must render the directory browser.")
    expect("data-save-audio-apply" in block.render_inspector_panel(node=node)["html"], "Inspector must own its apply action.")

    with isolated_server() as server:
        # Surfaces are release assets: a bundled kind serves none of them.
        model = install_test_package(server, "save_audio")
        key = quote(release_key(model), safe="")
        served = lambda payload, suffix: next(
            asset["path"] for asset in payload["assets"] if asset["path"].endswith(suffix))
        rendered = surface_payload(server, model, node, "inspector_panel")
        html = str(rendered.get("html") or "")
        expect('data-path-browser-select-mode="directory"' in html, "Save Audio must browse directories, not files.")
        assets = rendered.get("assets") or []
        with urlopen(f"{server.base_url}/api/blocks/{key}/assets/{served(rendered, 'assets/js/common.js')}", timeout=5) as response:
            common_js = response.read().decode("utf-8")
        expect("inspector_update_save_audio" in common_js, "The asset must call a Save Audio-owned action.")
        expect("runtimeAudioStreams" not in common_js, "The settings surface must not implement the audio data plane.")

        applied = http_json(
            server.base_url,
            "/api/blocks/save_audio/ui-action",
            method="POST",
            payload={
                "node": node,
                "action": "inspector_update_save_audio",
                "values": {
                    "output_dir": "exports/api-audio",
                    "filename_template": "api_{timestamp}",
                    "idle_finalize_sec": 0.1,
                },
            },
        )
        config = applied["node_patch"]["config"]
        expect(config["idle_finalize_sec"] == 0.25, "API action must apply the documented lower bound.")
        expect(config["filename_template"] == "api_{timestamp}", "API action must preserve a valid template.")


def _send_masked(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Send one final browser-style masked RFC 6455 frame."""

    header = bytearray([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header.append(0x80 | size)
    elif size <= 0xFFFF:
        header.extend((0x80 | 126, (size >> 8) & 0xFF, size & 0xFF))
    else:
        header.append(0x80 | 127)
        header.extend(size.to_bytes(8, "big"))
    mask = os.urandom(4)
    body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + mask + body)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly one server frame payload size."""

    body = bytearray()
    while len(body) < size:
        chunk = sock.recv(size - len(body))
        if not chunk:
            raise AssertionError("WebSocket closed before a complete server frame.")
        body.extend(chunk)
    return bytes(body)


def _read_server_json(sock: socket.socket) -> dict:
    """Read one unmasked server text frame as JSON."""

    sock.settimeout(3)
    first, second = _recv_exact(sock, 2)
    size = second & 0x7F
    if size == 126:
        size = int.from_bytes(_recv_exact(sock, 2), "big")
    elif size == 127:
        size = int.from_bytes(_recv_exact(sock, 8), "big")
    expect((first & 0x0F) == 1, f"Unexpected server WebSocket opcode: {first & 0x0F}")
    return json.loads(_recv_exact(sock, size).decode("utf-8"))


def _websocket(base_url: str, path: str) -> socket.socket:
    """Open a raw same-origin WebSocket to the isolated server."""

    parsed = urlsplit(base_url)
    host = str(parsed.hostname or "127.0.0.1")
    port = int(parsed.port or 80)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock = socket.create_connection((host, port), timeout=3)
    sock.sendall(request.encode("ascii"))
    response = sock.recv(2_048).decode("latin1", errors="replace")
    expect(response.startswith("HTTP/1.1 101 "), f"WebSocket upgrade failed: {response!r}")
    return sock


def test_graph_browser_ingress_end_to_end(*, play_sources: bool = False) -> None:
    """Verify FB1/FB2/FB4/FB6 for browser recording in an isolated blueprint.

    Args:
        play_sources: Release the legacy source wave when True. False tests
            the editor's Run-only lifecycle without manual block activation.
    """

    output_dir = "exports/save-audio-e2e"
    document = audio_document(output_dir)
    with isolated_server() as server:
        centralized_created = create_run_api(server, document, runtime_mode="centralized")
        centralized = wait_for_run_terminal(server, str(centralized_created["run_id"]), timeout_sec=15)
        expect(centralized.get("status") == "success", "Centralized audio edges must stay non-blocking.")
        expect(not centralized.get("results", {}).get("save-audio-1", {}).get("save_audio", {}).get("saved_files"),
               "Simulation must neither start a listener nor record a file.")
        expect(not (server.root_dir / output_dir).exists(), "Simulation must not create audio files.")

        created_project = create_project_api(server, title="Save Audio E2E", document=document)
        project = created_project["project"]
        graph_id = str(project.get("graph_id") or project.get("project_id") or "")
        workspace_project_id = str(project.get("workspace_project_id") or "1")
        expect(graph_id, f"Created blueprint has no graph id: {project}")
        prepared = http_json(
            server.base_url,
            f"/api/projects/{graph_id}/runs/prepare",
            method="POST",
            payload={"runtime_mode": "zeromq_active"},
        )
        run_id = str(prepared.get("run_id") or "")
        expect(run_id, f"Active project run did not prepare: {prepared}")
        if play_sources:
            play_run_api(server, run_id)
            wait_for_run_predicate(
                server,
                run_id,
                lambda state: state.get("status") == "running" and state.get("node_statuses", {}).get("microphone-1") == "success",
                "Microphone source did not become ready before browser ingress.",
                timeout_sec=8,
            )
        else:
            expect(prepared.get("status") == "prepared", "Run must prepare the audio service without Play.")

        ingress_path = (
            f"/api/projects/{workspace_project_id}/graphs/{graph_id}/instances/1/runs/"
            f"{run_id}/audio-stream-ingress"
        )
        descriptor = http_json(
            server.base_url,
            ingress_path,
            method="POST",
            payload={
                "node_id": "microphone-1",
                "output_port": "audio_out",
                "codec": "opus",
                "sample_rate_hz": 48_000,
                "channels": 1,
            },
        )
        sock = _websocket(server.base_url, descriptor["ws_path"])
        def command(action, **fields):
            """Use the microphone UI action and its explicit graph data output."""
            response = http_json(
                server.base_url, "/api/blocks/microphone_stream/ui-action", method="POST",
                payload={
                    # This existing UI endpoint still names its graph scope project_id.
                    "project_id": graph_id, "node": document["nodes"][0],
                    "action": "publish_capture_command",
                    "values": {"action": action, "stream_id": descriptor["stream_id"], **fields},
                },
            )
            expect(response.get("active_runtime_actions_result", {}).get("ok"),
                   f"Microphone command publication failed: {response}")

        try:
            _send_masked(
                sock,
                0x1,
                json.dumps({"type": "runtime_audio_stream.attach", "ticket": descriptor["ticket"]}).encode("utf-8"),
            )
            ready = _read_server_json(sock)
            expect(ready.get("type") == "runtime_audio_stream.ready", f"Ingress did not become ready: {ready}")
            command("start")
            _send_masked(sock, 0x2, WEBM_HEADER)
            wait_for_run_predicate(
                server, run_id,
                lambda state: state.get("results", {}).get("save-audio-1", {}).get("save_audio", {}).get("state") == "recording",
                "Save Audio must start recording without Play.", timeout_sec=4,
            )
            time.sleep(0.35)
            expect(list((server.root_dir / output_dir).glob(".*.part")),
                   "A pause in audio must not replace the explicit stop command.")
            # Stop can arrive before the final chunk on the separate audio transport.
            command("stop", frame_count=2, byte_count=len(WEBM_HEADER + b"-browser-body"))
            _send_masked(sock, 0x2, b"-browser-body")
            _send_masked(sock, 0x1, json.dumps({"type": "runtime_audio_stream.stop"}).encode("utf-8"))
            stopped = _read_server_json(sock)
            expect(stopped.get("type") == "runtime_audio_stream.stopped", f"Ingress stop was not acknowledged: {stopped}")
        finally:
            sock.close()

        target_dir = server.root_dir / output_dir
        deadline = time.time() + 5
        saved_paths: list[Path] = []
        while time.time() < deadline:
            saved_paths = list(target_dir.glob("*.webm")) if target_dir.exists() else []
            if saved_paths and saved_paths[0].read_bytes() == WEBM_HEADER + b"-browser-body":
                break
            time.sleep(0.05)
        # Stopping only the microphone must finalize the file while Run stays loaded.
        run_after_capture = get_run_api(server, run_id)
        expect(
            len(saved_paths) == 1,
            "Save Audio must record after Run and finalize after microphone Stop "
            f"(play_sources={play_sources}); files={saved_paths}, "
            f"run_status={run_after_capture.get('status')}, "
            f"node_statuses={run_after_capture.get('node_statuses')}.",
        )
        expect(saved_paths[0].read_bytes() == WEBM_HEADER + b"-browser-body", "Saved WebSocket chunks must remain ordered and exact.")
        expect(not list(target_dir.glob(".*.part")), "Microphone Stop must leave no unfinished recording after the idle delay.")
        if not play_sources:
            expect(run_after_capture.get("status") == "prepared", "Recording must not trigger a global Play.")
            expect(not any("seed batch" in line for line in run_after_capture.get("logs", [])),
                   "Explicit command publication must not release a source Play wave.")

        stop_run_api(server, run_id)
        final_run = get_run_api(server, run_id)
        expect(
            any("File saved" in line for line in final_run.get("logs", [])),
            "The persistent receiver must log successful finalization before manual Stop completes.",
        )


def main() -> None:
    """Run every Save Audio block-local scenario."""

    test_model_preparation_validation_and_simulation()
    test_direct_active_recordings()
    test_incomplete_audio_and_validation()
    test_owned_ui_api()
    test_graph_browser_ingress_end_to_end(play_sources=True)
    test_graph_browser_ingress_end_to_end()
    print("[ok] F5.45_save_audio_block")


if __name__ == "__main__":
    main()
