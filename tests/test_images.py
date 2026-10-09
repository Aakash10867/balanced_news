"""Outlets' share pictures (Oct 9 2026): found in a page's head or a feed entry, links only."""
import feedparser

from nishpaksh.ingest import clean_image, entry_image, page_image


def test_page_picture_is_og_image_first_then_twitter_and_made_absolute():
    head = ('<meta name="twitter:image" content="https://x.com/t.jpg">'
            '<meta content="/img/a&amp;b.jpg" property="og:image">')
    assert page_image(head, "https://site.in/news/1") == "https://site.in/img/a&b.jpg"
    assert page_image('<meta name="twitter:image:src" content="//cdn.x/y.png">', "https://a.b/") == "https://cdn.x/y.png"
    assert page_image("<p>no picture</p>", "https://a.b/") is None


def test_only_web_links_are_kept():
    assert clean_image("data:image/png;base64,xx") is None
    assert clean_image("javascript:alert(1)") is None
    assert clean_image("https://a.b/" + "x" * 2000) is None


def test_feed_entry_pictures():
    rss = ('<rss xmlns:media="http://search.yahoo.com/mrss/"><channel>'
           '<item><link>https://a.b/1</link><media:content url="https://i.a.b/1.jpg" medium="image"/></item>'
           '<item><link>https://a.b/2</link><enclosure url="https://i.a.b/2.jpg" type="image/jpeg"/></item>'
           '<item><link>https://a.b/3</link><media:thumbnail url="https://i.a.b/3.jpg"/></item>'
           '<item><link>https://a.b/4</link><media:content url="https://v.a.b/4.mp4" medium="video"/></item>'
           '</channel></rss>')
    assert [entry_image(e) for e in feedparser.parse(rss).entries] == [
        "https://i.a.b/1.jpg", "https://i.a.b/2.jpg", "https://i.a.b/3.jpg", None]
