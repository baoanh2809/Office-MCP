"""Character budget for read tools. Python strings index by code point, so cuts never split UTF-8."""

_WORD_BOUNDARY_WINDOW = 100


class TextBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.truncated = False

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def take(self, text: str) -> str:
        """Return the part of ``text`` that fits, preferring a whitespace boundary near the cut."""
        if not text:
            return ""
        if self.remaining <= 0:
            self.truncated = True
            return ""
        if len(text) <= self.remaining:
            self.used += len(text)
            return text
        cut = self.remaining
        for i in range(cut, max(cut - _WORD_BOUNDARY_WINDOW, 0), -1):
            if text[i].isspace():
                cut = i
                break
        self.used = self.limit
        self.truncated = True
        return text[:cut].rstrip()

    def spend(self, n: int) -> bool:
        """Reserve ``n`` characters for content kept whole (a spreadsheet cell)."""
        if n > self.remaining:
            self.truncated = True
            return False
        self.used += n
        return True
