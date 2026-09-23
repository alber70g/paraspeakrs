"""The ASR backend's native library has to actually load.

sherpa_onnx is a thin wrapper around a compiled extension that dlopens
libonnxruntime, which ships in the separate sherpa-onnx-core distribution. If
that is missing the import fails at runtime with an ImportError about a missing
dylib -- the package looks installed, the CLI starts, and every transcription
fails. Declaring only `sherpa-onnx` is not enough: its 1.13.2 wheels do not
declare the core dependency, so a resolver can legally produce that state.
"""

from __future__ import annotations

import importlib

import pytest


def test_sherpa_onnx_loads_its_native_library():
    try:
        importlib.import_module("sherpa_onnx")
    except ImportError as exc:
        pytest.fail(
            "sherpa_onnx could not load its native library -- sherpa-onnx-core is "
            f"probably missing or mismatched: {exc}"
        )


def test_mcp_server_module_imports():
    """The MCP server must import against the installed mcp release.

    mcp 2.x renamed FastMCP to MCPServer, so an unpinned `mcp>=1.2.0` resolves to a
    version where this module raises ModuleNotFoundError at import -- the server
    then fails on startup for every client, which no other test would notice
    because nothing else imports it.
    """
    pytest.importorskip("mcp")
    importlib.import_module("paraspeakrs.mcp_server")
