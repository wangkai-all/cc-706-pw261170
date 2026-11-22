"""Versioned tokenisation and phrase-slicing rules.

Both rules are data, identified by a version string. Every text-derived row
records the rule-set fingerprint it was produced under, so an index built with
one set of rules can never be silently reused to judge a document processed
under another.

Tokenizer ``corpuslake.tokenizer.v1``
    * ``\\r\\n`` / ``\\r`` line endings are folded to ``\\n``.
    * Unicode NFKC normalisation, then lower-casing. Normalisation is done per
      base character + combining marks so every produced character keeps the
      span it came from in the original text.
    * Characters are classified as *word*, *number* or *other*.
    * A token is a maximal run of characters of one class.
    * Each CJK ideograph (and kana / hangul syllable) is emitted as its own
      one-character word token.  Without a dictionary segmenter this is the
      only defensible way to keep unsegmented Chinese text from becoming one
      enormous token; it also means Chinese text is compared as character
      n-grams.
    * Punctuation, symbols and whitespace are separators and produce no token;
      whitespace still splits word runs into separate tokens.

Phrase slicer ``corpuslake.phrases.v1``
    * A phrase is ``n`` consecutive tokens inside one sentence span. Sentence
      spans end at a line break or one of ``. ! ? ; 。 ！ ？ ；`` (commas and
      other punctuation do not break a span).
    * The phrase set is de-duplicated: repeated phrases collapse to one
      element and keep an occurrence count for inspection. Jaccard similarity
      is defined on the phrase *set*, so repeating a sentence never inflates
      the score.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Sequence

TOKENIZER_ID = "corpuslake.tokenizer.v1"
PHRASES_ID = "corpuslake.phrases.v1"
VOCAB_ID = "corpuslake.vocab.v1"

DEFAULT_PHRASE_N = 3
MIN_PHRASE_N = 1
MAX_PHRASE_N = 8

TERMINAL_BREAK_CHARS = frozenset("\n.!?;。！？；")
PHRASE_SEPARATOR = "\x1f"

_CJK_RANGES = (
    (0x1100, 0x11FF),    # hangul jamo
    (0x3040, 0x30FF),    # hiragana / katakana
    (0x3400, 0x4DBF),    # CJK ext A
    (0x4E00, 0x9FFF),    # CJK unified
    (0xAC00, 0xD7AF),    # hangul syllables
    (0xF900, 0xFAFF),    # CJK compatibility
    (0x20000, 0x2FA1F),  # CJK ext B-F
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


CLASS_WORD = "word"
CLASS_NUMBER = "number"
CLASS_OTHER = "other"


def classify_char(ch: str) -> str:
    if ch.isdigit():
        return CLASS_NUMBER
    if ch.isalpha():
        return CLASS_WORD
    return CLASS_OTHER


@dataclass(frozen=True)
class RuleSpec:
    version: str
    vocab_version: str
    description: str


TOKENIZER_RULES = RuleSpec(
    version=TOKENIZER_ID,
    vocab_version=VOCAB_ID,
    description=(
        "NFKC + lowercase with original character spans retained; word runs and "
        "digit runs are separate tokens; each CJK character is its own token; "
        "punctuation/symbols/whitespace are separators."
    ),
)

PHRASE_RULES = RuleSpec(
    version=PHRASES_ID,
    vocab_version=VOCAB_ID,
    description=(
        "Contiguous n-grams of tokens inside a sentence span (line breaks and "
        ". ! ? ; 。 ！ ？ ； end a span); duplicate phrases collapse with a count; "
        "Jaccard is over the deduplicated phrase set."
    ),
)


@dataclass(frozen=True)
class Token:
    text: str
    start: int          # character offset in the original text
    end: int            # exclusive
    break_after: bool = False

    def as_row(self) -> list:
        return [self.text, self.start, self.end, 1 if self.break_after else 0]

    @staticmethod
    def from_row(row: Sequence) -> "Token":
        return Token(str(row[0]), int(row[1]), int(row[2]), bool(row[3]))


_JOINERS = {"‍", "️", "⃣"}


def _normalised_chars(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """NFKC+lower each base cluster, tracking original spans."""
    chars: list[str] = []
    origins: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                chars.append("\n")
                origins.append((i, i + 2))
                i += 2
            else:
                chars.append("\n")
                origins.append((i, i + 1))
                i += 1
            continue
        j = i + 1
        while j < n and (unicodedata.combining(text[j]) or text[j] in _JOINERS):
            j += 1
        cluster = text[i:j]
        folded = unicodedata.normalize("NFKC", cluster).lower()
        for c in folded:
            chars.append(c)
            origins.append((i, j))
        i = j
    return chars, origins


def tokenize_spans(text: str) -> list[Token]:
    """Tokenise ``text`` and keep each token's original character span."""
    chars, origins = _normalised_chars(text)
    tokens: list[Token] = []
    buf: list[str] = []
    buf_start = 0
    buf_end = 0
    cls = CLASS_OTHER

    def flush() -> None:
        nonlocal buf
        if buf:
            tokens.append(Token("".join(buf), buf_start, buf_end, False))
            buf = []

    for ch, (start, end) in zip(chars, origins):
        k = classify_char(ch)
        if k == CLASS_OTHER:
            flush()
            cls = CLASS_OTHER
            continue
        if k == CLASS_WORD and _is_cjk(ch):
            flush()
            cls = CLASS_OTHER
            tokens.append(Token(ch, start, end, False))
            continue
        if k != cls:
            flush()
            cls = k
        if not buf:
            buf_start = start
        buf_end = end
        buf.append(ch)
    flush()

    # Sentence ends are punctuation between tokens, so mark them on the token
    # just before the gap.
    for idx in range(len(tokens) - 1):
        gap = text[tokens[idx].end:tokens[idx + 1].start]
        if any(c in TERMINAL_BREAK_CHARS for c in gap):
            tok = tokens[idx]
            tokens[idx] = Token(tok.text, tok.start, tok.end, True)
    if tokens:
        last = tokens[-1]
        tokens[-1] = Token(last.text, last.start, last.end, True)
    return tokens


def tokenize(text: str) -> list[str]:
    return [t.text for t in tokenize_spans(text)]


def phrase_key(phrase: Sequence[str]) -> str:
    return PHRASE_SEPARATOR.join(phrase)


def phrase_key_display(key: str) -> str:
    return key.replace(PHRASE_SEPARATOR, " ")


def slice_phrases(tokens: Sequence[str], n: int = DEFAULT_PHRASE_N,
                  break_after: Sequence[bool] | None = None) -> list[tuple[str, int]]:
    """Return ``(phrase_key, occurrences)`` sorted by key."""
    if not MIN_PHRASE_N <= n <= MAX_PHRASE_N:
        raise ValueError("phrase length n must be between %d and %d" % (MIN_PHRASE_N, MAX_PHRASE_N))
    if n > len(tokens):
        return []
    breaks = break_after
    if breaks is None:
        breaks = [False] * len(tokens)
        if breaks:
            breaks[-1] = True
    counts: dict[str, int] = {}
    for i in range(len(tokens) - n + 1):
        if any(breaks[j] for j in range(i, i + n - 1)):
            continue
        key = phrase_key(tokens[i:i + n])
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items())


def slice_phrases_spans(tokens: Sequence[Token], n: int = DEFAULT_PHRASE_N) -> list[tuple[str, int]]:
    return slice_phrases([t.text for t in tokens], n, [t.break_after for t in tokens])


def decode_phrase(key: str) -> list[str]:
    return key.split(PHRASE_SEPARATOR)


def ruleset_payload(tokenizer_version: str, phrase_version: str, phrase_n: int,
                    vocab_version: str) -> dict:
    return {
        "tokenizer_version": tokenizer_version,
        "phrase_version": phrase_version,
        "phrase_n": phrase_n,
        "vocab_version": vocab_version,
    }


def ruleset_fingerprint(tokenizer_version: str, phrase_version: str, phrase_n: int,
                        vocab_version: str) -> str:
    blob = json.dumps(
        ruleset_payload(tokenizer_version, phrase_version, phrase_n, vocab_version),
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:24]


def describe_rules(tokenizer_version: str, phrase_version: str, phrase_n: int,
                   vocab_version: str = VOCAB_ID) -> dict:
    if tokenizer_version != TOKENIZER_RULES.version:
        raise ValueError("unknown tokenizer version: %s" % tokenizer_version)
    if phrase_version != PHRASE_RULES.version:
        raise ValueError("unknown phrase version: %s" % phrase_version)
    return {
        "tokenizer": {
            "version": TOKENIZER_RULES.version,
            "vocab_version": TOKENIZER_RULES.vocab_version,
            "description": TOKENIZER_RULES.description,
        },
        "phrases": {
            "version": PHRASE_RULES.version,
            "vocab_version": VOCAB_ID,
            "description": PHRASE_RULES.description,
            "n": phrase_n,
            "min_n": MIN_PHRASE_N,
            "max_n": MAX_PHRASE_N,
        },
        "vocabulary": {
            "version": VOCAB_ID,
            "description": (
                "Global phrase order per index task: document frequency ascending, "
                "then phrase key ascending. The order only applies to this task's "
                "document snapshot; its digest is recorded on the task."
            ),
        },
        "fingerprint": ruleset_fingerprint(tokenizer_version, phrase_version, phrase_n, vocab_version),
    }
