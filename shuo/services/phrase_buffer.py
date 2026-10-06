"""Small deterministic text buffer for streaming LLM -> TTS handoff."""


class BoundedPhraseBuffer:
    """Emit natural text fragments without ever waiting for a full response.

    Text is preserved byte-for-byte at the Python string level: concatenating all
    emitted chunks plus ``flush()`` exactly reproduces the input sequence. Hard
    punctuation may release early; otherwise ``max_chars`` is an absolute bound.
    No timer/task is created, so cancellation has no background ownership.
    """

    def __init__(
        self,
        max_chars: int,
        *,
        min_soft_chars: int = 24,
        first_max_chars: int | None = None,
        first_min_soft_chars: int | None = None,
    ):
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer")
        if isinstance(min_soft_chars, bool) or not isinstance(min_soft_chars, int) or min_soft_chars <= 0:
            raise ValueError("min_soft_chars must be a positive integer")
        if first_max_chars is not None and (
            isinstance(first_max_chars, bool)
            or not isinstance(first_max_chars, int)
            or first_max_chars <= 0
        ):
            raise ValueError("first_max_chars must be a positive integer")
        if first_min_soft_chars is not None and (
            isinstance(first_min_soft_chars, bool)
            or not isinstance(first_min_soft_chars, int)
            or first_min_soft_chars <= 0
        ):
            raise ValueError("first_min_soft_chars must be a positive integer")

        self._steady_max_chars = max_chars
        self._steady_min_soft_chars = min(min_soft_chars, max_chars)
        self._first_mode = first_max_chars is not None
        self._max_chars = first_max_chars or max_chars
        first_soft = (
            first_min_soft_chars
            if first_min_soft_chars is not None
            else min_soft_chars
        )
        self._min_soft_chars = min(first_soft, self._max_chars)
        self._buffer = ""

    @property
    def buffered_chars(self) -> int:
        return len(self._buffer)

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        self._buffer += text
        emitted = []
        while self._buffer:
            cut = self._find_hard_boundary()
            if cut is not None and cut <= self._max_chars:
                emitted.append(self._take(cut))
                continue

            cut = self._find_soft_boundary()
            if cut is not None and cut <= self._max_chars:
                emitted.append(self._take(cut))
                continue

            if len(self._buffer) < self._max_chars:
                break

            cut = self._bounded_cut()
            emitted.append(self._take(cut))
        return emitted

    def flush(self) -> str:
        text = self._buffer
        self._buffer = ""
        return text

    def _take(self, cut: int) -> str:
        text = self._buffer[:cut]
        self._buffer = self._buffer[cut:]
        if self._first_mode:
            self._first_mode = False
            self._max_chars = self._steady_max_chars
            self._min_soft_chars = self._steady_min_soft_chars
        return text

    def _find_hard_boundary(self):
        for index, char in enumerate(self._buffer):
            if char in "?!":
                return index + 1
            # A period is ambiguous at the current edge (3.8, abbreviations).
            # Require following whitespace unless max_chars forces a release.
            if char == "." and index + 1 < len(self._buffer) and self._buffer[index + 1].isspace():
                return index + 2
        return None

    def _find_soft_boundary(self):
        for index, char in enumerate(self._buffer):
            cut = index + 1
            if cut < self._min_soft_chars or char not in ",;:":
                continue
            if index + 1 < len(self._buffer) and self._buffer[index + 1].isspace():
                return index + 2
        return None

    def _bounded_cut(self) -> int:
        window = self._buffer[: self._max_chars]
        # Prefer retaining a word boundary, but never choose a very early space
        # that would defeat the configured batching cap. Include the whitespace
        # so concatenation remains exact.
        split_at = max((i for i, ch in enumerate(window) if ch.isspace()), default=-1)
        if split_at >= self._max_chars // 2:
            return split_at + 1
        return self._max_chars
