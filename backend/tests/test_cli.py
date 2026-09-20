"""CLI tests driven through Typer's runner against a real temporary corpus.

The commands are the only interface with no schema to validate against, so a wrong column
name or payload key shows up as a blank cell rather than an error. These tests therefore
assert on rendered output, not just exit codes: `dk wiki show` printing a citation table of
dashes exited 0 for several iterations while being useless.

Every command opens its own `session_scope`, which is why each test re-enters the CLI rather
than sharing a session with the fixtures.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from douyin_knowledge.cli.main import app
from douyin_knowledge.config.settings import Settings
from douyin_knowledge.db import init_engine, session_scope
from douyin_knowledge.jobs.handlers import register_default_handlers
from douyin_knowledge.jobs.queue import JobQueue
from douyin_knowledge.jobs.worker import Worker

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch) -> Iterator[Settings]:
    """Point the CLI at a throwaway data dir via the environment.

    The commands call `get_settings()` themselves rather than accepting an injected
    Settings, which is correct for a CLI -- and means the env is the only seam. The cache
    is cleared on both sides so neither this test nor the next inherits the other's paths.
    """
    from douyin_knowledge.config import settings as settings_module
    from douyin_knowledge.db import dispose_engine
    from douyin_knowledge.db.migrate import upgrade_to_head

    monkeypatch.setenv("DK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DK_AI_PROVIDER", "mock")
    monkeypatch.setenv("DK_CAPTURE_PROVIDER", "fixture")
    settings_module.get_settings.cache_clear()

    settings = settings_module.get_settings()
    settings.ensure_directories()
    upgrade_to_head(settings)
    init_engine(settings)
    try:
        yield settings
    finally:
        dispose_engine()
        settings_module.get_settings.cache_clear()


@pytest.fixture
def corpus(cli_env: Settings) -> Settings:
    """A synced and processed corpus, built by the CLI's own sync command."""
    result = runner.invoke(app, ["sync", "--process", "--wait"])
    assert result.exit_code == 0, result.output
    return cli_env


def _drain(settings: Settings) -> int:
    return Worker(register_default_handlers(), settings=settings, name="test").drain(
        max_jobs=200
    )


class TestDoctorAndStatus:
    def test_doctor_reports_ready(self, cli_env: Settings) -> None:
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "schema: ok" in result.output
        assert "demo" in result.output

    def test_doctor_never_prints_a_key(self, cli_env: Settings, monkeypatch) -> None:
        """`doctor` exists to answer "is this configured?" and must not answer "with what?".

        A terminal is the most likely place for a credential to end up in a screenshot or
        a pasted bug report, so the same redaction the API applies has to hold here.
        """
        from douyin_knowledge.config import settings as settings_module

        monkeypatch.setenv("DK_OPENAI_API_KEY", "sk-do-not-print-me")
        settings_module.get_settings.cache_clear()
        result = runner.invoke(app, ["doctor"])
        assert "sk-do-not-print-me" not in result.output
        assert "sk-" not in result.output

    def test_status_counts_an_empty_corpus_as_zero(self, cli_env: Settings) -> None:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "sources: 0" in result.output

    def test_status_json_is_machine_readable(self, corpus: Settings) -> None:
        import json

        result = runner.invoke(app, ["status", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["sources"] > 0
        assert payload["processed"] > 0

    def test_db_status_reports_head(self, cli_env: Settings) -> None:
        result = runner.invoke(app, ["db", "status"])
        assert result.exit_code == 0, result.output
        assert "up_to_date: True" in result.output


class TestPipeline:
    def test_sync_then_process_produces_knowledge(self, corpus: Settings) -> None:
        result = runner.invoke(app, ["status", "--json"])
        import json

        payload = json.loads(result.output)
        assert payload["collections"] > 0
        assert payload["entities"] > 0, "extraction must have run, not just capture"
        assert payload["jobs_failed"] == 0, "a green pipeline is part of the assertion"

    def test_process_without_force_skips_finished_sources(self, corpus: Settings) -> None:
        """The backlog default must not re-spend model calls on work already done.

        Asserted here because the cost of getting this wrong is invisible in mock mode and
        expensive with a real provider.
        """
        result = runner.invoke(app, ["process"])
        assert result.exit_code == 0, result.output
        assert "nothing to process" in result.output

    def test_reindex_is_idempotent(self, corpus: Settings) -> None:
        first = runner.invoke(app, ["search", "茶餐厅", "--json"])
        assert runner.invoke(app, ["reindex"]).exit_code == 0
        _drain(corpus)
        second = runner.invoke(app, ["search", "茶餐厅", "--json"])
        import json

        assert len(json.loads(first.output)["results"]) == len(
            json.loads(second.output)["results"]
        )


class TestQuery:
    def test_search_finds_processed_text(self, corpus: Settings) -> None:
        result = runner.invoke(app, ["search", "茶餐厅"])
        assert result.exit_code == 0, result.output
        assert "好运茶餐厅" in result.output

    def test_search_explains_an_empty_result(self, corpus: Settings) -> None:
        """"no matches" and "nothing indexed" need different reactions from the user."""
        result = runner.invoke(app, ["search", "量子色动力学"])
        assert result.exit_code == 0, result.output
        assert "no matches" in result.output
        assert "keyword_hits" in result.output, "the diagnostics are the actionable part"

    def test_ask_prints_an_answer_with_citations(self, corpus: Settings) -> None:
        result = runner.invoke(app, ["ask", "好运茶餐厅怎么样"])
        assert result.exit_code == 0, result.output
        assert "citations" in result.output
        assert "好运茶餐厅" in result.output

    def test_ask_says_so_when_it_has_no_evidence(self, corpus: Settings) -> None:
        result = runner.invoke(app, ["ask", "冰岛的签证怎么办"])
        assert result.exit_code == 0, result.output
        assert "no matching evidence" in result.output

    def test_ask_rejects_an_unknown_scope(self, corpus: Settings) -> None:
        result = runner.invoke(app, ["ask", "x", "--scope", "everything"])
        assert result.exit_code != 0
        assert "unknown scope" in result.output

    def test_sources_lists_processing_state(self, corpus: Settings) -> None:
        result = runner.invoke(app, ["sources"])
        assert result.exit_code == 0, result.output
        assert "processed" in result.output


class TestWiki:
    def test_wiki_list_shows_compiled_pages(self, corpus: Settings) -> None:
        result = runner.invoke(app, ["wiki", "list"])
        assert result.exit_code == 0, result.output
        assert "好运茶餐厅" in result.output

    def test_wiki_show_resolves_every_citation_to_a_source(self, corpus: Settings) -> None:
        """A fact-level support cites a claim, not a source, so the title needs a join.

        Without it the table rendered a dash for every fact -- a fully cited page looking
        uncited, which inverts the signal WIKI-002 depends on.
        """
        result = runner.invoke(app, ["wiki", "show", "好运茶餐厅"])
        assert result.exit_code == 0, result.output
        assert "this page has no citations" not in result.output
        rows = [
            line
            for line in result.output.splitlines()
            if "fact:" in line or "summary" in line
        ]
        assert rows, "the citation table must have statement rows"
        for row in rows:
            cells = [c.strip() for c in row.strip("│ ").split("│")]
            assert cells[1] and cells[1] != "-", f"unresolved source in: {row}"

    def test_wiki_show_fails_on_an_unknown_page(self, corpus: Settings) -> None:
        result = runner.invoke(app, ["wiki", "show", "不存在的页面"])
        assert result.exit_code != 0


class TestPolicy:
    def test_why_explains_the_default(self, corpus: Settings) -> None:
        from sqlalchemy import select

        from douyin_knowledge.db.models.capture import Source

        with session_scope() as session:
            source_id = session.scalars(select(Source.id)).first()

        result = runner.invoke(app, ["policy", "why", source_id])
        assert result.exit_code == 0, result.output
        assert "process" in result.output
        assert "default_policy" in result.output

    def test_exclude_rule_blocks_a_forced_reprocess(self, corpus: Settings) -> None:
        """The end-to-end promise: a rule added from the terminal stops real work.

        `--force` is used deliberately: the source has already been processed, so a run
        that appears afterwards can only mean the gate did not hold.
        """
        from sqlalchemy import select

        from douyin_knowledge.db.models.capture import Source
        from douyin_knowledge.db.models.policy import SourceProcessingState

        with session_scope() as session:
            source_id = session.scalars(select(Source.id)).first()
            before = session.get(SourceProcessingState, source_id)
            run_before = before.current_processing_run_id

        assert runner.invoke(app, ["policy", "exclude", "--source", source_id]).exit_code == 0
        assert runner.invoke(app, ["process", source_id, "--force"]).exit_code == 0

        with session_scope() as session:
            after = session.get(SourceProcessingState, source_id)
            assert after.current_processing_run_id == run_before, (
                "an excluded source must not get a new current run"
            )

        why = runner.invoke(app, ["policy", "why", source_id])
        assert "exclude" in why.output
        assert "source_rule_matched" in why.output

    def test_delete_restores_what_a_rule_was_hiding(self, corpus: Settings) -> None:
        """Reversibility, from the terminal (DEC-015).

        The CLI could add an exclude rule but not take one back, which left the central
        claim -- that exclusion is a visibility change, not a deletion -- unverifiable
        without the API. Nothing is reprocessed: the same run id comes back.
        """
        from sqlalchemy import select

        from douyin_knowledge.db.models.capture import Source
        from douyin_knowledge.db.models.policy import ProcessingRule, SourceProcessingState

        with session_scope() as session:
            source_id = session.scalars(select(Source.id)).first()
            run_before = session.get(SourceProcessingState, source_id).current_processing_run_id

        assert runner.invoke(app, ["policy", "exclude", "--source", source_id]).exit_code == 0
        with session_scope() as session:
            state = session.get(SourceProcessingState, source_id)
            assert state.current_policy_action == "exclude"
            rule_id = session.scalars(select(ProcessingRule.id)).first()

        result = runner.invoke(app, ["policy", "delete", rule_id])
        assert result.exit_code == 0, result.output
        assert "newly visible" in result.output

        with session_scope() as session:
            state = session.get(SourceProcessingState, source_id)
            assert state.current_policy_action == "process"
            assert state.current_processing_run_id == run_before, (
                "restoring visibility must not have cost a reprocess"
            )

        assert runner.invoke(app, ["policy", "delete", rule_id]).exit_code != 0

    def test_triage_labels_the_corpus_without_a_model(self, corpus: Settings) -> None:
        """`dk policy triage` answers "how much of this is entertainment?" for free.

        Triage is otherwise lazy -- nothing classifies until a semantic rule asks -- so
        this command exists to let a user look before writing the rule. It must not create
        runs, change visibility, or make a model call (DEC-016).
        """
        from douyin_knowledge.db.models.policy import SourceTriage

        result = runner.invoke(app, ["policy", "triage", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["classified"] > 0
        assert payload["model_calls"] == 0
        assert sum(payload["by_content_type"].values()) == payload["classified"]

        with session_scope() as session:
            assert session.query(SourceTriage).count() == payload["classified"]

        # --model without a configured fallback refuses rather than quietly running
        # cue-only and reporting a full pass.
        assert runner.invoke(app, ["policy", "triage", "--model"]).exit_code != 0

    def test_exclude_requires_exactly_one_target(self, cli_env: Settings) -> None:
        assert runner.invoke(app, ["policy", "exclude"]).exit_code != 0
        both = runner.invoke(
            app, ["policy", "exclude", "--source", "src_x", "--creator", "cre_x"]
        )
        assert both.exit_code != 0

    def test_list_says_so_when_there_are_no_rules(self, cli_env: Settings) -> None:
        result = runner.invoke(app, ["policy", "list"])
        assert result.exit_code == 0, result.output
        assert "processed by default" in result.output


class TestWorker:
    def test_worker_once_returns_when_idle(self, cli_env: Settings) -> None:
        result = runner.invoke(app, ["worker", "--once"])
        assert result.exit_code == 0, result.output

    def test_worker_drain_runs_queued_jobs(self, cli_env: Settings) -> None:
        with session_scope() as session:
            JobQueue(session).enqueue("sync_collections")
        result = runner.invoke(app, ["worker", "--drain"])
        assert result.exit_code == 0, result.output
        with session_scope() as session:
            assert JobQueue(session).counts_by_status().get("queued", 0) == 0


class TestEntryPoint:
    """The installed `dk` script and `python -m douyin_knowledge.cli` must behave alike.

    They were not: `[project.scripts]` pointed at the Typer `app`, skipping the `DKError`
    handler in `main`, so a domain error printed a traceback from `dk` and a one-line
    message from `python -m`. Both go through `main` now, and these tests keep it that way
    -- the entry-point string is not covered by any other test, and an editable reinstall
    is required to notice a change in it.
    """

    def test_console_script_routes_through_main(self) -> None:
        # Read as text rather than with tomllib: `requires-python` is >=3.10 and tomllib
        # arrived in 3.11, so parsing would skip this test on the lowest supported version.
        from pathlib import Path

        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        assert 'dk = "douyin_knowledge.cli.main:main"' in pyproject

    def test_main_turns_a_domain_error_into_one_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from douyin_knowledge.cli import main as cli_main
        from douyin_knowledge.core.errors import ConfigurationError

        def boom() -> None:
            raise ConfigurationError("sidecar url missing")

        monkeypatch.setattr(cli_main, "app", boom)
        with pytest.raises(SystemExit) as exit_info:
            cli_main.main()
        assert exit_info.value.code == 1
        # stderr: diagnostics must not contaminate the stdout that `--json` callers parse.
        err = capsys.readouterr().err
        assert "sidecar url missing" in err
        # The bracketed code has to survive rich's markup parser, which silently ate it.
        assert "[configuration_error]" in err
        assert "Traceback" not in err
