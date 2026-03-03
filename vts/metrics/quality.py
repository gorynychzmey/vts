"""Quality metrics: compression ratio, redundancy, number/date/unit mismatch, format."""
from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> tuple[int, bool]:
    """Return (token_count, is_estimate).

    Returns exact=False (is_estimate=True) and uses chars/4 as approximation.
    When real token counts are available from the tokenize endpoint, pass them
    directly to QualityAnalyzer.analyze() instead.
    """
    return max(1, len(text) // 4), True


# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------

_SENT_SEP = re.compile(r'(?<=[.!?…])\s+|\n{2,}')


def split_sentences(text: str) -> list[str]:
    """Naive sentence splitter on punctuation + double newlines."""
    parts = _SENT_SEP.split(text)
    return [s.strip() for s in parts if s.strip()]


# ---------------------------------------------------------------------------
# SimHash-based redundancy
# ---------------------------------------------------------------------------

def _word_ngrams(tokens: list[str], n: int) -> list[str]:
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def simhash(text: str, *, shingle_n: int = 3, bits: int = 64) -> int:
    """Compute SimHash of word-level n-gram shingles."""
    tokens = text.lower().split()
    if len(tokens) >= shingle_n:
        shingles = _word_ngrams(tokens, shingle_n)
    else:
        shingles = tokens
    if not shingles:
        return 0
    v = [0] * bits
    for shingle in shingles:
        h = hash(shingle)
        for i in range(bits):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1
    result = 0
    for i in range(bits):
        if v[i] > 0:
            result |= 1 << i
    return result


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def compute_redundancy(
    text: str,
    *,
    shingle_n: int = 3,
    bits: int = 64,
    max_hamming: int = 3,
) -> float:
    """Return ratio of near-duplicate sentences to total sentences (SimHash)."""
    sentences = split_sentences(text)
    if len(sentences) < 2:
        return 0.0
    hashes = [simhash(s, shingle_n=shingle_n, bits=bits) for s in sentences]
    dup_indices: set[int] = set()
    for i in range(1, len(hashes)):
        for j in range(i):
            if hamming_distance(hashes[i], hashes[j]) <= max_hamming:
                dup_indices.add(i)
                break
    return len(dup_indices) / len(sentences)


# ---------------------------------------------------------------------------
# Number extraction
# ---------------------------------------------------------------------------

_NUMBER_RAW = re.compile(r'-?\b\d+(?:[.,\s]\d+)*\b', re.UNICODE)

# Months for date extraction
_MONTH_NAMES: dict[str, str] = {
    # English
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    # Russian
    "январь": "01", "января": "01", "февраль": "02", "февраля": "02",
    "март": "03", "марта": "03", "апрель": "04", "апреля": "04",
    "май": "05", "мая": "05", "июнь": "06", "июня": "06",
    "июль": "07", "июля": "07", "август": "08", "августа": "08",
    "сентябрь": "09", "сентября": "09", "октябрь": "10", "октября": "10",
    "ноябрь": "11", "ноября": "11", "декабрь": "12", "декабря": "12",
    # German
    "januar": "01", "februar": "02", "märz": "03", "april": "04",
    "mai": "05", "juni": "06", "juli": "07", "august": "08",
    "september": "09", "oktober": "10", "november": "11", "dezember": "12",
}


def _normalize_number(s: str) -> str:
    """Normalize a number string: remove spaces, unify decimal separator."""
    s = re.sub(r"\s+", "", s)
    # If comma/dot appears as thousands separator (followed by 3 digits at word boundary)
    s = re.sub(r"[,.](?=\d{3}(?!\d))", "", s)
    return s


def extract_numbers(text: str) -> set[str]:
    """Extract and normalize numeric values from text."""
    result: set[str] = set()
    for m in _NUMBER_RAW.finditer(text):
        normalized = _normalize_number(m.group())
        if normalized and normalized not in {"", "-"}:
            result.add(normalized)
    return result


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

_DATE_ISO = re.compile(r'\b\d{4}-\d{2}-\d{2}\b')
_DATE_DOT = re.compile(r'\b\d{1,2}\.\d{1,2}\.\d{4}\b')
_DATE_SLASH = re.compile(r'\b\d{1,2}/\d{1,2}/\d{4}\b')

# Named month: 1-2 digits + month name or month name + 1-4 digits (year)
_DATE_NAMED = re.compile(
    r'\b(\d{1,2})\s+(' + '|'.join(_MONTH_NAMES) + r')\b'
    r'|\b(' + '|'.join(_MONTH_NAMES) + r')\s+(\d{1,4})\b',
    re.IGNORECASE | re.UNICODE,
)

def extract_dates(text: str) -> set[str]:
    """Extract date-like strings from text and return normalized set."""
    result: set[str] = set()
    for m in _DATE_ISO.finditer(text):
        result.add(m.group())
    for m in _DATE_DOT.finditer(text):
        result.add(m.group())
    for m in _DATE_SLASH.finditer(text):
        result.add(m.group())
    for m in _DATE_NAMED.finditer(text):
        result.add(m.group().lower())
    return result


# ---------------------------------------------------------------------------
# Unit extraction
# ---------------------------------------------------------------------------

_UNIT_PATTERN = (
    r"ms|sec|s|min|h|%|€|\$|gbps|mbps|kbps|gb|mb|kb|tb|pb|ghz|mhz|khz|hz|"
    r"km/h|m/s|km|mm|cm|m|kg|mg|g|kcal|cal|rpm|fps|px|dp|pt|"
    r"секунд|минут|часов|час|лет|год|нед|дней|день|"
    r"кбит|мбит|гбит|кб|мб|гб|тб|км|кг|мин"
)

_UNIT_RE = re.compile(
    r'(-?\d+(?:[.,]\d+)?)\s*(' + _UNIT_PATTERN + r')(?!\w)',
    re.IGNORECASE | re.UNICODE,
)


def extract_units(text: str) -> set[str]:
    """Extract value+unit pairs (normalized lowercase) from text."""
    result: set[str] = set()
    for m in _UNIT_RE.finditer(text):
        num = _normalize_number(m.group(1))
        unit = m.group(2).lower()
        result.add(f"{num}{unit}")
    return result


# ---------------------------------------------------------------------------
# Format metrics
# ---------------------------------------------------------------------------

def format_metrics(text: str) -> dict[str, Any]:
    """Compute format compliance metrics for a summary text."""
    lines = text.splitlines()
    if not lines:
        return {
            "paragraph_count": 0,
            "bullet_ratio": 0.0,
            "heading_count": 0,
            "format_violations": [],
        }

    bullet_lines = sum(
        1 for line in lines if re.match(r"^\s*[-*•]\s", line)
    )
    heading_lines = sum(
        1 for line in lines
        if re.match(r"^\s*#{1,6}\s", line)
        or re.match(r"^[A-ZА-ЯЁ][A-ZА-ЯЁ0-9 ]{2,}:\s*$", line)
    )

    # Paragraph count = number of blocks separated by blank lines
    paragraphs = 0
    in_block = False
    for line in lines:
        if line.strip():
            if not in_block:
                paragraphs += 1
                in_block = True
        else:
            in_block = False

    bullet_ratio = round(bullet_lines / len(lines), 4) if lines else 0.0

    return {
        "paragraph_count": paragraphs,
        "bullet_ratio": bullet_ratio,
        "heading_count": heading_lines,
        "format_violations": [],
    }


# ---------------------------------------------------------------------------
# QualityAnalyzer — main interface
# ---------------------------------------------------------------------------

class QualityAnalyzer:
    """Compute quality metrics for a summary vs its source transcript."""

    def __init__(
        self,
        *,
        shingle_n: int = 3,
        simhash_bits: int = 64,
        max_hamming: int = 3,
    ) -> None:
        self.shingle_n = shingle_n
        self.simhash_bits = simhash_bits
        self.max_hamming = max_hamming

    def analyze(
        self,
        *,
        summary_text: str,
        transcript_text: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Return a flat dict of quality metrics.

        Args:
            summary_text: LLM output to analyze.
            transcript_text: Source text the summary was generated from.
            prompt_tokens: Pre-computed token count for transcript_text (exact).
            completion_tokens: Pre-computed token count for summary_text (exact).
        """
        # Token counts
        if prompt_tokens is not None:
            transcript_tokens = prompt_tokens
            token_estimate = False
        else:
            transcript_tokens, token_estimate = estimate_tokens(transcript_text)

        if completion_tokens is not None:
            summary_tokens = completion_tokens
        else:
            summary_tokens, _ = estimate_tokens(summary_text)

        transcript_chars = len(transcript_text)
        summary_chars = len(summary_text)

        compression_ratio = (
            round(summary_tokens / transcript_tokens, 4) if transcript_tokens > 0 else 0.0
        )

        redundancy = round(
            compute_redundancy(
                summary_text,
                shingle_n=self.shingle_n,
                bits=self.simhash_bits,
                max_hamming=self.max_hamming,
            ),
            4,
        )

        t_numbers = extract_numbers(transcript_text)
        s_numbers = extract_numbers(summary_text)
        t_dates = extract_dates(transcript_text)
        s_dates = extract_dates(summary_text)
        t_units = extract_units(transcript_text)
        s_units = extract_units(summary_text)

        fmt = format_metrics(summary_text)

        return {
            "transcript_tokens": transcript_tokens,
            "transcript_chars": transcript_chars,
            "token_estimate": token_estimate,
            "summary_tokens": summary_tokens,
            "summary_chars": summary_chars,
            "compression_ratio": compression_ratio,
            "redundancy_dup_sentence_ratio": redundancy,
            "numbers_in_summary": len(s_numbers),
            "numbers_in_transcript": len(t_numbers),
            "number_mismatch_count": len(s_numbers - t_numbers),
            "dates_in_summary": len(s_dates),
            "dates_in_transcript": len(t_dates),
            "date_mismatch_count": len(s_dates - t_dates),
            "units_in_summary": len(s_units),
            "units_in_transcript": len(t_units),
            "unit_mismatch_count": len(s_units - t_units),
            "format": fmt,
        }
