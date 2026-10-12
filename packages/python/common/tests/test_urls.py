from urllib.parse import parse_qsl, urlsplit

from forge_common.urls import encoded_url, query_pairs, with_query


def test_each_name_and_value_is_encoded_whole_and_arrives_as_written() -> None:
    url = with_query(
        "https://api.example.com/search",
        [("q", "red & blue = #1/2?"), ("tag", "a"), ("tag", "b"), ("café", "naïve"), ("empty", "")],
    )
    assert url == (
        "https://api.example.com/search?q=red%20%26%20blue%20%3D%20%231%2F2%3F"
        "&tag=a&tag=b&caf%C3%A9=na%C3%AFve&empty="
    )
    assert parse_qsl(urlsplit(url).query, keep_blank_values=True) == [
        ("q", "red & blue = #1/2?"),
        ("tag", "a"),
        ("tag", "b"),
        ("café", "naïve"),
        ("empty", ""),
    ]


def test_the_urls_own_query_and_fragment_are_kept() -> None:
    assert (
        with_query("https://api.example.com/items?sort=asc#top", [("page", "2")])
        == "https://api.example.com/items?sort=asc&page=2#top"
    )
    assert with_query("https://api.example.com/items", []) == "https://api.example.com/items"


def test_what_a_url_cant_hold_is_encoded_and_the_rest_left_alone() -> None:
    assert encoded_url("https://api.example.com/files/my report.pdf") == (
        "https://api.example.com/files/my%20report.pdf"
    )
    # Escapes already made and reserved characters mean what they did.
    assert encoded_url("https://api.example.com/a%2Fb?x=1&y=[2]") == "https://api.example.com/a%2Fb?x=1&y=[2]"
    assert encoded_url('https://api.example.com/{id}/"quoted"') == (
        "https://api.example.com/%7Bid%7D/%22quoted%22"
    )
    assert query_pairs([]) == ""
