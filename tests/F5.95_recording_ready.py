"""FB2/FB4/FB6/FB8: completion events follow finalized files, never partial/aborted audio."""

from pathlib import Path
import json
import runpy
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.save_audio.block import SaveAudioBlock, SaveAudioBlockError

FIXTURES = runpy.run_path(str(Path(__file__).with_name("F5.45_save_audio_block.py")))


def outputs(recorder):
    """Drain the real listener and return only completion publications."""
    recorder.observed("saved")
    return [json.loads(out.value) for result in recorder.results for out in result.outputs
            if out.port_name == "recording_ready"]


def main():
    """No microphone or provider call: use isolated binary audio service fixtures."""
    block = SaveAudioBlock()
    with tempfile.TemporaryDirectory(prefix="recording-ready-") as temporary:
        root = Path(temporary)
        legacy = FIXTURES["save_context"](root, runtime_mode="centralized")
        legacy.output_ports = ()
        assert block.execute_runtime(legacy).status == "skipped"
        with FIXTURES["active_recorder"](root) as recorder:
            recorder.command("start", "complete", call_id="call-fixture")
            recorder.frame("complete", b"OggS-first")
            FIXTURES["wait_until"](lambda: recorder.observed("recording"), "No recording")
            assert not outputs(recorder)
            recorder.command("stop", "complete", frame_count=2, byte_count=15)
            time.sleep(.05)
            assert not outputs(recorder), "Stop alone must not announce a complete file"
            recorder.frame("complete", b"-last")
            FIXTURES["wait_until"](lambda: len(outputs(recorder)) == 1, "No completion output")
            ready = outputs(recorder)[0]
            assert ready["event"] == "recording_ready" and ready["call_id"] == "call-fixture"
            assert ready["stream_id"] == "complete" and ready["frames"] == 2 and ready["bytes"] == 15
            assert len(ready["recording_id"]) == 32 and Path(ready["path"]).is_absolute()
            assert Path(ready["path"]).read_bytes() == b"OggS-first-last"
            assert not list(Path(ready["path"]).parent.glob("*.part"))
            recorder.command("stop", "complete", frame_count=2, byte_count=15)
            recorder.command("start", "empty")
            recorder.command("stop", "empty", frame_count=0, byte_count=0)
            recorder.command("start", "abort")
            recorder.frame("abort", b"OggS-abort")
            recorder.command("stop", "abort", frame_count=1, byte_count=10, aborted=True)
            FIXTURES["wait_until"](lambda: recorder.observed("cancelled"), "No cancellation state")
            assert len(outputs(recorder)) == 1
        with FIXTURES["active_recorder"](root) as recorder:
            with patch.object(SaveAudioBlock, "_finalize_recording", side_effect=SaveAudioBlockError("fixture finalization failure")):
                recorder.command("start", "failure")
                recorder.frame("failure", b"OggS-fixture")
                recorder.command("stop", "failure", frame_count=1, byte_count=12)
                FIXTURES["wait_until"](lambda: recorder.observed("error"), "No finalization failure")
                assert not outputs(recorder)
        for invalid in (None, "", "x" * 129, "bad\x00id"):
            try:
                block._validate_command({"action": "start", "stream_id": "one", "call_id": invalid})
            except SaveAudioBlockError:
                pass
            else:
                raise AssertionError("Invalid call_id accepted")
    print("[ok] Save Audio finalized-only events, correlation, no duplicates and legacy compatibility")


if __name__ == "__main__":
    main()
