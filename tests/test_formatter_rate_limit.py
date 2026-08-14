from codex_feishu_bridge.feishu.contracts import EndpointContract, RateLimit
import json
import unicodedata

from codex_feishu_bridge.feishu.formatter import (
    format_text_chunks,
    invisible_marker,
    provider_visible_text,
    redact_text,
)
from codex_feishu_bridge.feishu.rate_limit import RateLimiter


def test_formatter_redacts_and_respects_serialized_utf8_budget() -> None:
    text = "Authorization: Bearer abc\napp_secret=xyz\n" + "中" * 1000
    chunks = format_text_chunks(text, marker_seed="fixture", byte_budget=512)
    assert len(chunks) > 1
    assert all(len(item.body_json.encode("utf-8")) <= 512 for item in chunks)
    assert all("abc" not in item.body_json and "xyz" not in item.body_json for item in chunks)
    assert len({item.marker for item in chunks}) == len(chunks)
    assert all("cfb:" not in item.body_json for item in chunks)
    assert all(item.marker not in json.loads(item.body_json)["text"] for item in chunks)
    assert all(
        all(unicodedata.category(character) == "Cf" for character in item.marker)
        for item in chunks
    )


def test_machine_marker_stays_internal_and_never_enters_visible_content() -> None:
    marker = invisible_marker("cfb:3667b48938aefc1cc97fcedc")
    content = "用户可见正文"
    assert "cfb:" not in marker and "3667b48938aefc1cc97fcedc" not in marker
    assert all(unicodedata.category(character) == "Cf" for character in marker)
    assert marker not in content


def test_markdown_link_keeps_label_and_hides_destination() -> None:
    source = (
        "请查看 [当前制造交接文件]"
        "(C:/Projects/example/docs/electronics/handoff.md:36)。"
    )
    visible = provider_visible_text(source)
    assert visible == "请查看 🔗【当前制造交接文件】。"
    assert "D:/" not in visible and "handoff.md" not in visible


def test_codex_file_citation_keeps_link_symbol_and_file_name_only() -> None:
    source = (
        '最新版加工 PDF：:codex-file-citation{path="C:/Projects/example/'
        'docs/manufacturing/ManufacturingPackage.pdf" purpose="output"}'
    )
    visible = provider_visible_text(source)
    assert visible == "最新版加工 PDF：🔗【ManufacturingPackage.pdf】"
    assert "C:/" not in visible
    assert "purpose" not in visible
    assert "codex-file-citation" not in visible


def test_memory_citation_envelope_is_not_provider_visible() -> None:
    source = """正文

<oai-mem-citation>
<citation_entries>
MEMORY.md:1-20|note=[internal context]
</citation_entries>
<rollout_ids>
019fd9d0-2fee-7c53-812f-76b4dced1dc2
</rollout_ids>
</oai-mem-citation>
"""
    visible = provider_visible_text(source)
    assert visible == "正文"
    assert "MEMORY.md" not in visible
    assert "rollout_ids" not in visible


def test_metadata_only_message_produces_no_provider_chunk() -> None:
    source = "<oai-mem-citation><citation_entries>x</citation_entries></oai-mem-citation>"
    assert format_text_chunks(source, marker_seed="fixture") == ()


def test_rate_limiter_independent_buckets_can_reserve() -> None:
    endpoint = EndpointContract(
        name="fixture",
        method="GET",
        path="/open-apis/fixture",
        exact_scopes=("scope",),
        administrator_approved=True,
        enabled=True,
        rate_limit=RateLimit(10, 10, 10, 10),
    )
    limiter = RateLimiter(global_capacity=10, global_refill=10)
    limiter.wait(endpoint, "chat")
    limiter.wait(endpoint, "chat")
