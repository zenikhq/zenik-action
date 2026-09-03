"""report.py: idempotent description block, agent-reply parsing, and the
summary-comment marker that makes re-runs update instead of stack."""
from report import (COMMENT_MARKER, DESC_MARKER_END, DESC_MARKER_START,
                    build_description_block, build_report, merge_description,
                    parse_agent_message, strip_description_block)


def _bundle():
    return {
        "changed": [{"name": "f", "path": "a.py", "start_line": 1,
                     "end_line": 3, "change_type": "modified"}],
        "impacted": [{"symbol": {"name": "g", "path": "svc/b.py",
                                 "start_line": 9},
                      "reason": "calls", "cross_service": True, "via": ["f"]}],
        "tests": [], "truncated": False,
    }


def test_description_merge_is_idempotent_and_preserves_author_text():
    block = build_description_block(_bundle())
    once = merge_description("My PR does X.", block)
    twice = merge_description(once, block)
    assert once == twice
    assert once.startswith("My PR does X.")
    assert once.count(DESC_MARKER_START) == 1
    assert once.count(DESC_MARKER_END) == 1
    assert strip_description_block(once) == "My PR does X."


def test_parse_agent_message_splits_prose_and_json():
    text = ("Summary here.\n\n```json\n"
            '{"per_symbol": [{"name": "f", "note": "ok"}], "overall": "fine"}'
            "\n```")
    prose, structured = parse_agent_message(text)
    assert prose == "Summary here."
    assert structured["overall"] == "fine"
    assert parse_agent_message("no block") == ("no block", None)


def test_build_report_carries_marker_and_trust_footer():
    body = build_report(bundle=_bundle(), agent_result=None, outcome="reported")
    assert body.startswith(COMMENT_MARKER)
    assert "cross-service" in body
    assert "/zenik fix" in body
    assert "only a derived index" in body
