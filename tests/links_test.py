import unittest
from unittest import mock

import gspread
from gspread.exceptions import InvalidLinkError
from gspread.http_client import HTTPClient
from gspread.links import CellLink, extract_cell_links
from gspread.spreadsheet import Spreadsheet
from gspread.worksheet import Worksheet

from .conftest import DUMMY_ACCESS_TOKEN, DummyCredentials

DOCS_URL = "https://docs.example.com"
DRIVE_URL = "https://drive.example.com/file"
OTHER_URL = "https://other.example.com"


def make_cell(text, **kwargs):
    """Build a minimal CellData dict for the given text."""
    cell = {
        "formattedValue": text,
        "userEnteredValue": {"stringValue": text},
    }
    cell.update(kwargs)
    return cell


def link_run(start, uri):
    return {"startIndex": start, "format": {"link": {"uri": uri}}}


def plain_run(start):
    return {"startIndex": start, "format": {}}


def chip_run(start, chip, end=None):
    return {
        "startIndex": start,
        "endIndex": start + 1 if end is None else end,
        "chip": chip,
    }


def link_chip(uri):
    return {"richLinkProperties": {"uri": uri, "title": "chip"}}


class ExtractCellLinksTest(unittest.TestCase):
    def test_empty_cell_has_no_links(self):
        self.assertEqual(extract_cell_links({}, "A1"), [])
        self.assertEqual(extract_cell_links(make_cell("plain text"), "A1"), [])

    def test_plain_url_text_is_not_a_link(self):
        cell = make_cell("https://example.com is not linked")
        self.assertEqual(extract_cell_links(cell, "A1"), [])

    def test_text_run_link(self):
        cell = make_cell(
            "see the docs",
            textFormatRuns=[plain_run(0), link_run(8, DOCS_URL)],
        )
        (link,) = extract_cell_links(cell, "B2")
        self.assertEqual(
            link,
            CellLink(
                cell="B2",
                text="docs",
                start=8,
                end=12,
                url=DOCS_URL,
                kind="text",
            ),
        )

    def test_adjacent_same_target_text_links_merge(self):
        cell = make_cell(
            "aabbccdd",
            textFormatRuns=[
                link_run(0, DOCS_URL),
                link_run(2, DOCS_URL),
                link_run(4, OTHER_URL),
                link_run(6, DOCS_URL),
            ],
        )
        links = extract_cell_links(cell, "A1")
        self.assertEqual(
            [(link.text, link.start, link.end, link.url) for link in links],
            [
                ("aabb", 0, 4, DOCS_URL),
                ("cc", 4, 6, OTHER_URL),
                ("dd", 6, 8, DOCS_URL),
            ],
        )

    def test_same_target_separated_by_plain_text_does_not_merge(self):
        cell = make_cell(
            "aabbcc",
            textFormatRuns=[
                link_run(0, DOCS_URL),
                plain_run(2),
                link_run(4, DOCS_URL),
            ],
        )
        links = extract_cell_links(cell, "A1")
        self.assertEqual(
            [(link.text, link.start, link.end) for link in links],
            [("aa", 0, 2), ("cc", 4, 6)],
        )

    def test_run_without_format_inherits_previous_link(self):
        cell = make_cell(
            "abcdef",
            textFormatRuns=[
                link_run(0, DOCS_URL),
                {"startIndex": 3},  # no format: inherits the link
            ],
        )
        (link,) = extract_cell_links(cell, "A1")
        self.assertEqual((link.text, link.start, link.end), ("abcdef", 0, 6))

    def test_empty_format_resets_inherited_link(self):
        cell = make_cell(
            "abcdef",
            textFormatRuns=[link_run(0, DOCS_URL), plain_run(3)],
        )
        (link,) = extract_cell_links(cell, "A1")
        self.assertEqual((link.text, link.start, link.end), ("abc", 0, 3))

    def test_emoji_offsets_are_utf16_code_units(self):
        # "😀" is 2 UTF-16 code units: link covers utf16 [2, 6) = "ab😀"
        cell = make_cell(
            "😀ab😀cd",
            textFormatRuns=[
                plain_run(0),
                link_run(2, DOCS_URL),
                plain_run(6),
            ],
        )
        (link,) = extract_cell_links(cell, "A1")
        self.assertEqual((link.text, link.start, link.end), ("ab😀", 2, 6))

    def test_invalid_run_offsets_name_the_source_cell(self):
        cases = [
            make_cell("abc", textFormatRuns=[plain_run(5)]),  # out of range
            make_cell("abc", textFormatRuns=[plain_run(-1)]),  # negative
            make_cell("😀x", textFormatRuns=[plain_run(1)]),  # splits the emoji
            make_cell("abc", textFormatRuns=[{"startIndex": "1"}]),  # not an int
            make_cell(  # runs out of order
                "abcdef",
                textFormatRuns=[plain_run(3), plain_run(2)],
            ),
        ]
        for cell in cases:
            with self.subTest(cell=cell):
                with self.assertRaises(InvalidLinkError) as ctx:
                    extract_cell_links(cell, "B2")
                self.assertIn("B2", str(ctx.exception))

    def test_chip_link_covers_only_the_placeholder(self):
        cell = make_cell(
            "see @ now",
            chipRuns=[chip_run(4, link_chip(DRIVE_URL))],
        )
        (link,) = extract_cell_links(cell, "C4")
        self.assertEqual(
            link,
            CellLink(
                cell="C4",
                text="@",
                start=4,
                end=5,
                url=DRIVE_URL,
                kind="chip",
            ),
        )

    def test_chip_end_index_is_clamped_to_the_placeholder(self):
        # the reported endIndex covers "@bc": the chip must not swallow it
        cell = make_cell(
            "a@bc",
            chipRuns=[chip_run(1, link_chip(DRIVE_URL), end=4)],
        )
        (link,) = extract_cell_links(cell, "A1")
        self.assertEqual((link.text, link.start, link.end), ("@", 1, 2))

    def test_chip_between_emoji(self):
        # utf16: 😀 = [0, 2), @ = [2, 3), 😀 = [3, 5)
        cell = make_cell(
            "😀@😀",
            chipRuns=[chip_run(2, link_chip(DRIVE_URL), end=5)],
        )
        (link,) = extract_cell_links(cell, "A1")
        self.assertEqual((link.text, link.start, link.end), ("@", 2, 3))

    def test_chips_are_never_merged(self):
        cell = make_cell(
            "@@",
            chipRuns=[
                chip_run(0, link_chip(DRIVE_URL)),
                chip_run(1, link_chip(DRIVE_URL)),
            ],
        )
        links = extract_cell_links(cell, "A1")
        self.assertEqual([(link.start, link.end) for link in links], [(0, 1), (1, 2)])

    def test_person_and_linkless_chips_are_skipped(self):
        cell = make_cell(
            "@ @ @",
            chipRuns=[
                chip_run(0, {"personProperties": {"email": "a@b.c"}}),
                chip_run(2, {"richLinkProperties": {"title": "no uri"}}),
                chip_run(4, {}),
            ],
        )
        self.assertEqual(extract_cell_links(cell, "A1"), [])

    def test_invalid_chip_positions_name_the_source_cell(self):
        cases = [
            make_cell("abc", chipRuns=[chip_run(1, link_chip(DRIVE_URL))]),  # no "@"
            make_cell("a@", chipRuns=[chip_run(5, link_chip(DRIVE_URL))]),  # past end
            make_cell("@", chipRuns=[{"chip": link_chip(DRIVE_URL)}]),  # no startIndex
        ]
        for cell in cases:
            with self.subTest(cell=cell):
                with self.assertRaises(InvalidLinkError) as ctx:
                    extract_cell_links(cell, "B2")
                self.assertIn("B2", str(ctx.exception))

    def test_whole_cell_link_is_a_fallback(self):
        cell = make_cell("click here", hyperlink=DOCS_URL)
        (link,) = extract_cell_links(cell, "A1")
        self.assertEqual(
            link,
            CellLink(
                cell="A1",
                text="click here",
                start=0,
                end=10,
                url=DOCS_URL,
                kind="cell",
            ),
        )

    def test_whole_cell_link_from_cell_format(self):
        cell = make_cell(
            "😀",
            userEnteredFormat={"hyperlink": DOCS_URL},
        )
        (link,) = extract_cell_links(cell, "A1")
        self.assertEqual(
            (link.text, link.start, link.end, link.kind), ("😀", 0, 2, "cell")
        )

    def test_whole_cell_link_loses_to_fragments(self):
        run_cell = make_cell(
            "see docs",
            hyperlink=OTHER_URL,
            textFormatRuns=[plain_run(0), link_run(4, DOCS_URL)],
        )
        self.assertEqual(
            [(link.kind, link.url) for link in extract_cell_links(run_cell, "A1")],
            [("text", DOCS_URL)],
        )

        chip_cell = make_cell(
            "@",
            hyperlink=OTHER_URL,
            chipRuns=[chip_run(0, link_chip(DRIVE_URL))],
        )
        self.assertEqual(
            [(link.kind, link.url) for link in extract_cell_links(chip_cell, "A1")],
            [("chip", DRIVE_URL)],
        )

    def test_links_are_sorted_by_position(self):
        cell = make_cell(
            "@ ab",
            textFormatRuns=[plain_run(0), link_run(2, DOCS_URL)],
            chipRuns=[chip_run(0, link_chip(DRIVE_URL))],
        )
        links = extract_cell_links(cell, "A1")
        self.assertEqual(
            [(link.kind, link.start, link.end) for link in links],
            [("chip", 0, 1), ("text", 2, 4)],
        )


class GetLinksTest(unittest.TestCase):
    def make_worksheet(self):
        gc = gspread.Client(auth=DummyCredentials(DUMMY_ACCESS_TOKEN))
        with mock.patch.object(
            HTTPClient,
            "fetch_sheet_metadata",
            return_value={"properties": {"id": "ssid", "title": "ss"}},
        ):
            spreadsheet = Spreadsheet(gc.http_client, {"id": "ssid"})
        return Worksheet(
            spreadsheet=spreadsheet,
            properties={
                "sheetId": 0,
                "title": "Sheet1",
                "index": 0,
                "gridProperties": {"rowCount": 100, "columnCount": 26},
            },
            spreadsheet_id="ssid",
            client=gc.http_client,
        )

    def test_get_links(self):
        worksheet = self.make_worksheet()
        response = {
            "sheets": [
                {
                    "properties": {"title": "Sheet1"},
                    "data": [
                        {
                            "startRow": 2,
                            "startColumn": 1,
                            "rowData": [
                                {
                                    "values": [
                                        make_cell("plain"),
                                        make_cell(
                                            "see docs",
                                            textFormatRuns=[
                                                plain_run(0),
                                                link_run(4, DOCS_URL),
                                            ],
                                        ),
                                    ]
                                },
                                {
                                    "values": [
                                        make_cell(
                                            "file @",
                                            chipRuns=[
                                                chip_run(5, link_chip(DRIVE_URL))
                                            ],
                                        ),
                                    ]
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        with mock.patch.object(
            HTTPClient, "fetch_sheet_metadata", return_value=response
        ) as fetch:
            links = worksheet.get_links("B3:D5")

        self.assertEqual(
            links,
            [
                CellLink(
                    cell="C3",
                    text="docs",
                    start=4,
                    end=8,
                    url=DOCS_URL,
                    kind="text",
                ),
                CellLink(
                    cell="B4",
                    text="@",
                    start=5,
                    end=6,
                    url=DRIVE_URL,
                    kind="chip",
                ),
            ],
        )

        # the request targets the worksheet range and asks for link metadata
        (spreadsheet_id,) = fetch.call_args.args
        params = fetch.call_args.kwargs["params"]
        self.assertEqual(spreadsheet_id, "ssid")
        self.assertEqual(params["ranges"], ["'Sheet1'!B3:D5"])
        self.assertIn("textFormatRuns", params["fields"])
        self.assertIn("chipRuns", params["fields"])

    def test_get_links_empty_range(self):
        worksheet = self.make_worksheet()
        with mock.patch.object(
            HTTPClient, "fetch_sheet_metadata", return_value={"sheets": []}
        ):
            self.assertEqual(worksheet.get_links("A1"), [])

    def test_get_links_error_locates_the_source_cell(self):
        worksheet = self.make_worksheet()
        response = {
            "sheets": [
                {
                    "properties": {"title": "Sheet1"},
                    "data": [
                        {
                            "startRow": 0,
                            "startColumn": 1,
                            "rowData": [
                                {
                                    "values": [
                                        make_cell(
                                            "no placeholder here",
                                            chipRuns=[
                                                chip_run(2, link_chip(DRIVE_URL))
                                            ],
                                        )
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with mock.patch.object(
            HTTPClient, "fetch_sheet_metadata", return_value=response
        ):
            with self.assertRaises(InvalidLinkError) as ctx:
                worksheet.get_links("B1")
        self.assertIn("Sheet1", str(ctx.exception))
        self.assertIn("B1", str(ctx.exception))
