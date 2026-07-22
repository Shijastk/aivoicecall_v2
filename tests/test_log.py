"""
Console logging must survive a non-UTF-8 terminal.

The Windows dev console is cp1252, which cannot encode the '\\u2502'
separator in ColorFormatter, the arrows in Logger.event, or the emoji in
the lifecycle helpers. Without the UTF-8 wrapper every single log line
raises UnicodeEncodeError inside the handler, and the operator sees a
wall of logging tracebacks instead of the call trace -- which is exactly
what the live-call diagnosis depends on.

This is the same class of defect as Bug C: correct on the Linux deploy
target, broken on the Windows dev machine.
"""

import io
import sys
import logging

import pytest

from shuo.log import ColorFormatter, _utf8_stream, setup_logging, get_logger, Logger


class Cp1252Stdout:
    """A stdout that behaves like a legacy Windows console."""

    encoding = "cp1252"

    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, text):
        # Faithfully reproduce the failure: cp1252 cannot encode U+2502.
        self.buffer.write(text.encode("cp1252"))

    def flush(self):
        pass


class TestUtf8Stream:
    def test_wraps_a_cp1252_console(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", Cp1252Stdout())
        stream = _utf8_stream()
        assert stream.encoding.lower().replace("-", "") == "utf8"

    def test_passes_through_a_utf8_console(self, monkeypatch):
        class Utf8Stdout:
            encoding = "utf-8"
            buffer = io.BytesIO()

        stdout = Utf8Stdout()
        monkeypatch.setattr(sys, "stdout", stdout)
        assert _utf8_stream() is stdout

    def test_survives_a_stream_with_no_buffer(self, monkeypatch):
        """pytest's captured stdout has no .buffer; must not explode."""
        class NoBuffer:
            encoding = "cp1252"

        stdout = NoBuffer()
        monkeypatch.setattr(sys, "stdout", stdout)
        assert _utf8_stream() is stdout


class TestFormatterOutput:
    def test_box_character_reaches_a_cp1252_console_without_raising(self, monkeypatch):
        """The regression: this raised UnicodeEncodeError on every line."""
        monkeypatch.setattr(sys, "stdout", Cp1252Stdout())

        handler = logging.StreamHandler(_utf8_stream())
        handler.setFormatter(ColorFormatter())
        record = logging.LogRecord(
            "probe", logging.INFO, __file__, 1, "hello", None, None
        )
        handler.emit(record)  # must not raise

    def test_emoji_lifecycle_lines_do_not_raise(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", Cp1252Stdout())
        setup_logging()
        # Each of these carries a non-cp1252 glyph.
        Logger.server_ready("https://example.test")
        Logger.call_initiating("+918071579312")
        Logger.websocket_connected()
        get_logger("probe").error("R0: recording endpoint returned 404")

    def test_formatter_output_contains_the_separator(self):
        record = logging.LogRecord(
            "probe", logging.INFO, __file__, 1, "hello", None, None
        )
        assert "│" in ColorFormatter().format(record)
