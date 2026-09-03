"""The "Zenik" check-run payload: annotation cap, levels, conclusion mapping."""
from report import (CHECK_RUN_NAME, build_check_annotations, build_check_run,
                    check_conclusion)


def _changed(name, path="svc/a.py", start=10, end=20, change_type="modified"):
    return {"name": name, "kind": "function", "path": path, "language": "python",
            "start_line": start, "end_line": end, "change_type": change_type}


def _impacted(name, via, cross=False, path="svc/b.py"):
    return {"symbol": {"name": name, "path": path, "start_line": 1},
            "reason": "calls", "depth": 1, "confidence": 0.9,
            "cross_service": cross, "score": 1.0, "via": [via]}


def test_conclusion_mapping():
    empty = {"changed": [], "impacted": [], "tests": []}
    found = {"changed": [_changed("f")], "impacted": [_impacted("g", "f")],
             "tests": []}
    assert check_conclusion(empty, "no_impact") == "success"
    assert check_conclusion(found, "reported") == "neutral"     # never blocks
    assert check_conclusion(found, "agent_failed") == "failure"
    assert check_conclusion(empty, "agent_failed") == "failure"


def test_annotation_levels_and_message():
    bundle = {
        "changed": [_changed("f"), _changed("h", start=30, end=40)],
        "impacted": [_impacted("g", "f"), _impacted("k", "f", cross=True),
                     _impacted("m", "h")],
        "tests": [],
    }
    ann = build_check_annotations(bundle)
    assert [a["annotation_level"] for a in ann] == ["warning", "notice"]
    a = ann[0]
    assert a["path"] == "svc/a.py"
    assert (a["start_line"], a["end_line"]) == (10, 20)
    assert "affects 2 caller(s)" in a["message"]
    assert "g" in a["message"] and "k" in a["message"]
    assert ann[1]["message"].startswith("`h` affects 1 caller(s): m")


def test_annotations_skip_deleted_and_callerless_symbols():
    bundle = {
        "changed": [_changed("gone", change_type="deleted"),
                    _changed("lonely"), _changed("f")],
        "impacted": [_impacted("x", "gone"), _impacted("g", "f")],
        "tests": [],
    }
    ann = build_check_annotations(bundle)
    assert [a["title"] for a in ann] == ["Zenik: f"]


def test_annotations_capped_at_50():
    changed = [_changed(f"f{i}", start=i + 1, end=i + 1) for i in range(80)]
    impacted = [_impacted(f"g{i}", f"f{i}") for i in range(80)]
    ann = build_check_annotations({"changed": changed, "impacted": impacted,
                                   "tests": []})
    assert len(ann) == 50


def test_build_check_run_shape():
    bundle = {"changed": [_changed("f")], "impacted": [_impacted("g", "f")],
              "tests": [_impacted("test_g", "f", path="tests/test_b.py")]}
    payload = build_check_run(bundle, "reported", "abc123")
    assert payload["name"] == CHECK_RUN_NAME == "Zenik"
    assert payload["head_sha"] == "abc123"
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "neutral"
    out = payload["output"]
    assert out["title"] == "Zenik: 1 affected site(s)"
    assert "1 test(s) likely relevant" in out["summary"]
    assert len(out["annotations"]) == 1
