"""
gspread.links
~~~~~~~~~~~~~

Read-only extraction of hyperlinks and smart chip links from cell data.

The Sheets API describes the links of a cell with three mechanisms:

* ``textFormatRuns``: format runs over the cell text, a run may carry a
  ``format.link.uri``. A run without a ``format`` key inherits the format
  of the previous run. All offsets are expressed in UTF-16 code units.
* ``chipRuns``: smart chips. A chip is materialised in the cell text by a
  single ``@`` placeholder character and only ever covers that character.
* ``hyperlink`` (deprecated): a link applied to the whole cell, only used
  here as a fallback when the cell carries no fragment-level link.

"""

from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

from .exceptions import InvalidLinkError

#: Character used by the Sheets API as an in-text placeholder for a smart
#: chip. A chip run only ever covers that placeholder, whatever ``endIndex``
#: the API reports: it must never swallow neighbouring characters.
CHIP_PLACEHOLDER = "@"

#: Field mask requesting everything :func:`extract_cell_links` needs,
#: used by :meth:`gspread.worksheet.Worksheet.get_links`.
CELL_LINK_FIELDS = ",".join(
    [
        "sheets.data.startRow",
        "sheets.data.startColumn",
        "sheets.data.rowData.values.userEnteredValue",
        "sheets.data.rowData.values.formattedValue",
        "sheets.data.rowData.values.textFormatRuns",
        "sheets.data.rowData.values.chipRuns",
        "sheets.data.rowData.values.hyperlink",
        "sheets.data.rowData.values.userEnteredFormat.hyperlink",
        "sheets.data.rowData.values.effectiveFormat.hyperlink",
    ]
)


@dataclass(frozen=True)
class CellLink:
    """A single link found in a cell.

    Instances of this class are returned by
    :meth:`gspread.worksheet.Worksheet.get_links`, they should not be
    created manually.
    """

    #: A1 notation of the cell the link was found in, e.g. ``"B2"``.
    cell: str

    #: Fragment of the cell text the link is applied to. For a smart chip
    #: this is the ``@`` placeholder character.
    text: str

    #: Start offset of the fragment in the cell text, in UTF-16 code units
    #: (inclusive) — the unit the Sheets API uses, where e.g. an emoji
    #: counts for 2 units.
    start: int

    #: End offset of the fragment in the cell text, in UTF-16 code units
    #: (exclusive).
    end: int

    #: Target URL of the link.
    url: str

    #: ``"text"`` for a link on a text fragment, ``"chip"`` for a smart
    #: chip link, ``"cell"`` for a link applied to the whole cell.
    kind: str = "text"


def _utf16_offsets(text: str) -> List[int]:
    """Return the cumulative UTF-16 code unit count at each index of ``text``.

    ``offsets[i]`` is the number of UTF-16 code units needed to encode
    ``text[:i]``. Characters outside the basic multilingual plane (emoji
    and other multi-code-unit characters) count for 2 units.
    """
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    return offsets


def _resolve_offset(
    offsets: List[int],
    offset: Any,
    address: str,
    location: str,
) -> int:
    """Translate a UTF-16 code unit ``offset`` into a Python string index.

    ``offsets`` comes from :func:`_utf16_offsets`. Raises
    :class:`~gspread.exceptions.InvalidLinkError` — pointing at the source
    cell ``address`` — when the offset is not a valid text boundary.
    """
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise InvalidLinkError(
            "cell {}: {} must be an integer, got {!r}".format(address, location, offset)
        )
    if offset < 0 or offset > offsets[-1]:
        raise InvalidLinkError(
            "cell {}: {} is {} but the cell text is only {} UTF-16 code units".format(
                address, location, offset, offsets[-1]
            )
        )
    index = bisect_left(offsets, offset)
    if offsets[index] != offset:
        raise InvalidLinkError(
            "cell {}: {} is {} which splits a multi-code-unit character".format(
                address, location, offset
            )
        )
    return index


def _cell_text(cell_data: Mapping[str, Any]) -> str:
    """Return the cell text that link and chip offsets apply to."""
    user_entered_value = cell_data.get("userEnteredValue")
    if isinstance(user_entered_value, dict):
        string_value = user_entered_value.get("stringValue")
        if isinstance(string_value, str):
            return string_value
    formatted_value = cell_data.get("formattedValue")
    return formatted_value if isinstance(formatted_value, str) else ""


def _link_uri(cell_format: Mapping[str, Any]) -> Optional[str]:
    """Return the link URI of a ``TextFormat``, if it carries one."""
    link = cell_format.get("link")
    if not isinstance(link, dict):
        return None
    return link.get("uri") or None


def _text_run_links(
    cell_data: Mapping[str, Any],
    text: str,
    offsets: List[int],
    address: str,
) -> List[CellLink]:
    runs = cell_data.get("textFormatRuns") or []

    # Resolve the effective format of every run first: a run without a
    # "format" key inherits the format of the previous run (an empty
    # format resets it). Store (start, start as python index, uri).
    points: List[Tuple[int, int, Optional[str]]] = []
    inherited_format: Mapping[str, Any] = {}
    previous_start = 0
    for position, run in enumerate(runs):
        location = "textFormatRuns[{}].startIndex".format(position)
        start = run.get("startIndex", 0)
        start_index = _resolve_offset(offsets, start, address, location)
        if start < previous_start:
            raise InvalidLinkError(
                "cell {}: {} is {} which is before the previous "
                "run start {}".format(address, location, start, previous_start)
            )
        previous_start = start
        if "format" in run:
            inherited_format = run["format"] or {}
        points.append((start, start_index, _link_uri(inherited_format)))

    # Cut the runs into [start, end) segments and merge adjacent segments
    # that point to the same URL into a single link.
    merged: List[List[Any]] = []
    for position, (start, start_index, uri) in enumerate(points):
        if position + 1 < len(points):
            end, end_index = points[position + 1][:2]
        else:
            end, end_index = offsets[-1], len(text)
        if uri is None or end <= start:
            continue
        if merged and merged[-1][1] == start and merged[-1][2] == uri:
            merged[-1][1] = end
            merged[-1][4] = end_index
        else:
            merged.append([start, end, uri, start_index, end_index])

    return [
        CellLink(
            cell=address,
            text=text[start_index:end_index],
            start=start,
            end=end,
            url=uri,
            kind="text",
        )
        for start, end, uri, start_index, end_index in merged
    ]


def _chip_links(
    cell_data: Mapping[str, Any],
    text: str,
    offsets: List[int],
    address: str,
) -> List[CellLink]:
    links = []
    for position, chip_run in enumerate(cell_data.get("chipRuns") or []):
        location = "chipRuns[{}].startIndex".format(position)
        start = chip_run.get("startIndex")
        if start is None:
            raise InvalidLinkError("cell {}: {} is missing".format(address, location))
        start_index = _resolve_offset(offsets, start, address, location)
        if start_index >= len(text):
            raise InvalidLinkError(
                "cell {}: {} is {} but the cell text is only {} UTF-16 code "
                "units, a chip must point at its {!r} placeholder".format(
                    address, location, start, offsets[-1], CHIP_PLACEHOLDER
                )
            )
        if text[start_index] != CHIP_PLACEHOLDER:
            raise InvalidLinkError(
                "cell {}: {} points at {!r} instead of a chip "
                "placeholder {!r}".format(
                    address, location, text[start_index], CHIP_PLACEHOLDER
                )
            )

        chip = chip_run.get("chip") or {}
        if "personProperties" in chip:
            # person chips are not links
            continue
        rich_link = chip.get("richLinkProperties")
        uri = rich_link.get("uri") if isinstance(rich_link, dict) else None
        if not uri:
            continue

        # A chip only occupies its single "@" placeholder: hard-clamp the
        # span to that character so it can never swallow or bleed into
        # neighbouring text, whatever endIndex the API reported.
        links.append(
            CellLink(
                cell=address,
                text=CHIP_PLACEHOLDER,
                start=start,
                end=start + 1,
                url=uri,
                kind="chip",
            )
        )
    return links


def _whole_cell_uri(cell_data: Mapping[str, Any]) -> Optional[str]:
    """Return the deprecated whole-cell hyperlink of a ``CellData``, if any."""
    uri = cell_data.get("hyperlink")
    if uri:
        return uri
    for key in ("userEnteredFormat", "effectiveFormat"):
        cell_format = cell_data.get(key)
        if isinstance(cell_format, dict) and cell_format.get("hyperlink"):
            return cell_format["hyperlink"]
    return None


def extract_cell_links(cell_data: Mapping[str, Any], address: str) -> List[CellLink]:
    """Extract every link from a single ``CellData`` resource.

    * Text links come from ``textFormatRuns`` (resolving format
      inheritance); adjacent fragments pointing to the same URL are merged
      into a single link.
    * Smart chips come from ``chipRuns``; each chip is returned
      individually and only covers its ``@`` placeholder. Person chips are
      skipped.
    * When the cell carries no fragment-level link at all, a deprecated
      whole-cell ``hyperlink`` is returned as a fallback.

    Plain text that merely looks like a URL is never considered a link.

    :param dict cell_data: a `CellData`_ resource as returned by the Sheets
        API (see ``spreadsheets.get`` with grid data).
    :param str address: A1 notation of the cell the data comes from, used
        in the returned links and in error messages.
    :returns: the links of the cell, sorted by position in the cell text.
    :rtype: list of :class:`CellLink`
    :raises ~gspread.exceptions.InvalidLinkError: if an offset or a chip
        position is invalid; the error message names the source cell.

    .. _CellData: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/cells#CellData
    """
    text = _cell_text(cell_data)
    offsets = _utf16_offsets(text)

    links = _text_run_links(cell_data, text, offsets, address)
    links += _chip_links(cell_data, text, offsets, address)

    if not links:
        uri = _whole_cell_uri(cell_data)
        if uri is not None:
            links.append(
                CellLink(
                    cell=address,
                    text=text,
                    start=0,
                    end=offsets[-1],
                    url=uri,
                    kind="cell",
                )
            )

    links.sort(key=lambda link: (link.start, link.end))
    return links
