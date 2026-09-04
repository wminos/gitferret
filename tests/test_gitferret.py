import asyncio
from pathlib import Path

import pytest
from textual.widgets import DataTable

from gitferret.main import (
    App,
    Configs,
    GitFerretApp,
    discover_repos,
    explain_autostash_failed,
    explain_fast_forward_failed,
    explain_fetch_failed,
    main,
    print_final_report,
    repo_display_name,
    run_git,
    short_text,
    shorten_path,
    truncate,
)


def test_configs_default():
    cfg = Configs()
    assert cfg.sort_mode == "path"
    assert cfg.sort_reverse is False
    assert cfg.show_workers is False
    assert cfg.autoquit is False


def test_configs_save_load(tmp_path: Path):
    cfg_file = tmp_path / "settings.json"
    cfg = Configs(
        sort_mode="state", sort_reverse=True, show_workers=True, autoquit=True
    )
    cfg.save(cfg_file)

    assert cfg_file.exists()
    loaded = Configs.load(cfg_file)
    assert loaded.sort_mode == "state"
    assert loaded.sort_reverse is True
    assert loaded.show_workers is True
    assert loaded.autoquit is True


def test_configs_load_invalid(tmp_path: Path):
    cfg_file = tmp_path / "invalid.json"
    cfg_file.write_text("invalid json content", encoding="utf-8")
    loaded = Configs.load(cfg_file)
    assert loaded.sort_mode == "path"


def test_repo_display_name(tmp_path: Path):
    root = tmp_path / "workspace"
    repo1 = root / "group" / "repo1"
    repo1.mkdir(parents=True)
    assert repo_display_name(root, repo1) == "group/repo1"
    assert repo_display_name(repo1, repo1) == "repo1"


def test_discover_repos(tmp_path: Path):
    root = tmp_path / "workspace"
    repo_a = root / "repo_a"
    repo_b = root / "sub" / "repo_b"
    non_repo = root / "other"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)
    non_repo.mkdir(parents=True)

    repos = discover_repos(root)
    assert len(repos) == 2
    assert repos[0] == repo_a
    assert repos[1] == repo_b


def test_short_text_and_truncate():
    assert short_text("  hello\nworld\r! ") == "hello world !"
    assert truncate("hello world", 5) == "hell…"
    assert truncate("hi", 5) == "hi"


def test_shorten_path():
    # Short paths stay intact
    assert shorten_path("repo1", 20) == "repo1"
    assert shorten_path("group/repo1", 20) == "group/repo1"

    # 3+ parts contract middle parts to '...'
    long_p = "alpha/beta/gamma/delta/my_repo"
    assert shorten_path(long_p, 25) == "alpha/beta/.../my_repo"
    assert shorten_path(long_p, 18) == "alpha/.../my_repo"
    assert shorten_path("_another_agentic/_agy/agy-2", 24) == "_another_ag.../.../agy-2"

    # 2 parts shorten first directory
    assert (
        shorten_path("__external/Monolith Demo - World", 28)
        == "__e.../Monolith Demo - World"
    )

    # Length invariant: result must never exceed max_width
    for w in range(10, 40):
        res = shorten_path("_another_agentic/_copilot/copilot-subagent-worker", w)
        assert len(res) <= w


def test_cli_help():
    with pytest.raises(SystemExit) as exc_info:
        main(["gitferret", "--help"])
    assert exc_info.value.code == 0


def test_gitferret_app_keybindings(tmp_path: Path):
    async def run():
        repo = tmp_path / "repo1"
        (repo / ".git").mkdir(parents=True)
        cfg = Configs()
        engine = App(tmp_path, [repo], cfg, 1)
        app = GitFerretApp(engine)
        async with app.run_test() as pilot:
            assert app.query_one("#repo-table", DataTable) is not None

            # Toggle workers
            assert engine.configs.show_workers is False
            await pilot.press("w")
            assert engine.configs.show_workers is True
            await pilot.press("w")
            assert engine.configs.show_workers is False

            # Sort navigation
            assert app.current_sort_key == "repo"
            await pilot.press(">")
            assert app.current_sort_key == "branch"
            await pilot.press(">")
            assert app.current_sort_key == "state"
            await pilot.press("<")
            assert app.current_sort_key == "branch"
            await pilot.press("s")
            assert app.current_sort_key == "state"

            # Sort reverse
            assert app.sort_reverse is False
            await pilot.press("r")
            assert app.sort_reverse is True
            await pilot.press("r")
            assert app.sort_reverse is False

            # Toggle autoquit action
            app.action_toggle_autoquit()
            assert engine.configs.autoquit is True
            app.action_toggle_autoquit()
            assert engine.configs.autoquit is False

            await pilot.press("q")

    asyncio.run(run())


def test_gitferret_app_header_selected(tmp_path: Path):
    from rich.text import Text
    from textual.widgets.data_table import ColumnKey

    async def run():
        repo = tmp_path / "repo1"
        (repo / ".git").mkdir(parents=True)
        cfg = Configs()
        engine = App(tmp_path, [repo], cfg, 1)
        app = GitFerretApp(engine)
        async with app.run_test():
            table = app.query_one("#repo-table", DataTable)
            app.on_data_table_header_selected(
                DataTable.HeaderSelected(
                    table,
                    column_key=ColumnKey("state"),
                    column_index=3,
                    label=Text("STATE"),
                )
            )
            assert app.current_sort_key == "state"
            assert app.sort_reverse is False

            app.on_data_table_header_selected(
                DataTable.HeaderSelected(
                    table,
                    column_key=ColumnKey("state"),
                    column_index=3,
                    label=Text("STATE"),
                )
            )
            assert app.sort_reverse is True

    asyncio.run(run())


def test_print_final_report_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo = tmp_path / "repo1"
    (repo / ".git").mkdir(parents=True)
    cfg = Configs()
    engine = App(tmp_path, [repo], cfg, 1)
    engine.repos[0].state = "done"
    engine.repos[0].skip_reason = "synced"
    print_final_report(engine)
    captured = capsys.readouterr()
    assert "All repositories already synced with upstream" in captured.out


def test_print_final_report_with_items(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo = tmp_path / "repo1"
    (repo / ".git").mkdir(parents=True)
    cfg = Configs()
    engine = App(tmp_path, [repo], cfg, 1)
    engine.repos[0].state = "skip"
    engine.repos[0].skip_reason = "dirty"
    print_final_report(engine)
    captured = capsys.readouterr()
    assert "repo1" in captured.out
    assert "skip" in captured.out


def test_gitferret_app_navigation(tmp_path: Path):
    async def run():
        repos = [tmp_path / f"repo{i}" for i in range(5)]
        for r in repos:
            (r / ".git").mkdir(parents=True)
        cfg = Configs()
        engine = App(tmp_path, repos, cfg, 1)
        app = GitFerretApp(engine)
        async with app.run_test() as pilot:
            table = app.query_one("#repo-table", DataTable)
            assert table.cursor_coordinate.row == 0

            # Press j / down
            await pilot.press("j")
            assert table.cursor_coordinate.row == 1
            await pilot.press("down")
            assert table.cursor_coordinate.row == 2

            # Press k / up
            await pilot.press("k")
            assert table.cursor_coordinate.row == 1
            await pilot.press("up")
            assert table.cursor_coordinate.row == 0

            # Press G / bottom
            await pilot.press("G")
            assert table.cursor_coordinate.row == 4

            # Press g / top
            await pilot.press("g")
            assert table.cursor_coordinate.row == 0

    asyncio.run(run())


def test_run_git_non_interactive(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    proc = run_git(
        tmp_path, "ls-remote", "https://127.0.0.1:9/fake/nonexistent.git", timeout=1.0
    )
    assert proc.returncode != 0


def test_explain_functions():
    assert "auth required" in explain_fetch_failed(
        "fatal: could not read Username for 'x': terminal prompts disabled"
    )
    assert "timed out" in explain_fetch_failed("git timed out after 20s")
    assert "timed out" in explain_fast_forward_failed("git timed out after 20s")
    assert "timed out" in explain_autostash_failed("git timed out after 20s")


def test_dynamic_column_widths(tmp_path: Path):
    async def run():
        repo = tmp_path / "repo1"
        (repo / ".git").mkdir(parents=True)
        cfg = Configs()

        engine = App(tmp_path, [repo], cfg, 1)
        app = GitFerretApp(engine)
        # Run on a wide terminal (136 cols, matching screenshot)
        async with app.run_test(size=(136, 40)) as pilot:
            table = app.query_one("#repo-table", DataTable)
            widths = app._column_widths(table)
            # Details should expand significantly beyond 36 when terminal is 136 wide
            assert widths["details"] >= 50
            assert widths["repo"] >= 30

            # Resize to smaller terminal
            await pilot.resize_terminal(80, 24)
            widths_small = app._column_widths(table)
            assert widths_small["details"] < widths["details"]

    asyncio.run(run())
