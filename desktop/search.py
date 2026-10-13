"""Shared local search normalization for the desktop UI.

Windows ships the Chinese variant mapping used here in ``LCMapStringEx``.
Using that API keeps the frozen application self-contained while handling the
full Windows NLS mapping instead of maintaining a small, incomplete character
table in the project.
"""

from __future__ import annotations

import ctypes
import sys
import unicodedata
from ctypes import wintypes
from functools import lru_cache
from typing import Any, Callable, Optional


# WinNLS.h values.  The flags are documented by Microsoft for LCMapStringEx:
# LCMAP_SIMPLIFIED_CHINESE maps traditional characters to simplified ones;
# LCMAP_TRADITIONAL_CHINESE maps simplified characters to traditional ones.
_LCMAP_SIMPLIFIED_CHINESE = 0x02000000


def _load_windows_mapper() -> Optional[Callable[..., int]]:
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        mapper = kernel32.LCMapStringEx
        mapper.argtypes = [
            ctypes.c_wchar_p,
            wintypes.DWORD,
            ctypes.c_wchar_p,
            wintypes.INT,
            ctypes.c_wchar_p,
            wintypes.INT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.LPARAM,
        ]
        mapper.restype = wintypes.INT
        return mapper
    except (AttributeError, OSError, TypeError):
        # The desktop target is Windows Vista or newer, but keeping the
        # helper importable on other platforms makes offline UI tests useful.
        return None


_WINDOWS_MAPPER = _load_windows_mapper()


def _simplify_chinese(value: str) -> str:
    if not value or _WINDOWS_MAPPER is None:
        return value
    try:
        # LCMapStringEx counts UTF-16 code units, while Python's len() counts
        # Unicode code points.  Build explicit wchar buffers so emoji and
        # other non-BMP characters do not truncate the source or result.
        source_bytes = value.encode("utf-16-le", "surrogatepass")
        source_units = len(source_bytes) // 2
        source = (ctypes.c_wchar * source_units).from_buffer_copy(source_bytes)

        # Passing the source length explicitly avoids treating an embedded NUL
        # as the end of the text.  The first call asks Windows for the exact
        # output size, including any one-to-many mappings.
        needed = _WINDOWS_MAPPER(
            "zh-TW",
            _LCMAP_SIMPLIFIED_CHINESE,
            source,
            source_units,
            None,
            0,
            None,
            None,
            0,
        )
        if needed <= 0:
            return value
        destination = (ctypes.c_wchar * needed)()
        written = _WINDOWS_MAPPER(
            "zh-TW",
            _LCMAP_SIMPLIFIED_CHINESE,
            source,
            source_units,
            destination,
            needed,
            None,
            None,
            0,
        )
        if written <= 0:
            return value
        mapped_bytes = ctypes.string_at(ctypes.addressof(destination), written * 2)
        return mapped_bytes.decode("utf-16-le", "surrogatepass")
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError):
        # A local search should remain usable if a restricted Windows runtime
        # refuses the optional NLS call.  The canonical Unicode/casefold text
        # is still returned in that rare case.
        return value


@lru_cache(maxsize=4096)
def _normalized_search_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return _simplify_chinese(value)


def normalize_search_text(value: Any) -> str:
    """Return a case-insensitive, Traditional/Simplified-insensitive key."""

    if value is None:
        return ""
    return _normalized_search_key(str(value))


def search_matches(query: Any, *values: Any) -> bool:
    """Return whether *query* occurs in any supplied local-search value."""

    needle = normalize_search_text(query).strip()
    if not needle:
        return True
    return any(needle in normalize_search_text(value) for value in values)


__all__ = ["normalize_search_text", "search_matches"]
