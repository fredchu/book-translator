from bs4 import BeautifulSoup

from scripts.content_blocks import (
    BLOCK_TAGS,
    TEXT_TAGS,
    extract_blocks,
    extract_paragraphs,
    strip_non_content,
    walk_text_nodes,
)


def _node_texts(html: str, *, skip_empty: bool = True) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [
        node.get_text(" ", strip=True)
        for node in walk_text_nodes(soup, skip_empty=skip_empty)
    ]


def test_public_tag_constants_are_canonical():
    assert TEXT_TAGS == (
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "li",
        "pre",
        "dt",
        "dd",
    )
    assert BLOCK_TAGS == TEXT_TAGS + ("div", "figure", "img")


def test_walk_text_nodes_basic_walk_dedup_and_skip_empty_toggle():
    html = """
    <h1>Title</h1>
    <blockquote><p>Quoted text.</p></blockquote>
    <p>   </p>
    <p>After.</p>
    """
    assert _node_texts(html) == ["Title", "Quoted text.", "After."]
    assert _node_texts(html, skip_empty=False) == [
        "Title",
        "Quoted text.",
        "",
        "After.",
    ]


def test_strip_non_content_is_idempotent_and_leaves_content_nodes():
    soup = BeautifulSoup(
        """
        <header><p>Header text.</p></header>
        <nav><p>Nav text.</p></nav>
        <script>bad()</script>
        <style>.x { color: red; }</style>
        <p>Body text.</p>
        <footer><p>Footer text.</p></footer>
        """,
        "html.parser",
    )
    strip_non_content(soup)
    strip_non_content(soup)
    assert [tag.name for tag in soup.find_all(["header", "nav", "script", "style", "footer"])] == []
    assert [node.get_text(" ", strip=True) for node in walk_text_nodes(soup)] == ["Body text."]


def test_extract_blocks_text_only_and_extract_paragraphs_derives_from_text_blocks():
    html = "<p>First.</p><p>Second.</p>"
    blocks = extract_blocks(html)
    assert blocks == [
        {"type": "text", "text": "First."},
        {"type": "text", "text": "Second."},
    ]
    assert extract_paragraphs(html) == [
        block["text"] for block in blocks if block["type"] == "text"
    ]


def test_extract_blocks_image_only_paragraph_and_ordered_text_image_mix():
    html = """
    <p>Intro.</p>
    <p><img src="../images/figure.png" alt="fig 1"/></p>
    <div><img src="diagram.jpg" alt="diagram"/></div>
    <p>After.</p>
    """
    assert extract_blocks(html) == [
        {"type": "text", "text": "Intro."},
        {"type": "image", "src": "figure.png", "alt": "fig 1"},
        {"type": "image", "src": "diagram.jpg", "alt": "diagram"},
        {"type": "text", "text": "After."},
    ]


def test_extract_blocks_drops_inline_image_inside_text_container():
    html = '<p><img src="../images/icon.jpg" alt=""/> Sure, I would be happy.</p>'
    assert extract_blocks(html) == [
        {"type": "text", "text": "Sure, I would be happy."}
    ]


def test_extract_blocks_bare_image_and_empty_src_drop():
    html = '<body><img src="https://x.com/path/standalone.gif"/><img alt="empty"/></body>'
    assert extract_blocks(html) == [
        {"type": "image", "src": "standalone.gif", "alt": ""}
    ]


def test_nested_li_blockquote_dedups_to_outer_list_item():
    html = "<ul><li><blockquote><p>Nested quoted item.</p></blockquote></li></ul>"
    soup = BeautifulSoup(html, "html.parser")
    nodes = list(walk_text_nodes(soup))
    assert [node.name for node in nodes] == ["li"]
    assert [node.get_text(" ", strip=True) for node in nodes] == ["Nested quoted item."]
    assert extract_paragraphs(html) == ["Nested quoted item."]


def test_pre_with_text_and_image_emits_text_only():
    html = '<pre>Diagram follows <img src="diagram.png" alt="diagram"/></pre>'
    assert extract_blocks(html) == [
        {"type": "text", "text": "Diagram follows"}
    ]


def test_figure_with_figcaption_emits_image_but_not_caption_text():
    html = '<figure><img src="../images/photo.jpg" alt="photo"/><figcaption>Caption</figcaption></figure>'
    assert extract_blocks(html) == [
        {"type": "image", "src": "photo.jpg", "alt": "photo"}
    ]
    assert extract_paragraphs(html) == []


def test_walk_text_nodes_sees_header_footer_when_caller_does_not_strip():
    html = """
    <header><p>Header text.</p></header>
    <p>Body text.</p>
    <footer><p>Footer text.</p></footer>
    """
    assert _node_texts(html) == ["Header text.", "Body text.", "Footer text."]
