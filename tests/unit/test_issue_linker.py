"""STAGE 11.4 — Unit tests for the Issue/PR linker (STAGE 5.5)."""

from __future__ import annotations

import pytest

from omnicode.git_context.issue_linker import IssueLinker, IssueReference


class TestIssueLinkerExtraction:
    def test_extract_github_issue_reference(self):
        refs = IssueLinker.extract("fix #42 NULL deref")
        ids = {r.identifier: r for r in refs}
        assert "#42" in ids
        assert ids["#42"].kind == "github"
        assert ids["#42"].closing is True
        assert ids["#42"].number == 42

    def test_extract_github_alt_with_closing_verb(self):
        refs = IssueLinker.extract("closes GH-7 finally")
        ids = {r.identifier for r in refs}
        assert "GH-7" in ids
        gh7 = next(r for r in refs if r.identifier == "GH-7")
        assert gh7.closing is True

    def test_extract_pull_request_distinct_from_jira(self):
        """PR-99 must be classified as github_pr, not as jira project=PR."""
        refs = IssueLinker.extract("Reviewed PR-99 yesterday")
        kinds = {r.identifier: r.kind for r in refs}
        # The github_pr pattern wins; jira's exclusion list rules out PR-.
        assert "PR-99" in kinds
        assert kinds["PR-99"] == "github_pr"
        # Shouldn't appear as jira too.
        assert sum(1 for r in refs if r.identifier == "PR-99") == 1

    def test_extract_jira_with_project_capture(self):
        refs = IssueLinker.extract("addresses ABC-123 and XYZ-9999")
        ids = {r.identifier: r for r in refs}
        assert "ABC-123" in ids
        assert ids["ABC-123"].project == "ABC"
        assert ids["XYZ-9999"].number == 9999

    def test_jira_excludes_common_non_issue_prefixes(self):
        """PR / GH / AB / MR / CI / CD must NOT be parsed as JIRA projects."""
        refs = IssueLinker.extract("CI-1 and CD-2 and MR-3")
        # If the exclusion regex works, none of these should appear with kind=jira
        jira_refs = [r for r in refs if r.kind == "jira"]
        assert jira_refs == []

    def test_extract_gitlab_mr_pattern(self):
        refs = IssueLinker.extract("synced with !17 in the gitlab fork")
        ids = {r.identifier for r in refs}
        assert "!17" in ids

    def test_extract_azure_devops_pattern(self):
        refs = IssueLinker.extract("see AB#5510 for details")
        ids = {r.identifier for r in refs}
        assert "AB#5510" in ids

    def test_no_duplicates_for_same_reference(self):
        msg = "fix #42, also fixes #42 again, and #42 once more"
        refs = IssueLinker.extract(msg)
        same = [r for r in refs if r.identifier == "#42"]
        assert len(same) == 1

    def test_closing_verb_detection_window(self):
        """Closing verbs must be within ~24 chars of the reference."""
        # 'fix' is far away ⇒ should NOT mark as closing
        far = IssueLinker.extract("fix the build, then mention #42 separately")
        assert all(not r.closing for r in far if r.identifier == "#42")
        # 'fixes' adjacent ⇒ should mark as closing
        near = IssueLinker.extract("fixes #42")
        assert all(r.closing for r in near if r.identifier == "#42")

    def test_empty_input_returns_empty_list(self):
        assert IssueLinker.extract("") == []
        assert IssueLinker.extract(None) == []  # type: ignore[arg-type]

    def test_source_commit_propagated(self):
        refs = IssueLinker.extract("fixes #42", source_commit="abc12345")
        assert all(r.source_commit == "abc12345" for r in refs)


class TestIssueLinkerInstance:
    def test_init_uses_environment_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_xyz")
        linker = IssueLinker(str(tmp_path))
        assert linker.github_token == "ghp_test_xyz"

    def test_explicit_token_overrides_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
        linker = IssueLinker(str(tmp_path), github_token="ghp_arg")
        assert linker.github_token == "ghp_arg"

    def test_disable_network_flag(self, tmp_path):
        linker = IssueLinker(str(tmp_path), enable_network=False)
        # enrich_with_github should be a no-op when network is disabled
        refs = [
            IssueReference(raw="#42", kind="github", identifier="#42", number=42),
        ]
        out = linker.enrich_with_github(refs)
        # Returns the same list, untouched (no state filled in)
        assert out[0].state is None
        assert out[0].title is None
