#!/usr/bin/env python3
"""FB1/FB2/FB4/FB5/FB6: cancellation is per stream, never a fatal recording error."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]


def load_fixture(path: Path, name: str):
    """Reuse block-owned test fixtures, never another block's implementation in production."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAVE = load_fixture(Path(__file__).with_name("F5.45_save_audio_block.py"), "save_cancel_fixtures")
from block_test_fixtures import instance_scope


def states(recorder, state: str):
    """Drain listener progress and retain all business transitions for exact assertions."""
    recorder.observed("__drain_only__")
    return [result for result in recorder.results if result.metadata.get("save_audio", {}).get("state") == state]


def test_cancelled_recording_preserves_other_streams_and_reuses_listener():
    """FB1/FB2/FB4: discard only the cancelled file, ignore its tail and record again without Run."""
    with TemporaryDirectory(prefix="save-cancel-") as directory:
        root = Path(directory)
        target = root / "exports/audio-test"
        with SAVE.active_recorder(root) as recorder:
            recorder.command("start", "kept")
            recorder.frame("kept", SAVE.WEBM_HEADER)
            recorder.command("stop", "kept", frame_count=1, byte_count=len(SAVE.WEBM_HEADER))
            SAVE.wait_until(lambda: bool(states(recorder, "saved")), "Initial capture must be saved.")
            kept = target / "capture_kept.webm"

            recorder.command("start", "parallel")
            # The service's default sequence is publisher-wide. Interleaved sources explicitly
            # supply per-stream sequences so this test doesn't invent a gap in the kept stream.
            recorder.frame("parallel", SAVE.WEBM_HEADER, sequence=1)
            # More cancellations than the simultaneous-session limit detect leaked session slots.
            for index in range(6):
                stream_id = f"cancelled-{index}"
                recorder.command("start", stream_id)
                recorder.frame(stream_id, SAVE.WEBM_HEADER)
                SAVE.wait_until(lambda: bool(list(target.glob(f".*{stream_id}*.part"))), "Capture must be open before abort.")
                recorder.command("stop", stream_id, frame_count=1, byte_count=len(SAVE.WEBM_HEADER), aborted=True)
                SAVE.wait_until(lambda: len(states(recorder, "cancelled")) == index + 1,
                                "Producer cancellation must not fail or stop Save Audio.")
                result = states(recorder, "cancelled")[-1]
                assert result.status == "success" and result.metadata["save_audio"]["stream_id"] == stream_id
                assert result.metadata["save_audio"]["saved_files"][0]["stream_id"] == "kept"
                assert "microphone" not in result.last_message.lower()
                assert not list(target.glob(f"*{stream_id}*")) and not list(target.glob(f".*{stream_id}*"))
                # Already queued or repeated events for a retired stream cannot reopen its file.
                recorder.frame(stream_id, b"late-tail")
                recorder.command("start", stream_id)
                recorder.command("stop", stream_id, frame_count=2, byte_count=999, aborted=True)

            recorder.frame("parallel", b"-complete", sequence=2)
            recorder.command("stop", "parallel", frame_count=2, byte_count=len(SAVE.WEBM_HEADER + b"-complete"))
            recorder.command("start", "next")
            recorder.frame("next", b"OggS-next")
            recorder.command("stop", "next", frame_count=1, byte_count=len(b"OggS-next"))
            SAVE.wait_until(lambda: len(states(recorder, "saved")) == 3, "The same listener must save subsequent audio.")
            assert kept.read_bytes() == SAVE.WEBM_HEADER
            assert (target / "capture_parallel.webm").read_bytes() == SAVE.WEBM_HEADER + b"-complete"
            assert (target / "capture_next.ogg").read_bytes() == b"OggS-next"
            assert len(list(target.iterdir())) == 3 and not list(target.glob(".*.part"))
            assert len(states(recorder, "cancelled")) == 6
            assert not states(recorder, "error") and not recorder.host.failure
            assert recorder.host._thread.is_alive()


def test_cancel_before_start_or_audio_is_idempotent():
    """FB4: early cancellation retires the stream even when start/audio are late or absent."""
    with TemporaryDirectory(prefix="save-cancel-early-") as directory:
        root = Path(directory)
        target = root / "exports/audio-test"
        with SAVE.active_recorder(root) as recorder:
            recorder.command("start", "empty")
            recorder.command("stop", "empty", frame_count=0, byte_count=0, aborted=True)
            # This audio enters the pre-start buffer before its cancellation is processed.
            recorder.frame("early", SAVE.WEBM_HEADER)
            time.sleep(.12)
            recorder.command("stop", "early", frame_count=1, byte_count=len(SAVE.WEBM_HEADER), aborted=True)
            SAVE.wait_until(lambda: len(states(recorder, "cancelled")) == 2, "Empty and early aborts must stay nonfatal.")
            recorder.command("start", "early")
            recorder.frame("early", b"late-audio")
            recorder.command("stop", "early", frame_count=1, byte_count=0, aborted=True)
            recorder.command("start", "next")
            SAVE.wait_until(lambda: len(states(recorder, "armed")) == 2, "Only a fresh stream may arm after cancellation.")
            assert not target.exists(), "Cancelled buffered audio must never create a file later."
            assert not states(recorder, "error") and not recorder.host.failure
            assert len(states(recorder, "cancelled")) == 2 and recorder.host._thread.is_alive()


def test_cancel_cleanup_failure_remains_visible():
    """FB4/FB5: a real filesystem cleanup error must not be misreported as successful cancellation."""
    with TemporaryDirectory(prefix="save-cancel-fs-") as directory:
        root = Path(directory)
        target = root / "exports/audio-test"
        unlink = Path.unlink

        def denied_part(path, *args, **kwargs):
            """Deny only this fixture's temporary-file deletion while preserving other cleanup."""
            if path.parent == target and path.suffix == ".part":
                raise PermissionError("test-only cleanup denied")
            return unlink(path, *args, **kwargs)

        with SAVE.active_recorder(root) as recorder:
            recorder.command("start", "blocked")
            recorder.frame("blocked", SAVE.WEBM_HEADER)
            SAVE.wait_until(lambda: bool(states(recorder, "recording")), "Fixture must be recording.")
            with patch.object(Path, "unlink", denied_part):
                recorder.command("stop", "blocked", frame_count=1, byte_count=len(SAVE.WEBM_HEADER), aborted=True)
                SAVE.wait_until(lambda: bool(states(recorder, "error")), "Cleanup errors must remain visible.")
                assert "test-only cleanup denied" in states(recorder, "error")[-1].error
                assert not states(recorder, "cancelled")
        assert not list(target.iterdir()), "Shutdown must retry cleanup once the filesystem recovers."


def test_simulation_has_no_cancellation_io():
    """FB6: even an aborted command is a no-op in centralized simulation, with no files/listener."""
    with TemporaryDirectory(prefix="save-cancel-simulation-") as directory:
        root = Path(directory)
        block = SAVE.SaveAudioBlock()
        context = SAVE.save_context(root, runtime_mode="centralized")
        context.input_attribute("command_in").update({"action": "stop", "stream_id": "simulation",
            "frame_count": 1, "byte_count": 100, "aborted": True})
        with patch.object(block, "_abort_recording", side_effect=AssertionError("No cleanup in simulation")), \
                patch.object(block, "_open_recording", side_effect=AssertionError("No recording in simulation")):
            assert not block.prepare_runtime(context).listen_on_run
            assert block.execute_runtime(context).status == "skipped"
        assert not list(root.iterdir())


def test_real_tts_interruptions_then_save_on_the_same_run():
    """FB1/FB2/FB4/FB6: real HTTP/Opus/TTS/data edges survive repeated aborts and save the next response."""
    import zmq
    control = load_fixture(ROOT / "blocs/openai_tts_stream/tests/F5.50_tts_interrupt_commands.py", "save_tts_fixtures")
    fixtures = control.FIXTURES
    payload = control.command_graph_document()
    payload["nodes"].append(SAVE.SaveAudioBlock().build_node_payload(node_id="save",
        config_overrides={"output_dir": "exports/tts", "filename_template": "capture_{stream_id}"}))
    payload["edges"].extend([
        {"id": "save-audio", "from": {"node": "tts", "port": 1}, "to": {"node": "save", "port": 1}, "kind": "data"},
        {"id": "save-command", "from": {"node": "tts", "port": 2}, "to": {"node": "save", "port": 2}, "kind": "data"},
    ])
    document, graph = fixtures.compile_runtime_graph_document(fixtures.GraphDocument.from_payload(payload))
    with TemporaryDirectory(prefix="save-tts-cancel-graph-") as directory, fixtures.fake_openai("hold_tail") as api:
        root = Path(directory)
        target = root / "exports/tts"
        wallet = fixtures.SecretManager(root / "secrets")
        wallet.initialize("test-only-wallet-password")
        wallet.set_secret(ref=fixtures.REF, value=fixtures.KEY)
        engine = fixtures.WorkflowOrchestrator(root_dir=root, runs_dir=root / "runs", secret_manager=wallet,
                                               active_worker_host="thread")
        run = engine.prepare_active_run(graph, document=document, run_data_scope=instance_scope(root, payload))
        assert run.status == "prepared", run.logs
        publisher = zmq.Context.instance().socket(zmq.PUB)
        publisher.setsockopt(zmq.LINGER, 0)

        def publish(source, value, sequence, content_type="text/plain"):
            """Drive normal compiled graph edges; never call Save/TTS listeners directly."""
            topic = run.plan.worker_configs[source].outputs[0].topic
            envelope = fixtures.MessageEnvelope(run_id=run.run_id, source_node_id=source, source_port_id=1,
                payload=value, sequence=sequence, content_type=content_type)
            publisher.send_multipart([topic.encode(), envelope.to_json().encode()])

        def saved_state():
            """Observe worker-published Save Audio metadata, including stream-scoped cancellation."""
            return run.results.get("save", {}).get("save_audio", {})

        try:
            publisher.connect(engine._active_sessions[run.run_id].pub_endpoint)
            time.sleep(.2)  # Only this diagnostic publisher bypasses the production readiness gate.
            for index in range(3):
                publish("text", f"Interrupted answer {index}", index + 1)
                fixtures.until(lambda: saved_state().get("state") == "recording", "TTS must reach Save Audio before interrupt.")
                publish("command", control.INTERRUPT, index + 1, "application/json")
                fixtures.until(lambda: saved_state().get("state") == "cancelled",
                               f"TTS cancellation must keep Save Audio and Run alive: {run.logs[-8:]}", timeout=2)
                assert run.status == "prepared" and run.run_id in engine._active_sessions
                assert not saved_state()["saved_files"] and not list(target.iterdir())
                assert json.loads(run.output_values["tts:2"]["value"])["aborted"] is True

            publish("text", "Complete answer after interruptions", 4)
            api.release.set()
            fixtures.until(lambda: saved_state().get("state") == "saved", "The same Run must save the next TTS response.")
            files = saved_state()["saved_files"]
            assert len(files) == 1 and len(list(target.iterdir())) == 1
            assert Path(files[0]["absolute_path"]).read_bytes() == fixtures.encoded_opus()
            assert len(api.requests) == 4 and run.status == "prepared"
            assert not any("[save-audio-error]" in line or "[runtime-listener-error] save" in line for line in run.logs)
            assert fixtures.KEY not in str(run.results) and fixtures.KEY not in str(run.logs)
        finally:
            publisher.close(0)
            engine.stop_active_run(run.run_id)
        previous_files = {path.name: path.read_bytes() for path in target.iterdir()}
        simulation = engine.create_run(graph, document=document, runtime_mode="centralized", auto_start=False)
        with patch.object(fixtures.OpenAITtsStreamBlock, "_http_client", side_effect=AssertionError("No HTTP in simulation")):
            engine._execute_run(simulation)
        assert simulation.status == "success", simulation.logs
        assert len(api.requests) == 4 and "tts:2" not in simulation.output_values
        assert {path.name: path.read_bytes() for path in target.iterdir()} == previous_files


if __name__ == "__main__":
    for test in (test_cancelled_recording_preserves_other_streams_and_reuses_listener,
                 test_cancel_before_start_or_audio_is_idempotent, test_cancel_cleanup_failure_remains_visible,
                 test_simulation_has_no_cancellation_io, test_real_tts_interruptions_then_save_on_the_same_run):
        test()
        print(f"[ok] {test.__name__}", flush=True)
