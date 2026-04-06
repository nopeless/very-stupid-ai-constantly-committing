from self_improver.patcher import PatchGuard


def test_patch_guard_allows_valid_diff() -> None:
    guard = PatchGuard(
        allowed_paths=["self_improver", "tests"],
        max_patch_bytes=5_000,
        max_patch_paths=3,
        max_patch_hunks=10,
    )
    diff = """diff --git a/self_improver/a.py b/self_improver/a.py
index 1111111..2222222 100644
--- a/self_improver/a.py
+++ b/self_improver/a.py
@@ -1 +1 @@
-x=1
+x=2
"""
    result = guard.validate(diff)
    assert result.ok is True
    assert result.changed_paths == ["self_improver/a.py"]


def test_patch_guard_blocks_disallowed_path() -> None:
    guard = PatchGuard(
        allowed_paths=["self_improver"],
        max_patch_bytes=5_000,
        max_patch_paths=3,
        max_patch_hunks=10,
    )
    diff = """diff --git a/setup.py b/setup.py
index 1111111..2222222 100644
--- a/setup.py
+++ b/setup.py
@@ -1 +1 @@
-a
+b
"""
    result = guard.validate(diff)
    assert result.ok is False
    assert "outside allowlist" in result.message
