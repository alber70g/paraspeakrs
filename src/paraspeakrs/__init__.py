"""Parakeet INT8 ASR + speakrs diarization for long meeting recordings."""

from importlib import metadata

from .config import Settings
from .pipeline import TranscriptionPipeline

__all__ = ["Settings", "TranscriptionPipeline", "__version__"]

try:
    __version__ = metadata.version("paraspeakrs")
except metadata.PackageNotFoundError:  # running from a source tree without an install
    __version__ = "unknown"
