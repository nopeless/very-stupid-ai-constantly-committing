from self_improver.utils import extract_json_object, extract_unified_diff


def test_extract_json_object_from_wrapped_text() -> None:
    payload = extract_json_object("prefix {\"a\": 1, \"b\": \"x\"} suffix")
    assert payload["a"] == 1
    assert payload["b"] == "x"


def test_extract_unified_diff_from_fenced_block() -> None:
    raw = """```diff
diff --git a/a.txt b/a.txt
index 1111111..2222222 100644
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
+new
```"""
    diff = extract_unified_diff(raw)
    assert diff.startswith("diff --git")
    assert "+++ b/a.txt" in diff
