#!/usr/bin/env python3
"""FB1/FB2/FB4/FB6: fresh lifecycle events, rapid multi-stream commands and no replay."""
import json
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.save_audio.block import SaveAudioBlock
from bloxsmith_app.block_runtime import BlockInputEvent

FIXTURES = runpy.run_path(str(Path(__file__).with_name("F5.45_save_audio_block.py")))


def event(command, sequence=1, port=2):
    """Match real runtime provenance; values remain independent JSON documents."""
    return BlockInputEvent(edge_id="commands", input_port_id=port,
        input_port_name="command_in", source_node_id="merge", source_port_id=2,
        value=json.dumps(command), content_type="application/json", sequence=sequence)


def main():
    """Keep regression fixtures isolated from user projects and real equipment."""
    block = SaveAudioBlock()
    assert block.model["runtime"]["active_execution_policy"] == "on_each_event"
    streams = [("one", b"OggS-first-source"), ("two", b"OggS-second-source")]
    commands = ([{"action": "start", "stream_id": stream} for stream, _ in streams]
        + [{"action": "stop", "stream_id": stream, "frame_count": 1, "byte_count": len(data), "aborted": False}
           for stream, data in streams])
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sent = []
        context = FIXTURES["save_context"](root, runtime_mode="zeromq_active",
            services={"runtime_listener": SimpleNamespace(send=sent.append)})
        # The old grouped attribute is deliberately not a valid JSON object.
        context.input_attribute("command_in").update("\n".join(json.dumps(c) for c in commands))
        context.input_events = tuple(event(c, index) for index, c in enumerate(commands, 1))
        assert block.execute_runtime(context).status == "success"
        assert sent == commands
        sent.clear()
        context.input_events = (event(commands[0]), event({"action": "invalid"}))
        assert block.execute_runtime(context).status == "failed" and not sent
        context.input_events = tuple(event(commands[0], i) for i in range(65))
        assert block.execute_runtime(context).status == "failed" and not sent
        context.input_events = (event(commands[0], port=1),)
        assert block.execute_runtime(context).status == "skipped" and not sent
        context.input_events = ()
        context.input_attribute("command_in").status = "consumed"
        assert block.execute_runtime(context).status == "skipped" and not sent
        context.input_attribute("command_in").update(commands[0])
        assert block.execute_runtime(context).status == "success" and sent == [commands[0]]
        context.runtime_mode = "centralized"
        context.input_events = tuple(event(c) for c in commands)
        assert block.execute_runtime(context).status == "skipped" and len(sent) == 1
        assert not (root / "exports").exists()

        with FIXTURES["active_recorder"](root) as recorder:
            context = FIXTURES["save_context"](root, runtime_mode="zeromq_active",
                services={"runtime_listener": recorder.host.client})
            context.input_events = tuple(event(c, index) for index, c in enumerate(commands, 1))
            # Data stop can precede final audio on the separate transport.
            assert block.execute_runtime(context).status == "success"
            for stream, data in streams:
                recorder.frame(stream, data)
            def saved_both():
                recorder.observed("saved")
                return any(len(r.metadata.get("save_audio", {}).get("saved_files", [])) == 2
                           for r in recorder.results)
            FIXTURES["wait_until"](saved_both, "Rapid lifecycle commands must finalize both streams")
            for stream, data in streams:
                assert (root / "exports/audio-test" / f"capture_{stream}.ogg").read_bytes() == data
            assert not list((root / "exports/audio-test").glob(".*.part"))
            assert not any(r.status == "failed" for r in recorder.results)
    print("[ok] Save Audio: event bursts, no stale replay, two exact files and simulation")


if __name__ == "__main__":
    main()
