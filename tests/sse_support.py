"""Reading Server-Sent Events in tests, from the wire bytes (no SSE client library).

`parse_sse` follows the WHATWG event-stream rules the browser applies: frames are
separated by a blank line, `:` starts a comment, `field: value` drops one
leading space, `data` lines join with "\\n". `SseReader` holds a real streaming
connection open in a thread, so a test can upload while it listens and drop the
connection on purpose.
"""

import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Frame:
    event: str | None = None
    id: str | None = None
    data_lines: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)

    @property
    def data(self) -> str:
        return "\n".join(self.data_lines)

    def json(self) -> Any:
        return json.loads(self.data)

    @property
    def is_comment(self) -> bool:
        return self.event is None and self.id is None and not self.data_lines


def _apply(frame: Frame, line: str) -> None:
    if line.startswith(":"):
        frame.comments.append(line[1:].removeprefix(" "))
        return
    name, _, value = line.partition(":")
    value = value.removeprefix(" ")
    if name == "event":
        frame.event = value
    elif name == "id":
        frame.id = value
    elif name == "data":
        frame.data_lines.append(value)


def parse_sse(text: str) -> list[Frame]:
    """Every frame of a complete body, comment-only frames included."""
    frames: list[Frame] = []
    current = Frame()
    touched = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            if touched:
                frames.append(current)
            current, touched = Frame(), False
            continue
        _apply(current, line)
        touched = True
    if touched:
        frames.append(current)
    return frames


def events(frames: list[Frame]) -> list[Frame]:
    """The frames a browser would dispatch (comments are not events)."""
    return [frame for frame in frames if not frame.is_comment]


class SseReader:
    """One live connection, read in a background thread.

    `next_frame` / `next_event` wait for the next parsed frame. `close` drops the
    connection the way a closed laptop lid or a lost network does: the reader
    stops at the next frame (heartbeats keep frames coming) and the socket closes.
    """

    def __init__(
        self, base_url: str, headers: dict[str, str], path: str = "/notifications/stream"
    ) -> None:
        self.frames: queue.Queue[Frame | BaseException] = queue.Queue()
        self.status: int | None = None
        self.headers: httpx.Headers | None = None
        self._opened = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(base_url, headers, path), name="sse-reader", daemon=True
        )
        self._thread.start()
        if not self._opened.wait(10):
            raise AssertionError("the stream did not answer")

    def _run(self, base_url: str, headers: dict[str, str], path: str) -> None:
        try:
            with (
                httpx.Client(base_url=base_url, timeout=httpx.Timeout(10.0)) as client,
                client.stream("GET", path, headers=headers) as response,
            ):
                self.status = response.status_code
                self.headers = response.headers
                self._opened.set()
                current, touched = Frame(), False
                for line in response.iter_lines():
                    if line == "":
                        if touched:
                            self.frames.put(current)
                        current, touched = Frame(), False
                        if self._stop.is_set():
                            return
                        continue
                    _apply(current, line)
                    touched = True
        except BaseException as error:  # noqa: BLE001 - handed to the test
            self.frames.put(error)
        finally:
            self._opened.set()

    def next_frame(self, timeout: float = 5.0) -> Frame:
        item = self.frames.get(timeout=timeout)
        if isinstance(item, BaseException):
            raise item
        return item

    def next_event(self, timeout: float = 5.0) -> Frame:
        """The next dispatched event, skipping keep-alive comments."""
        while True:
            frame = self.next_frame(timeout)
            if not frame.is_comment:
                return frame

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), "the reader did not stop"
