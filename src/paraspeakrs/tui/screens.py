"""Backwards-compatible names for the screens, which now live in their own modules.

The single-screen UI grew a browser, a queue, a voice cache and a labeling table;
keeping them in one file made every one of them harder to read than it had to be.
"""

from .browse import BrowserTree, best_match, default_browse_root, fuzzy_matches
from .home import HomeScreen
from .modals import ChoiceModal, NameSuggester, PathSuggester, TextPromptModal
from .note import NotePanel
from .speakers import SpeakerPanel, TranscriptScreen
from .voices import VoicesScreen

__all__ = [
    "BrowserTree",
    "ChoiceModal",
    "HomeScreen",
    "NameSuggester",
    "NotePanel",
    "PathSuggester",
    "SpeakerPanel",
    "TextPromptModal",
    "TranscriptScreen",
    "VoicesScreen",
    "best_match",
    "default_browse_root",
    "fuzzy_matches",
]
