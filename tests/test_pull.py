# tests/test_pull.py
import responses

from brain import pull

PROPFIND_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response><d:href>/webhook_share/conversations/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
  <d:response><d:href>/webhook_share/conversations/u7910.jsonl</d:href>
    <d:propstat><d:prop><d:getetag>"aaa"</d:getetag><d:resourcetype/></d:prop></d:propstat></d:response>
</d:multistatus>"""


def test_parse_listing_extracts_files():
    entries = pull.parse_listing(PROPFIND_XML)
    files = [e for e in entries if not e["is_dir"]]
    assert len(files) == 1
    assert files[0]["name"] == "u7910.jsonl"
    assert files[0]["etag"] == '"aaa"'


@responses.activate
def test_pull_downloads_changed_and_skips_unchanged(tmp_path):
    base = "https://nas.example.com/webhook_share/"
    responses.add("PROPFIND", "https://nas.example.com/webhook_share/conversations/", body=PROPFIND_XML, status=207)
    responses.add(responses.GET, "https://nas.example.com/webhook_share/conversations/u7910.jsonl",
                  body='{"mid": 1}\n', status=200, headers={"ETag": '"aaa"'})

    client = pull.WebDAVClient(base, "u", "p", verify=False)
    state = {"conversations": {}}
    new_state = pull.pull_conversations(client, "conversations/", state, str(tmp_path))

    assert (tmp_path / "u7910.jsonl").read_text(encoding="utf-8") == '{"mid": 1}\n'
    assert new_state["conversations"]["u7910"]["etag"] == '"aaa"'

    # 第二轮:etag 未变 → 不应再产生 GET(仅 PROPFIND)
    calls_before = len(responses.calls)
    pull.pull_conversations(client, "conversations/", new_state, str(tmp_path))
    get_calls = [c for c in responses.calls[calls_before:] if c.request.method == "GET"]
    assert get_calls == []
