from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from rich.text import Text
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

MAX_JOBS = min(4, max(1, os.cpu_count() or 1))
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_WHITE = "\033[37m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_CYAN = "\033[36m"
ANSI_MAGENTA = "\033[35m"
DEFAULT_ROOT = "."


def config_path() -> Path:
    return Path.home() / ".gitferret" / "settings.json"


CONFIG_PATH = config_path()


class AlignedHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    def __init__(self, prog: str) -> None:
        terminal_width = shutil.get_terminal_size(fallback=(88, 24)).columns
        max_help_position = max(32, min(40, terminal_width // 2))
        super().__init__(
            prog, max_help_position=max_help_position, width=terminal_width
        )


def short_text(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def explain_dirty(repo: Path) -> str:
    return f"local changes exist; trying autostash for {repo.name}"


def explain_ahead(ahead: int, behind: int) -> str:
    return f"local commits not pushed ({ahead} ahead, {behind} behind); push or rebase first"


def explain_no_upstream() -> str:
    return "no upstream set; use git branch --set-upstream-to or git push -u"


def explain_fetch_failed(stderr: str = "") -> str:
    text = stderr.lower()
    if (
        "terminal prompts disabled" in text
        or "could not read username" in text
        or "authentication" in text
    ):
        return "auth required; terminal prompts disabled"
    if "timed out" in text:
        return "fetch timed out; check network connection"
    return "fetch failed; check network or remote access and retry"


def explain_fast_forward_failed(stderr: str = "") -> str:
    text = stderr.lower()
    if "timed out" in text:
        return "pull timed out; check network connection"
    return "ff-only pull failed; history diverged or changed during fetch"


def explain_autostash_failed(stderr: str = "") -> str:
    text = stderr.lower()
    if "timed out" in text:
        return "pull timed out; check network connection"
    return "autostash pull failed; stash/review local changes and retry"


def autostash_had_conflicts(stderr: str) -> bool:
    text = stderr.lower()
    return "autostash" in text and "conflict" in text


def explain_up_to_date() -> str:
    return "already synced with upstream"


DEFAULT_GIT_TIMEOUT = 20.0


def run_git(
    repo: Path, *args: str, timeout: float = DEFAULT_GIT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(repo), *args],
            returncode=124,
            stdout="",
            stderr=f"git timed out after {timeout:.0f}s",
        )


def discover_repos(root: Path) -> list[Path]:
    repos: list[Path] = []
    if not root.is_dir():
        return repos
    for current, dirs, _files in os.walk(root):
        dirs.sort()
        current_path = Path(current)
        if (current_path / ".git").is_dir():
            repos.append(current_path)
            dirs[:] = []
    return sorted(repos, key=lambda repo: repo.relative_to(root).as_posix())


def repo_display_name(root: Path, repo: Path) -> str:
    relative = repo.relative_to(root)
    if str(relative) == ".":
        return repo.name
    return relative.as_posix()


def repo_local_branch(repo: Path) -> str:
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.returncode != 0:
        return ""
    return short_text(branch.stdout)


def use_ansi_output() -> bool:
    return sys.stdout.isatty() and sys.stderr.isatty() and "NO_COLOR" not in os.environ


def ansi(text: str, *codes: str) -> str:
    if not codes:
        return text
    prefix = "".join(codes)
    return f"{prefix}{text}{ANSI_RESET}"


def ansi_for_state(state: str, text: str) -> str:
    if state == "done":
        return ansi(text, ANSI_GREEN)
    if state == "skip":
        return ansi(text, ANSI_YELLOW)
    if state == "running":
        return ansi(text, ANSI_CYAN)
    if state in {"queued", "idle"}:
        return ansi(text, ANSI_MAGENTA)
    return ansi(text, ANSI_WHITE)


@dataclass
class RepoState:
    index: int
    path: Path
    name: str
    state: str = "queued"
    state_msg: str = "waiting"
    details: list[str] = field(default_factory=list)
    branch: str = ""
    slot: int | None = None
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class SlotState:
    index: int
    repo_index: int | None = None
    state: str = "idle"
    detail: str = "-"
    branch: str = ""
    updated_at: float = field(default_factory=time.time)


@dataclass
class Configs:
    sort_mode: str = "path"
    sort_reverse: bool = False
    show_workers: bool = False
    autoquit: bool = False

    @classmethod
    def load(cls, path: Path) -> Configs:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return cls()
        if not isinstance(payload, dict):
            return cls()

        sort_mode = payload.get("sort_mode", "path")
        if sort_mode not in {"path", "state", "branch", "no"}:
            sort_mode = "path"

        return cls(
            sort_mode=sort_mode,
            sort_reverse=bool(payload.get("sort_reverse", False)),
            show_workers=bool(payload.get("show_workers", False)),
            autoquit=bool(payload.get("autoquit", False)),
        )

    def save(self, path: Path) -> None:
        data = {
            "sort_mode": self.sort_mode,
            "sort_reverse": self.sort_reverse,
            "show_workers": self.show_workers,
            "autoquit": self.autoquit,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            return


class App:
    def __init__(
        self, root: Path, repos: list[Path], configs: Configs, worker_count: int
    ):
        self.root = root
        self.repos = [
            RepoState(index=i, path=repo, name=repo_display_name(root, repo))
            for i, repo in enumerate(repos)
        ]
        self.configs = configs
        self.worker_count = worker_count
        self.slots = [SlotState(index=i) for i in range(worker_count)]
        self.todo: queue.Queue[int] = queue.Queue()
        for i in range(len(self.repos)):
            self.todo.put(i)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.finished = 0
        self.total = len(self.repos)
        self.tempdir = Path(tempfile.mkdtemp(prefix="pull-all-repos-"))
        self.shutdown_status = ""
        self.version = 0

    def cleanup(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def set_repo(self, idx: int, **changes: object) -> None:
        with self.lock:
            repo = self.repos[idx]
            for key, value in changes.items():
                setattr(repo, key, value)
            self.version += 1

    def set_slot(self, idx: int, **changes: object) -> None:
        with self.lock:
            slot = self.slots[idx]
            for key, value in changes.items():
                setattr(slot, key, value)
            slot.updated_at = time.time()
            self.version += 1

    def mark_finished(self, idx: int, success: bool) -> None:
        with self.lock:
            self.finished += 1
            self.repos[idx].finished_at = time.time()
            if not success and self.repos[idx].state == "running":
                self.repos[idx].state = "skip"
            self.version += 1

    def finalize_stopped_repos(self) -> None:
        with self.lock:
            now = time.time()
            for repo in self.repos:
                if repo.state == "queued":
                    repo.state = "skip"
                    repo.state_msg = "not started before quit"
                    repo.details = []
                    repo.finished_at = now
            self.version += 1

    def worker(self, slot_idx: int) -> None:
        while not self.stop.is_set():
            try:
                repo_idx = self.todo.get_nowait()
            except queue.Empty:
                break

            try:
                repo = self.repos[repo_idx]
                self.set_slot(
                    slot_idx,
                    repo_index=repo_idx,
                    state="running",
                    detail="starting",
                    branch=repo.name,
                )
                self.set_repo(
                    repo_idx,
                    state="running",
                    state_msg="scanning",
                    details=[],
                    slot=slot_idx,
                    started_at=time.time(),
                )

                probe = run_git(repo.path, "rev-parse", "--is-inside-work-tree")
                if probe.returncode != 0 or short_text(probe.stdout) != "true":
                    detail = "not a git work tree; skip"
                    self.set_repo(repo_idx, state="skip", state_msg=detail, details=[])
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                branch_name = repo_local_branch(repo.path)
                if branch_name:
                    self.set_repo(repo_idx, branch=branch_name)
                    self.set_slot(slot_idx, branch=branch_name)

                dirty = run_git(repo.path, "status", "--porcelain")
                has_dirty_worktree = bool(short_text(dirty.stdout))
                if has_dirty_worktree:
                    detail = explain_dirty(repo.path)
                    self.set_repo(repo_idx, state_msg=detail, details=[])
                    self.set_slot(slot_idx, detail=detail)

                upstream = run_git(
                    repo.path,
                    "rev-parse",
                    "--abbrev-ref",
                    "--symbolic-full-name",
                    "@{u}",
                )
                upstream_ref = short_text(upstream.stdout)
                if upstream.returncode != 0 or not upstream_ref:
                    detail = explain_no_upstream()
                    self.set_repo(repo_idx, state="skip", state_msg=detail, details=[])
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                self.set_repo(repo_idx, state_msg="fetching upstream", details=[])
                self.set_slot(slot_idx, detail="fetching upstream")
                fetch = run_git(repo.path, "fetch", "--prune", "--quiet")
                if fetch.returncode != 0:
                    detail = explain_fetch_failed(fetch.stderr)
                    self.set_repo(repo_idx, state="skip", state_msg=detail, details=[])
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                compare = run_git(
                    repo.path,
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"HEAD...{upstream_ref}",
                )
                if compare.returncode != 0:
                    detail = "compare failed; retry later"
                    if compare.stderr:
                        detail = (
                            f"{detail}: {short_text(compare.stderr).split(':', 1)[0]}"
                        )
                    self.set_repo(repo_idx, state="skip", state_msg=detail, details=[])
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                parts = short_text(compare.stdout).split()
                ahead = behind = 0
                if len(parts) >= 2:
                    ahead, behind = int(parts[0]), int(parts[1])

                if ahead != 0:
                    detail = explain_ahead(ahead, behind)
                    self.set_repo(repo_idx, state="skip", state_msg=detail, details=[])
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                if behind == 0:
                    details = ["local changes preserved"] if has_dirty_worktree else []
                    detail = explain_up_to_date()
                    self.set_repo(
                        repo_idx,
                        state="done",
                        state_msg=detail,
                        details=details,
                    )
                    self.set_slot(
                        slot_idx,
                        state="done",
                        detail=repo_detail_text(self.repos[repo_idx]),
                    )
                    self.mark_finished(repo_idx, success=True)
                    continue

                if has_dirty_worktree:
                    pulling_detail = f"pulling with autostash ({behind} behind)"
                else:
                    pulling_detail = f"pulling ff-only ({behind} behind)"
                self.set_repo(repo_idx, state_msg=pulling_detail, details=[])
                self.set_slot(slot_idx, detail=pulling_detail)
                pull_args = ["pull", "--ff-only"]
                if has_dirty_worktree:
                    pull_args.append("--autostash")
                pull = run_git(repo.path, *pull_args)
                if pull.returncode == 0 and not autostash_had_conflicts(pull.stderr):
                    detail = "pulled fast-forward; now synced"
                    if has_dirty_worktree:
                        detail = "pulled with autostash; now synced"
                    self.set_repo(
                        repo_idx,
                        state="done",
                        state_msg=detail,
                        details=[],
                    )
                    self.set_slot(slot_idx, state="done", detail=detail)
                    self.mark_finished(repo_idx, success=True)
                else:
                    detail = (
                        explain_autostash_failed(pull.stderr)
                        if has_dirty_worktree
                        else explain_fast_forward_failed(pull.stderr)
                    )
                    if pull.returncode == 0 and autostash_had_conflicts(pull.stderr):
                        detail = f"{detail}: autostash reapply conflicted"
                    elif (
                        pull.stderr
                        and "terminal prompts disabled" not in pull.stderr
                        and "timed out" not in pull.stderr
                    ):
                        detail = f"{detail}: {short_text(pull.stderr).split(':', 1)[0]}"
                    self.set_repo(repo_idx, state="skip", state_msg=detail, details=[])
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)

            except Exception as exc:  # noqa: BLE001
                detail = f"error: {short_text(str(exc))}"
                self.set_repo(repo_idx, state="skip", state_msg=detail, details=[])
                self.set_slot(slot_idx, state="skip", detail=detail)
                self.mark_finished(repo_idx, success=False)
            finally:
                self.todo.task_done()

        self.set_slot(slot_idx, repo_index=None, state="idle", detail="-", branch="")


def repo_detail_text(repo: RepoState) -> str:
    if repo.details:
        details_str = "; ".join(repo.details)
        return f"{repo.state_msg}; {details_str}"
    return repo.state_msg


def should_include_in_final_report(repo: RepoState) -> bool:
    return repo.state != "done" or bool(repo.details)


def app_is_complete(finished: int, total: int, active_workers: int) -> bool:
    return total > 0 and finished >= total and active_workers == 0


def print_final_report(app: App) -> None:
    with app.lock:
        repos = sorted(
            app.repos,
            key=lambda repo: repo.name,
            reverse=app.configs.sort_reverse,
        )
        repos = [repo for repo in repos if should_include_in_final_report(repo)]

    colored = use_ansi_output()
    if not repos:
        message = "All repositories already synced with upstream"
        print(ansi(message, ANSI_GREEN, ANSI_BOLD) if colored else message)
        return

    header = f"gitferret | root: {app.root}"
    print(ansi(header, ANSI_BOLD) if colored else header)
    print(ansi("-" * 72, ANSI_DIM) if colored else "-" * 72)
    for repo in repos:
        branch = repo.branch or "-"
        detail = repo_detail_text(repo)
        line = f"{repo.name} | {branch} | {repo.state} | {detail}"
        if colored:
            line = (
                f"{ansi(repo.name, ANSI_BOLD)} | "
                f"{ansi(branch, ANSI_MAGENTA)} | "
                f"{ansi_for_state(repo.state, repo.state)} | "
                f"{ansi(detail, ANSI_DIM)}"
            )
        print(line)


def plain_run(app: App) -> None:
    workers = [
        threading.Thread(target=app.worker, args=(i,), daemon=True)
        for i in range(app.worker_count)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    with app.lock:
        for repo in app.repos:
            print(f"{repo.name}: {repo.state} - {repo_detail_text(repo)}")


SortKey = Literal["no", "repo", "branch", "state"]


class GitFerretApp(TextualApp[None]):
    """Interactive TUI for gitferret repository synchronizer."""

    TITLE = "GITFERRET"

    CSS = """
    Screen {
        overflow: hidden;
    }

    #header-info {
        width: 100%;
        padding: 0 1;
        background: $surface;
    }

    #worker-panel {
        width: 100%;
        padding: 0 1;
        background: $surface-darken-1;
    }

    #repo-table {
        height: 1fr;
        width: 100%;
    }

    #summary {
        width: 100%;
        padding: 0 1;
        background: $surface;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit_app", "Quit"),
        Binding("w", "toggle_workers", "Workers"),
        Binding("a", "toggle_autoquit", "Autoquit"),
        Binding("s", "sort_next", "Sort"),
        Binding("less_than_sign,comma", "sort_prev", "Prev sort", show=False),
        Binding("greater_than_sign,full_stop", "sort_next", "Next sort", show=False),
        Binding("r", "toggle_sort_direction", "Reverse"),
        Binding("ctrl+r", "toggle_sort_direction", "Reverse", show=False),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("g,home", "cursor_top", "Top", show=False),
        Binding("G,end", "cursor_bottom", "Bottom", show=False),
        Binding("pageup", "page_up", "Page Up", show=False),
        Binding("pagedown", "page_down", "Page Down", show=False),
        Binding("ctrl+c", "quit_app", "Quit", show=False),
    ]

    SORT_KEYS: tuple[SortKey, ...] = ("no", "repo", "branch", "state")
    SORT_LABELS: ClassVar[dict[str, str]] = {
        "no": "NO",
        "repo": "REPO",
        "branch": "BRANCH",
        "state": "STATE",
    }
    TABLE_COLUMNS: ClassVar[dict[str, tuple[str, int]]] = {
        "no": ("NO", 6),
        "repo": ("REPO", 28),
        "branch": ("BRANCH", 16),
        "state": ("STATE", 10),
        "details": ("DETAILS", 36),
    }

    def __init__(self, engine: App) -> None:
        super().__init__()
        self.engine = engine
        self.worker_threads: list[threading.Thread] = []
        initial_key = (
            "repo"
            if self.engine.configs.sort_mode == "path"
            else self.engine.configs.sort_mode
        )
        self.current_sort_key: SortKey = (
            initial_key if initial_key in self.SORT_KEYS else "repo"
        )
        self.sort_reverse = self.engine.configs.sort_reverse
        self._last_rendered_states: dict[int, tuple[str, str, str]] = {}
        self._last_engine_version = -1
        self._last_header_state: tuple[object, ...] | None = None
        self._last_worker_state: tuple[object, ...] | None = None
        self._last_summary_state: tuple[object, ...] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="header-info")
        yield Static("", id="worker-panel")
        yield DataTable(
            id="repo-table", show_row_labels=False, show_cursor=True, cursor_type="row"
        )
        yield Static("", id="summary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#repo-table", DataTable)
        self._rebuild_table(table)
        table.focus()
        self._update_header_info()
        self._update_worker_panel()
        self._update_summary()

        self.worker_threads = [
            threading.Thread(target=self.engine.worker, args=(i,), daemon=True)
            for i in range(self.engine.worker_count)
        ]
        for t in self.worker_threads:
            t.start()

        self.set_interval(0.10, self._on_ui_tick)

    def _header_text(self, key: str, label: str) -> Text:
        if key == self.current_sort_key:
            arrow = " ▼" if self.sort_reverse else " ▲"
            return Text(f"{label}{arrow}", style="bold black on yellow")
        return Text(label, style="bold bright_cyan")

    def _style_for_state(self, state: str) -> str:
        if state == "done":
            return "green"
        if state == "running":
            return "bold cyan"
        if state == "skip":
            return "yellow"
        if state in {"queued", "idle"}:
            return "dim"
        return "white"

    def _sort_rows(self) -> list[RepoState]:
        with self.engine.lock:
            repos = list(self.engine.repos)

        match self.current_sort_key:
            case "no":
                key_fn = lambda r: r.index
            case "repo":
                key_fn = lambda r: r.name.lower()
            case "branch":
                key_fn = lambda r: (r.branch or "").lower()
            case "state":
                key_fn = lambda r: (r.state, r.name)
            case _:
                key_fn = lambda r: r.index

        return sorted(repos, key=key_fn, reverse=self.sort_reverse)

    def _rebuild_table(self, table: DataTable | None = None) -> None:
        if table is None:
            table = self.query_one("#repo-table", DataTable)
        table.clear(columns=True)
        for key, (label, width) in self.TABLE_COLUMNS.items():
            table.add_column(self._header_text(key, label), key=key, width=width)

        sorted_repos = self._sort_rows()
        self._last_rendered_states.clear()
        for repo in sorted_repos:
            row_style = self._style_for_state(repo.state)
            detail = repo_detail_text(repo)
            table.add_row(
                Text(f"{repo.index + 1:>5}", style=row_style),
                Text(repo.name, style=row_style),
                Text(repo.branch or "-", style=row_style),
                Text(repo.state, style=row_style),
                Text(detail, style=row_style),
                key=str(repo.index),
            )
            self._last_rendered_states[repo.index] = (repo.branch, repo.state, detail)

    def _update_header_info(self) -> None:
        with self.engine.lock:
            active_workers = sum(
                1
                for s in self.engine.slots
                if s.repo_index is not None and s.state != "idle"
            )
            total_workers = len(self.engine.slots)
            total_repos = self.engine.total
        root_display = str(self.engine.root.resolve())
        state = (root_display, total_repos, active_workers, total_workers)
        if self._last_header_state == state:
            return
        self._last_header_state = state

        self.query_one("#header-info", Static).update(
            Text.assemble(
                "Root: ",
                (root_display, "bold white"),
                " | Total Repos: ",
                (str(total_repos), "bold cyan"),
                " | Workers: ",
                (f"{active_workers}/{total_workers}", "bold yellow"),
            )
        )

    def _update_worker_panel(self) -> None:
        panel = self.query_one("#worker-panel", Static)
        with self.engine.lock:
            show = self.engine.configs.show_workers
            active = sum(
                1
                for s in self.engine.slots
                if s.repo_index is not None and s.state != "idle"
            )
            total = len(self.engine.slots)
            if not show:
                state = (show, active, total)
                if self._last_worker_state == state:
                    return
                self._last_worker_state = state
                panel.update(
                    Text.assemble(
                        "Workers: ",
                        (f"{active}/{total}", "bold yellow"),
                        " active (Press ",
                        ("w", "bold white"),
                        " to expand worker details)",
                    )
                )
                return

            slots_snapshot = [
                (
                    s.index,
                    s.repo_index,
                    self.engine.repos[s.repo_index].name
                    if s.repo_index is not None
                    else None,
                    s.branch,
                    s.detail,
                )
                for s in self.engine.slots
            ]

        state = (show, active, total, tuple(slots_snapshot))
        if self._last_worker_state == state:
            return
        self._last_worker_state = state

        text_items: list[Text] = [
            Text.assemble(
                "Workers: ",
                (f"{active}/{total}", "bold yellow"),
                " active (Press ",
                ("w", "bold white"),
                " to collapse)",
            )
        ]
        for slot_idx, repo_idx, repo_name, branch, detail in slots_snapshot:
            slot_num = f"[{slot_idx + 1:02d}]"
            if repo_idx is None or repo_name is None:
                row_text = Text.assemble(
                    ("  ", ""),
                    (slot_num, "dim"),
                    (" idle", "dim"),
                )
            else:
                branch_text = f" ({branch or '-'}): "
                row_text = Text.assemble(
                    ("  ", ""),
                    (slot_num, "bold cyan"),
                    (" ", ""),
                    (repo_name, "bold white"),
                    (branch_text, "dim"),
                    (detail, "cyan"),
                )
            text_items.append(row_text)

        result = Text("\n").join(text_items)
        panel.update(result)

    def _update_summary(self) -> None:
        with self.engine.lock:
            queued = sum(1 for r in self.engine.repos if r.state == "queued")
            running = sum(1 for r in self.engine.repos if r.state == "running")
            done = sum(1 for r in self.engine.repos if r.state == "done")
            skipped = sum(1 for r in self.engine.repos if r.state == "skip")
            autoquit = self.engine.configs.autoquit

        state = (
            self.current_sort_key,
            self.sort_reverse,
            queued,
            running,
            done,
            skipped,
            autoquit,
        )
        if self._last_summary_state == state:
            return
        self._last_summary_state = state

        sort_label = self.SORT_LABELS[self.current_sort_key]
        direction = "DESC" if self.sort_reverse else "ASC"
        autoquit_tag = ("ON", "bold green") if autoquit else ("OFF", "dim")

        self.query_one("#summary", Static).update(
            Text.assemble(
                "Sort: ",
                (sort_label, "bold yellow"),
                f" ({direction}) | Queued: ",
                (str(queued), "dim"),
                "  Running: ",
                (str(running), "cyan"),
                "  Done: ",
                (str(done), "green"),
                "  Skipped: ",
                (str(skipped), "yellow"),
                " | Autoquit: ",
                autoquit_tag,
            )
        )

    def _on_ui_tick(self) -> None:
        with self.engine.lock:
            version = self.engine.version
            finished = self.engine.finished
            total = self.engine.total
            active = sum(
                1
                for s in self.engine.slots
                if s.repo_index is not None and s.state != "idle"
            )
            autoquit = self.engine.configs.autoquit

            if autoquit and app_is_complete(finished, total, active):
                self.action_quit_app()
                return

            if version == self._last_engine_version:
                return
            self._last_engine_version = version

            changed_rows: list[tuple[str, str, str, str]] = []
            for repo in self.engine.repos:
                detail = repo_detail_text(repo)
                current_tuple = (repo.branch, repo.state, detail)
                if self._last_rendered_states.get(repo.index) != current_tuple:
                    changed_rows.append(
                        (str(repo.index), repo.branch or "-", repo.state, detail)
                    )
                    self._last_rendered_states[repo.index] = current_tuple

        table = self.query_one("#repo-table", DataTable)
        for row_key, branch, state, detail in changed_rows:
            style = self._style_for_state(state)
            try:
                table.update_cell(row_key, "branch", Text(branch, style=style))
                table.update_cell(row_key, "state", Text(state, style=style))
                table.update_cell(row_key, "details", Text(detail, style=style))
            except (KeyError, ValueError):
                pass

        self._update_header_info()
        self._update_worker_panel()
        self._update_summary()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        col_key = str(event.column_key.value)
        if col_key in self.SORT_KEYS:
            if self.current_sort_key == col_key:
                self.sort_reverse = not self.sort_reverse
            else:
                self.current_sort_key = col_key  # type: ignore[assignment]
                self.sort_reverse = False
            self.engine.configs.sort_mode = (
                "path" if self.current_sort_key == "repo" else self.current_sort_key
            )
            self.engine.configs.sort_reverse = self.sort_reverse
            self._rebuild_table()
            self._last_summary_state = None
            self._update_summary()

    def action_cursor_down(self) -> None:
        self.query_one("#repo-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#repo-table", DataTable).action_cursor_up()

    def action_cursor_top(self) -> None:
        self.query_one("#repo-table", DataTable).move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        table = self.query_one("#repo-table", DataTable)
        table.move_cursor(row=max(0, table.row_count - 1))

    def action_page_up(self) -> None:
        self.query_one("#repo-table", DataTable).action_page_up()

    def action_page_down(self) -> None:
        self.query_one("#repo-table", DataTable).action_page_down()

    def action_sort_next(self) -> None:
        idx = self.SORT_KEYS.index(self.current_sort_key)
        self.current_sort_key = self.SORT_KEYS[(idx + 1) % len(self.SORT_KEYS)]
        self.engine.configs.sort_mode = (
            "path" if self.current_sort_key == "repo" else self.current_sort_key
        )
        self._rebuild_table()
        self._last_summary_state = None
        self._update_summary()

    def action_sort_prev(self) -> None:
        idx = self.SORT_KEYS.index(self.current_sort_key)
        self.current_sort_key = self.SORT_KEYS[(idx - 1) % len(self.SORT_KEYS)]
        self.engine.configs.sort_mode = (
            "path" if self.current_sort_key == "repo" else self.current_sort_key
        )
        self._rebuild_table()
        self._last_summary_state = None
        self._update_summary()

    def action_toggle_sort_direction(self) -> None:
        self.sort_reverse = not self.sort_reverse
        self.engine.configs.sort_reverse = self.sort_reverse
        self._rebuild_table()
        self._last_summary_state = None
        self._update_summary()

    def action_toggle_workers(self) -> None:
        with self.engine.lock:
            self.engine.configs.show_workers = not self.engine.configs.show_workers
        self._last_worker_state = None
        self._update_worker_panel()

    def action_toggle_autoquit(self) -> None:
        with self.engine.lock:
            self.engine.configs.autoquit = not self.engine.configs.autoquit
        self._last_summary_state = None
        self._update_summary()

    def action_quit_app(self) -> None:
        self.engine.stop.set()
        for t in self.worker_threads:
            t.join(timeout=0.1)
        self.engine.finalize_stopped_repos()
        self.exit()


def main(argv: list[str] | None = None, default_root: str | Path = DEFAULT_ROOT) -> int:
    if argv is None:
        argv = sys.argv
    parser = argparse.ArgumentParser(
        prog="gitferret",
        usage="gitferret [--root [ROOT]] [-w WORKERS] [root]",
        formatter_class=AlignedHelpFormatter,
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=argparse.SUPPRESS,
        help="root folder to scan for git repositories",
    )
    parser.add_argument(
        "--root",
        dest="root_flag",
        nargs="?",
        const=str(Path(default_root).expanduser()),
        default=argparse.SUPPRESS,
        metavar="ROOT",
        help="root folder to scan for git repositories",
    )
    parser.add_argument(
        "-w", "--workers", type=int, default=MAX_JOBS, help="worker count override"
    )
    args = parser.parse_args(argv[1:])

    default_root_text = str(Path(default_root).expanduser())
    root_arg = getattr(args, "root_flag", getattr(args, "root", default_root_text))
    root = Path(root_arg).expanduser().resolve()
    if not root.is_dir():
        print(f"root not found: {root}", file=sys.stderr)
        return 1

    repos = discover_repos(root)
    if not repos:
        print(f"no git repositories found under: {root}")
        return 0

    worker_count = args.workers
    if worker_count < 1:
        print("worker count must be at least 1", file=sys.stderr)
        return 1

    configs = Configs.load(CONFIG_PATH)
    engine = App(root, repos, configs, worker_count)
    try:
        if sys.stdout.isatty() and sys.stderr.isatty():
            tui_app = GitFerretApp(engine)
            tui_app.run()
            print_final_report(engine)
        else:
            plain_run(engine)
    finally:
        engine.configs.save(CONFIG_PATH)
        engine.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
