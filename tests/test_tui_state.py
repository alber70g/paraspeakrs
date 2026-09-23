import json
from pathlib import Path

from paraspeakrs.tui.state import DONE, FAILED, RUNNING, WAITING, UiState


def test_browse_dir_survives_a_restart(tmp_path):
    """Recordings live nowhere near the working directory, so a browser that
    forgets means walking the same eight directories before every session."""
    state = UiState.load(tmp_path / "ui.json")
    state.remember_dir(tmp_path)

    assert UiState.load(tmp_path / "ui.json").browse_dir == tmp_path


def test_a_browse_dir_that_no_longer_exists_is_dropped(tmp_path):
    """An external drive gets unmounted between sessions. Restoring a path that
    cannot be listed strands the browser with no obvious way out."""
    gone = tmp_path / "volume"
    gone.mkdir()
    state = UiState.load(tmp_path / "ui.json")
    state.remember_dir(gone)
    gone.rmdir()

    assert UiState.load(tmp_path / "ui.json").browse_dir is None


def test_unreadable_state_costs_the_last_directory_not_the_app(tmp_path):
    """This file is a convenience and never a source of truth; a half-written one
    must not be able to stop the UI from starting."""
    path = tmp_path / "ui.json"
    path.write_text("{not json", encoding="utf-8")

    state = UiState.load(path)

    assert state.browse_dir is None
    assert state.queue == []


def test_queue_round_trips(tmp_path):
    path = tmp_path / "ui.json"
    state = UiState.load(path)
    state.enqueue(tmp_path / "a.wav")
    state.enqueue(tmp_path / "b.wav")

    reloaded = UiState.load(path)

    assert [item.path.name for item in reloaded.queue] == ["a.wav", "b.wav"]
    assert {item.status for item in reloaded.queue} == {WAITING}


def test_a_run_interrupted_by_a_crash_goes_back_in_line(tmp_path):
    """Nothing will ever finish a job whose process died mid-transcribe, so an
    item left marked running would sit there claiming progress forever."""
    path = tmp_path / "ui.json"
    path.write_text(
        json.dumps({"queue": [{"path": str(tmp_path / "a.wav"), "status": RUNNING}]}),
        encoding="utf-8",
    )

    assert UiState.load(path).queue[0].status == WAITING


def test_finished_items_do_not_come_back_but_failures_do(tmp_path):
    """A done item has become a job and is listed as one; a failure has nowhere
    else to be seen, so it stays until it is dealt with."""
    path = tmp_path / "ui.json"
    path.write_text(
        json.dumps(
            {
                "queue": [
                    {"path": str(tmp_path / "ok.wav"), "status": DONE, "job_id": "j1"},
                    {"path": str(tmp_path / "bad.wav"), "status": FAILED, "error": "no such codec"},
                ]
            }
        ),
        encoding="utf-8",
    )

    queue = UiState.load(path).queue

    assert [item.path.name for item in queue] == ["bad.wav"]
    assert queue[0].error == "no such codec"


def test_the_same_recording_is_not_queued_twice(tmp_path):
    """Two queue entries for one file transcribe it twice: the fingerprint check
    only reuses a job that already exists, and the first has not finished yet."""
    state = UiState.load(tmp_path / "ui.json")
    state.enqueue(tmp_path / "a.wav")

    assert state.enqueue(tmp_path / "a.wav") is None
    assert len(state.queue) == 1


def test_a_running_item_cannot_be_dropped(tmp_path):
    """It owns a worker thread that will report back about it; removing the entry
    it reports into loses the result."""
    state = UiState.load(tmp_path / "ui.json")
    item = state.enqueue(tmp_path / "a.wav")
    state.mark(item, RUNNING)

    assert state.drop(tmp_path / "a.wav") is False
    assert state.next_waiting() is None


def test_next_waiting_walks_the_queue_in_order(tmp_path):
    state = UiState.load(tmp_path / "ui.json")
    first = state.enqueue(tmp_path / "a.wav")
    state.enqueue(tmp_path / "b.wav")

    assert state.next_waiting() is first
    state.mark(first, DONE, job_id="j1")
    assert state.next_waiting().path == Path(tmp_path / "b.wav")


def test_a_finished_item_leaves_the_queue(tmp_path):
    """A done item has become a job and is listed as one; leaving it in the queue
    puts the same recording in two lists at once."""
    state = UiState.load(tmp_path / "ui-state.json")
    item = state.enqueue(Path("/rec/standup.wav"))

    state.mark(item, DONE, job_id="job-1")

    assert state.queue == []


def test_a_failed_item_stays_in_the_queue(tmp_path):
    """A failure is the only place the error is ever shown - dropping it would
    make a transcription that did not work look like one that never started."""
    state = UiState.load(tmp_path / "ui-state.json")
    item = state.enqueue(Path("/rec/standup.wav"))

    state.mark(item, FAILED, error="boom")

    assert [i.status for i in state.queue] == [FAILED]
    assert state.queue[0].error == "boom"


def test_a_single_speaker_channel_answer_survives_a_restart(tmp_path):
    """The question is asked once, at enter; a queued item transcribed after a
    restart must not silently forget the answer."""
    path = tmp_path / "ui-state.json"
    state = UiState.load(path)
    state.enqueue(Path("/rec/call.wav"), ["L"])

    assert UiState.load(path).queue[0].single_speaker_channels == ["L"]


def test_requeuing_a_failed_recording_retries_it_instead_of_adding_a_second(tmp_path):
    """The queue table keys rows by path. A retry that appended a second entry
    for a failed file crashed the table with DuplicateKey, and because the
    queue is saved first, every later start crashed the same way."""
    state = UiState.load(tmp_path / "ui.json")
    item = state.enqueue(tmp_path / "a.wav", ["L"])
    state.mark(item, FAILED, error="speakrs-diar failed with exit code 101")

    retried = state.enqueue(tmp_path / "a.wav", ["R"])

    assert retried is item
    assert len(state.queue) == 1
    assert (item.status, item.error, item.single_speaker_channels) == (WAITING, None, ["R"])


def test_a_state_file_with_duplicate_paths_still_loads_one_row_per_path(tmp_path):
    """Files written before the retry fix can hold the same path twice; they
    must heal on load instead of crashing the UI on every start."""
    path = tmp_path / "ui.json"
    wav = str(tmp_path / "a.wav")
    path.write_text(
        json.dumps({"queue": [{"path": wav, "status": FAILED, "error": "boom"}, {"path": wav, "status": WAITING}]}),
        encoding="utf-8",
    )

    queue = UiState.load(path).queue

    assert [(item.path.name, item.status) for item in queue] == [("a.wav", WAITING)]
