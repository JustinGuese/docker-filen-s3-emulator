"""Pure pagination logic: Delimiter -> CommonPrefixes, MaxKeys, StartAfter/Marker."""

from datetime import UTC, datetime

from df_s3_filen_wrapper import Entry

from filen_s3_emulator import listing

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def entries(*keys: str) -> list[Entry]:
    return [Entry(key, len(key), NOW) for key in keys]


def test_no_delimiter_lists_everything_under_prefix():
    page = listing.paginate(
        entries("a/1", "a/2", "b/1"), prefix="a/", delimiter="", max_keys=1000, after=""
    )
    assert [e.key for e in page.contents] == ["a/1", "a/2"]
    assert page.common_prefixes == []
    assert not page.is_truncated


def test_delimiter_groups_into_common_prefixes():
    page = listing.paginate(
        entries("a.txt", "b/1.txt", "b/2.txt", "c/x/y.txt", "d.txt"),
        prefix="",
        delimiter="/",
        max_keys=1000,
        after="",
    )
    assert [e.key for e in page.contents] == ["a.txt", "d.txt"]
    assert page.common_prefixes == ["b/", "c/"]


def test_pagination_resumes_after_a_key():
    all_entries = entries("a", "b", "c", "d")
    page1 = listing.paginate(all_entries, prefix="", delimiter="", max_keys=2, after="")
    assert [e.key for e in page1.contents] == ["a", "b"]
    assert page1.is_truncated
    assert page1.next_marker == "b"

    page2 = listing.paginate(
        all_entries, prefix="", delimiter="", max_keys=2, after=page1.next_marker
    )
    assert [e.key for e in page2.contents] == ["c", "d"]
    assert not page2.is_truncated
    assert page2.next_marker is None


def test_pagination_resumes_inside_a_common_prefix_without_repeating_it():
    all_entries = entries("a", "b/1", "b/2", "b/3", "c")
    page1 = listing.paginate(all_entries, prefix="", delimiter="/", max_keys=2, after="")
    assert [e.key for e in page1.contents] == ["a"]
    assert page1.common_prefixes == ["b/"]
    assert page1.next_marker == "b/"

    page2 = listing.paginate(
        all_entries, prefix="", delimiter="/", max_keys=2, after=page1.next_marker
    )
    assert page2.common_prefixes == []
    assert [e.key for e in page2.contents] == ["c"]


def test_start_after_excludes_the_key_itself():
    page = listing.paginate(entries("a", "b", "c"), prefix="", delimiter="", max_keys=10, after="b")
    assert [e.key for e in page.contents] == ["c"]


def test_max_keys_is_capped_and_parsed():
    assert listing.parse_max_keys(None) == listing.DEFAULT_MAX_KEYS
    assert listing.parse_max_keys("5") == 5
    assert listing.parse_max_keys("999999") == listing.DEFAULT_MAX_KEYS


def test_continuation_token_round_trips_unicode_and_special_characters():
    key = "dir/ü ñame (1)+x.bin"
    assert listing.decode_token(listing.encode_token(key)) == key
