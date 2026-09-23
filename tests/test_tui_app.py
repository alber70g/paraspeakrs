"""Headless runs of the real app. These catch what unit tests structurally cannot:
a CSS rule that does not parse, a widget queried by an id nothing yields, a
binding pointing at an action that was renamed.
"""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("textual")

from test_labeling import (  # noqa: E402
    FakeAsr,
    FakeAudio,
    ThreeSpeakerDiarizer,
    ThreeSpeakerEmbeddings,
    WordAsr,
)

from paraspeakrs.cache import SpeakerCache  # noqa: E402
from paraspeakrs.labeling import LabelingWorkspace  # noqa: E402
from paraspeakrs.mcp_store import ArtifactStore  # noqa: E402
from paraspeakrs.pipeline import TranscriptionPipeline  # noqa: E402
from paraspeakrs.tui.app import DiarizeApp  # noqa: E402
from paraspeakrs.tui.browse import BrowserTree  # noqa: E402
from paraspeakrs.tui.lines import LinesPanel  # noqa: E402
from paraspeakrs.tui.note import NotePanel  # noqa: E402
from paraspeakrs.tui.speakers import SpeakerPanel  # noqa: E402
from paraspeakrs.tui.state import DONE, FAILED, UiState  # noqa: E402
from paraspeakrs.tui.voices import VoicesScreen  # noqa: E402


def _app(tmp_path: Path, browse_dir: Path | None = None, asr=None) -> DiarizeApp:
    pipeline = TranscriptionPipeline(
        diarizer=ThreeSpeakerDiarizer(),
        asr=asr or FakeAsr(),
        audio=FakeAudio(),
        speaker_cache=SpeakerCache(tmp_path / "speaker-cache.json"),
        embedding_extractor=ThreeSpeakerEmbeddings(),
    )
    workspace = LabelingWorkspace(pipeline, ArtifactStore(tmp_path / "jobs"))
    app = DiarizeApp(workspace, tmp_path / "ui-state.json")
    if browse_dir is not None:
        app.state.browse_dir = browse_dir
    return app


def _recordings(tmp_path: Path) -> Path:
    directory = tmp_path / "recordings"
    directory.mkdir()
    # Distinct bytes: identical recordings are deliberately reused as one job,
    # which would make a two-item queue look like a worker that stopped early.
    (directory / "standup.wav").write_bytes(b"standup")
    (directory / "weekly.wav").write_bytes(b"weekly")
    return directory


class _FileSelected:
    """Stands in for DirectoryTree.FileSelected, which only its path is read from."""

    def __init__(self, path: Path) -> None:
        self.path = path


def _file_selected(path: Path) -> _FileSelected:
    return _FileSelected(path)


async def _drain(app, pilot, timeout: float = 5.0) -> None:
    """Wait for the queue worker thread to finish, then let the UI settle."""
    deadline = asyncio.get_running_loop().time() + timeout
    while any(item.status != DONE for item in app.state.queue):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"queue did not drain: {[i.status for i in app.state.queue]}")
        await pilot.pause(0.05)
    await pilot.pause()


async def _select_processed(app, pilot, row: int = 0) -> SpeakerPanel:
    """Put the cursor on a processed recording and hand back its speaker panel."""
    app.screen.action_refresh()
    await pilot.pause()
    await pilot.pause()
    table = app.screen.query_one("#processed")
    table.focus()
    table.move_cursor(row=row)
    await pilot.pause()
    return app.screen.query_one(SpeakerPanel)


@pytest.mark.asyncio
async def test_home_screen_mounts_with_the_remembered_directory(tmp_path):
    """The whole point of caching the directory is not having to walk back to it."""
    directory = _recordings(tmp_path)
    state = UiState.load(tmp_path / "ui-state.json")
    state.remember_dir(directory)

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert Path(app.screen.query_one(BrowserTree).path) == directory


@pytest.mark.asyncio
async def test_queueing_a_recording_transcribes_it_without_blocking_the_browser(tmp_path):
    """Enter on a file queues it and hands control straight back; the browser is
    never disabled waiting out the minutes of ASR it just started."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        assert [item.path.name for item in app.state.queue] == ["standup.wav"]
        assert app.screen.query_one(BrowserTree).disabled is False

        await _drain(app, pilot)

        # A finished item leaves the queue - it is a job now, and the processed
        # panel is where jobs are listed.
        assert app.state.queue == []
        assert [job.source_path.name for job in app.workspace.list_jobs()] == ["standup.wav"]


@pytest.mark.asyncio
async def test_the_queue_drains_every_item_it_was_given(tmp_path):
    """Queuing while a run is in flight must not cancel that run - Textual's
    exclusive workers would, which would abandon a transcription mid-file."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        app.screen._enqueue(directory / "weekly.wav")

        await _drain(app, pilot)

        assert sorted(job.source_path.name for job in app.workspace.list_jobs()) == ["standup.wav", "weekly.wav"]


@pytest.mark.asyncio
async def test_an_already_transcribed_recording_is_marked_and_not_requeued(tmp_path):
    """Thirty similarly named recordings and no way to tell which have been done
    means doing several of them twice."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        app.screen.action_refresh()
        await pilot.pause()
        await pilot.pause()

        # Asserting the drawn label, not the marks dict: the tree hands out
        # resolved paths, so a dict keyed by unresolved ones looks perfectly
        # populated while every row still renders bare.
        tree = app.screen.query_one(BrowserTree)
        assert Path(tree.path) == directory.resolve()
        labels = {
            node.data.path.name: tree.render_label(node, tree.rich_style, tree.rich_style).plain
            for node in tree.root.children
        }
        assert "✓" in labels["standup.wav"]
        assert "✓" not in labels["weekly.wav"]

        # And queueing it again is refused rather than transcribing it twice.
        app.screen._enqueue(directory / "standup.wav")
        assert app.state.queue == []


@pytest.mark.asyncio
async def test_a_directory_says_how_many_of_its_recordings_are_done(tmp_path):
    """A ✓ per recording only shows once the directory is open; the parent has
    to say which directories still hold untranscribed audio."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        app.screen.action_refresh()
        await pilot.pause()
        await pilot.pause()

        tree = app.screen.query_one(BrowserTree)
        node = next(n for n in tree.root.children if n.data.path.name == "recordings")
        label = tree.render_label(node, tree.rich_style, tree.rich_style).plain
        assert "1/2" in label and "✓" not in label


@pytest.mark.asyncio
async def test_ctrl_s_lists_the_newest_recording_first(tmp_path):
    """Recorders name files by device as often as by date; the one from this
    morning has to be findable without reading every name."""
    import os

    directory = _recordings(tmp_path)
    os.utime(directory / "standup.wav", (1_000, 1_000))
    os.utime(directory / "weekly.wav", (2_000, 2_000))
    # created_at prefers birth time where the OS has one, which utime cannot
    # set, so pin it to mtime to make the order the test's to decide.
    import paraspeakrs.tui.browse as browse

    browse_created = browse.created_at
    browse.created_at = lambda path: path.stat().st_mtime
    try:
        app = _app(tmp_path, browse_dir=directory)
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.screen.query_one(BrowserTree)
            assert [n.data.path.name for n in tree.root.children] == ["standup.wav", "weekly.wav"]
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.pause()
            assert [n.data.path.name for n in tree.root.children] == ["weekly.wav", "standup.wav"]
    finally:
        browse.created_at = browse_created


@pytest.mark.asyncio
async def test_speaker_screen_separates_what_was_decided_from_what_was_guessed(tmp_path):
    """The suggestion column exists so a cosine match can be offered without
    being applied; if naming wrote into the same place, it could not be."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)

        panel = await _select_processed(app, pilot)
        assert [row.assigned_label for row in panel._rows.values()] == [None, None, None]

        panel._label("SPEAKER_00", "Albert")
        await pilot.pause()

        assert panel._rows["SPEAKER_00"].assigned_label == "Albert"


@pytest.mark.asyncio
async def test_merging_selected_speakers_names_them_together(tmp_path):
    """Stereo splits one person across channels, so this is the ordinary case."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)

        panel = await _select_processed(app, pilot)
        panel._selected = {"SPEAKER_01", "SPEAKER_02"}
        panel._merge_as("Michiel")
        await pilot.pause()

        assert panel._rows["SPEAKER_01"].assigned_label == "Michiel"
        assert panel._rows["SPEAKER_02"].assigned_label == "Michiel"
        assert app.workspace.pipeline.speaker_cache.names() == ["Michiel"]


@pytest.mark.asyncio
async def test_voices_screen_mounts(tmp_path):
    directory = _recordings(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        app.workspace.label_speaker(app.workspace.list_jobs()[0].job_id, "SPEAKER_00", "Albert")

        await app.push_screen(VoicesScreen())
        await pilot.pause()
        assert [str(node.label).split()[0] for node in app.screen.query_one("#voices").root.children] == ["Albert"]

@pytest.mark.asyncio
async def test_enter_on_a_transcribed_recording_opens_its_own_speakers_beside_it(tmp_path):
    """Enter used to answer "already transcribed, press ctrl+j" - which opened a
    second list you then had to find the same filename in again. The panel is
    already showing that file, so enter only has to hand focus to it."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "weekly.wav")
        await _drain(app, pilot)
        app.screen.action_refresh()
        await pilot.pause()
        await pilot.pause()

        home = app.screen
        home._chose_recording(_file_selected(directory / "weekly.wav"))
        await pilot.pause()

        # Still one screen: nothing was pushed.
        assert app.screen is home
        panel = home.query_one(SpeakerPanel)
        assert panel.summary is not None
        assert panel.summary.source_path.name == "weekly.wav"
        assert panel.query_one("#speakers").has_focus


@pytest.mark.asyncio
async def test_enter_on_an_untranscribed_recording_still_queues_it(tmp_path):
    """The other half of the same key must not have changed meaning."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._chose_recording(_file_selected(directory / "standup.wav"))
        assert [item.path.name for item in app.state.queue] == ["standup.wav"]
        await _drain(app, pilot)


@pytest.mark.asyncio
async def test_the_detail_panels_follow_the_browser_cursor(tmp_path):
    """Parent to detail: the speakers shown are always the selected file's, so
    moving off a transcribed recording must stop showing its speakers."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        app.screen.action_refresh()
        await pilot.pause()
        await pilot.pause()

        home = app.screen
        tree = home.query_one(BrowserTree)
        by_name = {node.data.path.name: node for node in tree.root.children}

        tree.move_cursor(by_name["standup.wav"])
        await pilot.pause()
        assert home.query_one(SpeakerPanel).summary.source_path.name == "standup.wav"

        tree.move_cursor(by_name["weekly.wav"])
        await pilot.pause()
        assert home.query_one(SpeakerPanel).summary is None
        assert "not transcribed yet" in str(home.query_one(SpeakerPanel).border_title)


@pytest.mark.asyncio
async def test_the_processed_panel_lists_finished_recordings_and_aims_the_detail(tmp_path):
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        app.screen.action_refresh()
        await pilot.pause()
        await pilot.pause()

        home = app.screen
        table = home.query_one("#processed")
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "standup.wav"

        table.focus()
        await pilot.pause()
        assert home.query_one(SpeakerPanel).summary.source_path.name == "standup.wav"


@pytest.mark.asyncio
async def test_tab_walks_the_six_panels_in_reading_order(tmp_path):
    """Everything is on one screen, so Tab is the only way between the panels."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        seen = [app.screen.focused.id or type(app.screen.focused).__name__]
        for _ in range(5):
            await pilot.press("tab")
            seen.append(app.screen.focused.id or type(app.screen.focused).__name__)
        assert seen == ["BrowserTree", "processed", "queue", "speakers", "lines", "note"]


@pytest.mark.asyncio
async def test_a_note_typed_about_a_meeting_is_saved_when_focus_leaves_it(tmp_path):
    """Diarization says who spoke; only a note says why the recording mattered."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        app.screen.action_refresh()
        await pilot.pause()
        await pilot.pause()

        home = app.screen
        job_id = app.workspace.list_jobs()[0].job_id
        home.query_one("#processed").focus()
        await pilot.pause()

        note = home.query_one(NotePanel)
        note.query_one("#note").focus()
        await pilot.pause()
        await pilot.press("m", "i", "g", "r", "a", "t", "i", "o", "n")
        await pilot.pause()
        assert app.workspace.job_note(job_id) == ""

        home.query_one(BrowserTree).focus()
        await pilot.pause()

        assert app.workspace.job_note(job_id) == "migration"
        # And the processed list marks that it now has one.
        assert "\u270e" in home.query_one("#processed").get_row_at(0)[3]


@pytest.mark.asyncio
async def test_a_saved_note_comes_back_when_the_recording_is_selected_again(tmp_path):
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        job_id = app.workspace.list_jobs()[0].job_id
        app.workspace.set_job_note(job_id, "sprint planning")
        app.screen.action_refresh()
        await pilot.pause()
        await pilot.pause()

        home = app.screen
        home.query_one("#processed").focus()
        await pilot.pause()
        assert home.query_one(NotePanel).query_one("#note").text == "sprint planning"

        # Moving to an untranscribed file has nothing to show and nothing to edit.
        tree = home.query_one(BrowserTree)
        tree.focus()
        tree.move_cursor({node.data.path.name: node for node in tree.root.children}["weekly.wav"])
        await pilot.pause()
        area = home.query_one(NotePanel).query_one("#note")
        assert area.text == ""
        assert area.read_only is True


@pytest.mark.asyncio
async def test_typing_a_note_does_not_trip_the_screen_shortcuts(tmp_path):
    """Every letter on this screen is a shortcut somewhere - q quits, j opens the
    job list, g asks for a path. In a note field they have to be letters."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        app.screen.action_refresh()
        await pilot.pause()
        await pilot.pause()

        home = app.screen
        home.query_one("#processed").focus()
        await pilot.pause()
        home.query_one(NotePanel).query_one("#note").focus()
        await pilot.pause()

        await pilot.press("q", "j", "g", "x", "r", "v")
        await pilot.pause()

        assert app.screen is home, "a shortcut fired instead of typing"
        assert home.query_one(NotePanel).query_one("#note").text == "qjgxrv"


@pytest.mark.asyncio
async def test_a_job_can_be_deleted_from_the_processed_panel(tmp_path):
    """The separate job list was the only place delete lived, and it listed the
    same recordings this panel does; the key had to come with it."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        await _select_processed(app, pilot)
        job_id = app.workspace.list_jobs()[0].job_id

        home = app.screen
        await pilot.press("d")
        await pilot.pause()
        # It asks first: a transcript and its names are not cheap to get back.
        assert app.screen is not home
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert app.workspace.list_jobs() == []
        assert home.query_one("#processed").row_count == 0
        assert not (app.workspace.store.root / job_id).exists()


@pytest.mark.asyncio
async def test_deleting_takes_the_note_with_the_job_but_keeps_the_voice(tmp_path):
    """A note is about one recording and goes with it. A voice was learned from
    it and is how every future recording gets recognized, so it stays."""
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._enqueue(directory / "standup.wav")
        await _drain(app, pilot)
        job_id = app.workspace.list_jobs()[0].job_id
        app.workspace.set_job_note(job_id, "sprint planning")
        app.workspace.label_speaker(job_id, "SPEAKER_00", "Albert")
        await _select_processed(app, pilot)

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert app.workspace.job_note(job_id) == ""
        assert app.workspace.pipeline.speaker_cache.names() == ["Albert"]


class _StereoAudio(FakeAudio):
    def channel_count(self, input_path):
        return 2


@pytest.mark.asyncio
async def test_enter_on_a_stereo_recording_asks_which_channel_is_one_speaker(tmp_path):
    """Only the user knows a headset channel holds one person; the answer has
    to reach the pipeline, or the channel gets split into voices to name."""
    from paraspeakrs.tui.modals import ChoiceModal

    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    app.workspace.pipeline.audio = _StereoAudio()
    seen = []
    transcribe = app.workspace.transcribe
    app.workspace.transcribe = lambda path, **kw: seen.append(kw["single_speaker_channels"]) or transcribe(path, **kw)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._chose_recording(_file_selected(directory / "standup.wav"))
        await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        assert app.state.queue == []

        await pilot.click("#choice-L")
        await _drain(app, pilot)
        assert seen == [frozenset({"L"})]


@pytest.mark.asyncio
async def test_escaping_the_channel_question_queues_nothing(tmp_path):
    directory = _recordings(tmp_path)
    app = _app(tmp_path, browse_dir=directory)
    app.workspace.pipeline.audio = _StereoAudio()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._chose_recording(_file_selected(directory / "standup.wav"))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.state.queue == []


async def _meeting(tmp_path: Path, pilot, app) -> SpeakerPanel:
    directory = _recordings(tmp_path)
    await pilot.pause()
    app.screen._enqueue(directory / "standup.wav")
    await _drain(app, pilot)
    panel = await _select_processed(app, pilot)
    panel.focus_table()
    await pilot.pause()
    return panel


def _transcript(app) -> str:
    return app.workspace.transcript_text(app.workspace.list_jobs()[0].job_id)


@pytest.mark.asyncio
async def test_d_on_a_speaker_takes_all_their_lines_out_and_d_again_brings_them_back(tmp_path):
    """The kids talked through the call; one key removes them, and a mis-press
    is one key to undo."""
    app = _app(tmp_path, asr=WordAsr())
    async with app.run_test() as pilot:
        panel = await _meeting(tmp_path, pilot, app)
        await pilot.press("down", "d")
        await pilot.pause()

        assert panel._rows["SPEAKER_01"].excluded
        assert "Mama!" not in _transcript(app) and "Juice." not in _transcript(app)
        assert "Morning." in _transcript(app)

        await pilot.press("d")
        await pilot.pause()
        assert "Mama!" in _transcript(app)


@pytest.mark.asyncio
async def test_the_transcript_pane_follows_the_speaker_and_removes_single_lines(tmp_path):
    """Moving through the speaker table aims the transcript at that speaker; in
    the transcript, l jumps to their next line and d removes only that one."""
    app = _app(tmp_path, asr=WordAsr())
    async with app.run_test() as pilot:
        await _meeting(tmp_path, pilot, app)
        await pilot.press("down")
        await pilot.pause()
        lines = app.screen.query_one(LinesPanel)
        assert lines.speaker == "SPEAKER_01"
        assert lines._lines[lines.query_one("#lines").highlighted].text == "Mama!"

        await pilot.press("tab", "l")
        await pilot.pause()
        assert lines._lines[lines.query_one("#lines").highlighted].text == "Juice."

        await pilot.press("d")
        await pilot.pause()
        assert "Juice." not in _transcript(app)
        assert "Mama!" in _transcript(app)
        # The cursor stays on the line just removed, so d again undoes it.
        await pilot.press("d")
        await pilot.pause()
        assert "Juice." in _transcript(app)


@pytest.mark.asyncio
async def test_the_note_says_which_meeting_it_is_and_who_was_in_it(tmp_path):
    app = _app(tmp_path, asr=WordAsr())
    async with app.run_test() as pilot:
        panel = await _meeting(tmp_path, pilot, app)
        panel._label("SPEAKER_00", "Albert")
        await pilot.pause()
        await pilot.pause()

        header = str(app.screen.query_one("#note-header").render())
        assert header.startswith("standup · ")
        assert header.endswith(" · Albert")


class _PanickingDiarizer(ThreeSpeakerDiarizer):
    def diarize(self, audio_path):
        raise RuntimeError("speakrs-diar failed with exit code 101: thread 'main' panicked")


def test_retrying_a_failed_recording_survives_a_restart(tmp_path):
    """The reported crash: a diarizer panic, a retry of the same file, then the
    app refusing to start again with DuplicateKey on the queue table."""
    directory = _recordings(tmp_path)
    wav = (directory / "standup.wav").resolve()

    async def fail_then_retry() -> None:
        app = _app(tmp_path)
        app.workspace.pipeline.diarizer = _PanickingDiarizer()
        async with app.run_test() as pilot:
            app.screen._enqueue(wav)
            await _until(pilot, lambda: app.state.queue[0].status == FAILED)
            app.screen._enqueue(wav)
            await _until(pilot, lambda: app.state.queue[0].status == FAILED)
            assert len(app.state.queue) == 1

    async def restart() -> None:
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#queue").row_count == 1

    asyncio.run(fail_then_retry())
    asyncio.run(restart())


async def _until(pilot, done, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not done():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await pilot.pause(0.05)
    await pilot.pause()
