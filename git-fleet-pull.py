#!/usr/bin/env python3
from __future__ import annotations

import curses
import json
import os
import queue
import subprocess
import sys
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


MAX_JOBS = max(1, os.cpu_count() or 1)
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_WHITE = "\033[37m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_CYAN = "\033[36m"
ANSI_MAGENTA = "\033[35m"
CONFIG_PATH = Path.home() / ".git-fleet-pull"


def short_text(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def explain_dirty(repo: Path) -> str:
    return f"local changes exist; trying autostash for {repo.name}"


def explain_ahead(ahead: int, behind: int) -> str:
    return f"local commits not pushed ({ahead} ahead, {behind} behind); push or rebase first"


def explain_no_upstream() -> str:
    return "no upstream set; use git branch --set-upstream-to or git push -u"


def explain_fetch_failed() -> str:
    return "fetch failed; check network or remote access and retry"


def explain_fast_forward_failed() -> str:
    return "ff-only pull failed; history diverged or changed during fetch"


def explain_autostash_failed() -> str:
    return "autostash pull failed; stash/review local changes and retry"


def autostash_had_conflicts(stderr: str) -> bool:
    text = stderr.lower()
    return "autostash" in text and "conflict" in text


def explain_up_to_date() -> str:
    return "already synced with upstream"


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
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
    return f"{''.join(codes)}{text}{ANSI_RESET}"


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
    detail: str = "waiting"
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

    @classmethod
    def load(cls, path: Path) -> Configs:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return cls()
        if not isinstance(payload, dict):
            return cls()

        sort_mode = payload.get("sort_mode", "path")
        if sort_mode not in {"path", "state", "branch"}:
            sort_mode = "path"

        return cls(
            sort_mode=sort_mode,
            sort_reverse=bool(payload.get("sort_reverse", False)),
            show_workers=bool(payload.get("show_workers", False)),
        )

    def save(self, path: Path) -> None:
        data = {
            "sort_mode": self.sort_mode,
            "sort_reverse": self.sort_reverse,
            "show_workers": self.show_workers,
        }
        try:
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            return


class App:
    def __init__(self, root: Path, repos: list[Path], configs: Configs):
        self.root = root
        self.repos = [
            RepoState(index=i, path=repo, name=repo_display_name(root, repo))
            for i, repo in enumerate(repos)
        ]
        self.configs = configs
        self.slots = [SlotState(index=i) for i in range(MAX_JOBS)]
        self.todo: queue.Queue[int] = queue.Queue()
        for i in range(len(self.repos)):
            self.todo.put(i)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.finished = 0
        self.total = len(self.repos)
        self.tempdir = Path(tempfile.mkdtemp(prefix="pull-all-repos-"))
        self.has_colors = False
        self.repo_scroll = 0

    def cleanup(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def set_repo(self, idx: int, **changes: object) -> None:
        with self.lock:
            repo = self.repos[idx]
            for key, value in changes.items():
                setattr(repo, key, value)

    def set_slot(self, idx: int, **changes: object) -> None:
        with self.lock:
            slot = self.slots[idx]
            for key, value in changes.items():
                setattr(slot, key, value)
            slot.updated_at = time.time()

    def cycle_sort_mode(self) -> None:
        with self.lock:
            modes = ("path", "state", "branch")
            current = modes.index(self.configs.sort_mode)
            self.configs.sort_mode = modes[(current + 1) % len(modes)]

    def toggle_sort_reverse(self) -> None:
        with self.lock:
            self.configs.sort_reverse = not self.configs.sort_reverse

    def toggle_workers(self) -> None:
        with self.lock:
            self.configs.show_workers = not self.configs.show_workers

    def scroll_repos(self, delta: int, visible_rows: int) -> None:
        with self.lock:
            if visible_rows <= 0:
                self.repo_scroll = 0
                return
            max_scroll = max(0, len(self.repos) - visible_rows)
            self.repo_scroll = max(0, min(self.repo_scroll + delta, max_scroll))

    def jump_repos(self, position: int, visible_rows: int) -> None:
        with self.lock:
            if visible_rows <= 0:
                self.repo_scroll = 0
                return
            max_scroll = max(0, len(self.repos) - visible_rows)
            self.repo_scroll = max(0, min(position, max_scroll))

    def mark_finished(self, idx: int, success: bool) -> None:
        with self.lock:
            self.finished += 1
            self.repos[idx].finished_at = time.time()
            if not success and self.repos[idx].state == "running":
                self.repos[idx].state = "skip"

    def worker(self, slot_idx: int) -> None:
        while not self.stop.is_set():
            try:
                repo_idx = self.todo.get_nowait()
            except queue.Empty:
                break

            try:
                repo = self.repos[repo_idx]
                self.set_slot(slot_idx, repo_index=repo_idx, state="running", detail="starting", branch=repo.name)
                self.set_repo(repo_idx, state="running", detail="scanning", slot=slot_idx, started_at=time.time())

                probe = run_git(repo.path, "rev-parse", "--is-inside-work-tree")
                if probe.returncode != 0 or short_text(probe.stdout) != "true":
                    detail = "not a git work tree; skip"
                    self.set_repo(repo_idx, state="skip", detail=detail)
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
                    self.set_repo(repo_idx, detail=detail)
                    self.set_slot(slot_idx, detail=detail)

                upstream = run_git(repo.path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
                upstream_ref = short_text(upstream.stdout)
                if upstream.returncode != 0 or not upstream_ref:
                    detail = explain_no_upstream()
                    self.set_repo(repo_idx, state="skip", detail=detail)
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                self.set_repo(repo_idx, detail="fetching upstream")
                self.set_slot(slot_idx, detail="fetching upstream")
                fetch = run_git(repo.path, "fetch", "--prune", "--quiet")
                if fetch.returncode != 0:
                    detail = explain_fetch_failed()
                    self.set_repo(repo_idx, state="skip", detail=detail)
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                compare = run_git(repo.path, "rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}")
                if compare.returncode != 0:
                    detail = "compare failed; retry later"
                    if compare.stderr:
                        detail = f"{detail}: {short_text(compare.stderr).split(':', 1)[0]}"
                    self.set_repo(repo_idx, state="skip", detail=detail)
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                parts = short_text(compare.stdout).split()
                ahead = behind = 0
                if len(parts) >= 2:
                    ahead, behind = int(parts[0]), int(parts[1])

                if ahead != 0:
                    detail = explain_ahead(ahead, behind)
                    self.set_repo(repo_idx, state="skip", detail=detail)
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
                    continue

                if behind == 0:
                    detail = explain_up_to_date()
                    if has_dirty_worktree:
                        detail = f"{detail}; local changes preserved"
                    self.set_repo(repo_idx, state="done", detail=detail)
                    self.set_slot(slot_idx, state="done", detail=detail)
                    self.mark_finished(repo_idx, success=True)
                    continue

                if has_dirty_worktree:
                    pulling_detail = f"pulling with autostash ({behind} behind)"
                else:
                    pulling_detail = f"pulling ff-only ({behind} behind)"
                self.set_repo(repo_idx, detail=pulling_detail)
                self.set_slot(slot_idx, detail=pulling_detail)
                pull_args = ["pull", "--ff-only"]
                if has_dirty_worktree:
                    pull_args.append("--autostash")
                pull = run_git(repo.path, *pull_args)
                if pull.returncode == 0 and not autostash_had_conflicts(pull.stderr):
                    detail = "pulled fast-forward; now synced"
                    if has_dirty_worktree:
                        detail = "pulled with autostash; now synced"
                    self.set_repo(repo_idx, state="done", detail=detail)
                    self.set_slot(slot_idx, state="done", detail=detail)
                    self.mark_finished(repo_idx, success=True)
                else:
                    detail = explain_autostash_failed() if has_dirty_worktree else explain_fast_forward_failed()
                    if pull.returncode == 0 and autostash_had_conflicts(pull.stderr):
                        detail = f"{detail}: autostash reapply conflicted"
                    if pull.stderr:
                        detail = f"{detail}: {short_text(pull.stderr).split(':', 1)[0]}"
                    self.set_repo(repo_idx, state="skip", detail=detail)
                    self.set_slot(slot_idx, state="skip", detail=detail)
                    self.mark_finished(repo_idx, success=False)
            except Exception as exc:  # noqa: BLE001
                detail = f"error: {short_text(str(exc))}"
                self.set_repo(repo_idx, state="skip", detail=detail)
                self.set_slot(slot_idx, state="skip", detail=detail)
                self.mark_finished(repo_idx, success=False)
            finally:
                self.todo.task_done()

        self.set_slot(slot_idx, repo_index=None, state="idle", detail="-", branch="")


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def compute_column_widths(width: int, repos: list[RepoState]) -> tuple[int, int]:
    index_width = 4
    state_width = 8
    spaces = 3
    available = max(0, width - index_width - state_width - spaces)

    if available <= 0:
        return 0, 0

    max_name_len = max((len(repo.name) for repo in repos), default=12)
    min_detail_width = min(60, max(24, width // 2))

    name_width = min(max_name_len, max(12, available - min_detail_width))
    if available - name_width < min_detail_width:
        name_width = max(12, available - min_detail_width)
    name_width = min(name_width, available)
    detail_width = max(0, available - name_width)

    if detail_width < 1:
        detail_width = 1
        name_width = max(12, available - detail_width)

    return name_width, detail_width


def compute_branch_width(width: int, repos: list[RepoState]) -> int:
    max_branch_len = max((len(repo.branch) for repo in repos), default=8)
    return max(8, min(24, max_branch_len))


def compute_repo_row_widths(width: int, repos: list[RepoState]) -> tuple[int, int, int]:
    index_width = 4
    state_width = 8
    spaces = 5
    available = max(0, width - index_width - state_width - spaces)
    if available <= 0:
        return 0, 0, 0

    branch_width = compute_branch_width(width, repos)
    branch_width = min(branch_width, max(8, available // 4))
    remaining = max(0, available - branch_width)
    name_width, detail_width = compute_column_widths(remaining + 3, repos)
    if name_width + detail_width > remaining:
        detail_width = max(0, remaining - name_width)
    return name_width, branch_width, detail_width


def style_for_state(state: str) -> int:
    if state in {"done"}:
        return curses.color_pair(2)
    if state in {"skip"}:
        return curses.color_pair(3)
    if state in {"running"}:
        return curses.color_pair(4)
    if state in {"queued", "idle"}:
        return curses.color_pair(5)
    return curses.color_pair(1)


def sort_label(mode: str) -> str:
    if mode == "state":
        return "state"
    if mode == "branch":
        return "branch"
    return "path"


def repo_range_label(repo_scroll: int, visible_rows: int, total_repos: int) -> str:
    digits = max(2, len(str(total_repos)))
    if total_repos <= 0:
        return f"{0:0{digits}d}-{0:0{digits}d}/{0:0{digits}d}"
    start = min(total_repos, repo_scroll + 1)
    end = min(total_repos, repo_scroll + visible_rows) if visible_rows > 0 else start
    if visible_rows <= 0:
        end = start
    return f"{start:0{digits}d}-{end:0{digits}d}/{total_repos:0{digits}d}"


def worker_rows(show_workers: bool, slot_count: int) -> int:
    return slot_count if show_workers else 0


def repo_section_top(slot_count: int, show_workers: bool) -> int:
    repo_start = 1  # title
    repo_start += 1  # separator
    repo_start += 1  # workers heading
    repo_start += worker_rows(show_workers, slot_count)
    repo_start += 1  # separator
    repo_start += 1  # repos heading
    return repo_start


def repo_list_visible_rows(height: int, slot_count: int, show_workers: bool) -> int:
    return max(0, height - repo_section_top(slot_count, show_workers) - 2)  # bottom separator + summary


def page_scroll_step(visible_rows: int) -> int:
    if visible_rows <= 0:
        return 1
    return max(1, visible_rows - 1)

def repo_sort_key(repo: RepoState, mode: str) -> tuple[str, str]:
    if mode == "state":
        return (repo.state, repo.name)
    if mode == "branch":
        return (repo.branch or "", repo.name)
    return (repo.name, repo.state)


def line(stdscr: curses.window, y: int, text: str, width: int, attr: int = 0) -> None:
    if y < 0:
        return
    try:
        stdscr.move(y, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(y, 0, truncate(text, width), width, attr)
    except curses.error:
        return


def dim_suffix(stdscr: curses.window, y: int, prefix: str, suffix: str, width: int) -> None:
    if width <= len(prefix) or not suffix:
        return
    try:
        stdscr.addnstr(y, len(prefix), truncate(suffix, width - len(prefix)), width - len(prefix), curses.A_DIM)
    except curses.error:
        return


def draw_scrollbar(
    stdscr: curses.window,
    width: int,
    height: int,
    scroll: int,
    visible_rows: int,
    total_rows: int,
    top: int,
) -> None:
    if width <= 0 or visible_rows <= 0 or total_rows <= visible_rows:
        return

    max_scroll = max(0, total_rows - visible_rows)
    if max_scroll <= 0:
        return

    track_top = max(0, top)
    track_bottom = min(height - 2, track_top + visible_rows - 1)
    track_height = max(0, track_bottom - track_top + 1)
    if track_height <= 0:
        return

    bar_height = max(1, round(visible_rows * visible_rows / total_rows))
    bar_height = min(bar_height, track_height)
    bar_range = max(0, track_height - bar_height)
    thumb_top = track_top + (round(scroll * bar_range / max_scroll) if bar_range > 0 else 0)
    thumb_bottom = min(track_bottom, thumb_top + bar_height - 1)

    for y in range(track_top, track_bottom + 1):
        try:
            stdscr.addch(y, width - 1, "#" if thumb_top <= y <= thumb_bottom else "|")
        except curses.error:
            return


def draw(stdscr: curses.window, app: App) -> None:
    try:
        height, width = stdscr.getmaxyx()
        visible_rows = repo_list_visible_rows(height, len(app.slots), app.configs.show_workers)
        app.scroll_repos(0, visible_rows)
        stdscr.erase()
        content_width = max(0, width - 1)
        for y, text, attr in build_view_lines(app, content_width, height, include_quit_hint=True):
            line(stdscr, y, text, content_width, attr)
            if text.startswith("Workers "):
                dim_suffix(stdscr, y, "Workers", text[len("Workers") :], content_width)
            elif text.startswith("Repos total: "):
                dim_suffix(stdscr, y, "Repos", text[len("Repos") :], content_width)
        draw_scrollbar(
            stdscr,
            width,
            height,
            app.repo_scroll,
            visible_rows,
            len(app.repos),
            repo_section_top(len(app.slots), app.configs.show_workers),
        )
        stdscr.noutrefresh()
        curses.doupdate()
    except curses.error:
        return


def build_view_lines(app: App, width: int, height: int, *, include_quit_hint: bool) -> list[tuple[int, str, int]]:
    with app.lock:
        repo_indexed = list(app.repos)
        slots = list(app.slots)
        sort_mode = app.configs.sort_mode
        sort_reverse = app.configs.sort_reverse
        repo_scroll = app.repo_scroll
        show_workers = app.configs.show_workers
        repos = sorted(repo_indexed, key=lambda repo: repo_sort_key(repo, sort_mode), reverse=sort_reverse)
        queued = sum(1 for repo in repo_indexed if repo.state == "queued")
        running = sum(1 for repo in repo_indexed if repo.state == "running")
        done = sum(1 for repo in repo_indexed if repo.state == "done")
        skipped = sum(1 for repo in repo_indexed if repo.state == "skip")
        active_workers = sum(1 for slot in slots if slot.repo_index is not None and slot.state != "idle")

    name_width, branch_width, detail_width = compute_repo_row_widths(width, repos)
    visible_rows = repo_list_visible_rows(height, len(slots), show_workers)
    max_scroll = max(0, len(repos) - visible_rows)
    repo_scroll = min(max(repo_scroll, 0), max_scroll)

    lines: list[tuple[int, str, int]] = []
    y = 0
    direction = "desc" if sort_reverse else "asc"
    header = f"Git fleet pull | root: {app.root}"
    lines.append((y, header, curses.A_BOLD))
    y += 1
    lines.append((y, "-" * max(0, width), 0))
    y += 1
    lines.append((y, f"Workers {active_workers} / {len(slots)} | w:show", curses.A_BOLD))
    y += 1

    if show_workers:
        for slot in slots:
            if slot.repo_index is None:
                text = f"[{slot.index + 1:02d}] {'-':<{name_width}} {'-':<{branch_width}} {'idle':<8} {'-':<{detail_width}}"
                attr = style_for_state("idle")
            else:
                repo = repo_indexed[slot.repo_index]
                name = truncate(repo.name, name_width)
                branch = truncate(repo.branch, branch_width)
                detail = truncate(repo.detail, detail_width)
                text = f"[{slot.index + 1:02d}] {name:<{name_width}} {branch:<{branch_width}} {slot.state:<8} {detail:<{detail_width}}"
                attr = style_for_state(slot.state)
            lines.append((y, text, attr))
            y += 1

    lines.append((y, "-" * max(0, width), 0))
    y += 1
    lines.append(
        (
            y,
            f"Repos total: {repo_range_label(repo_scroll, visible_rows, len(repos))} | s:sort r:reverse up/down pgup/dn home/end | sort: {sort_label(sort_mode)} {direction}",
            curses.A_BOLD,
        )
    )
    y += 1

    visible_repos = repos[repo_scroll : repo_scroll + visible_rows] if visible_rows > 0 else []
    for repo in visible_repos:
        name = truncate(repo.name, name_width)
        branch = truncate(repo.branch, branch_width)
        detail = truncate(repo.detail, detail_width)
        text = f"[{repo.index + 1:02d}] {name:<{name_width}} {branch:<{branch_width}} {repo.state:<8} {detail:<{detail_width}}"
        lines.append((y, text, style_for_state(repo.state)))
        y += 1

    if height >= 2:
        lines.append((height - 2, "-" * max(0, width), 0))

    summary = f"q:quit  summary: queued={queued} running={running} done={done} skip={skipped}"
    lines.append((height - 1, summary, curses.A_DIM))
    return lines


def print_snapshot(app: App) -> None:
    width = shutil.get_terminal_size(fallback=(120, 24)).columns
    height = shutil.get_terminal_size(fallback=(120, 24)).lines
    for _, text, _ in build_view_lines(app, width, height, include_quit_hint=False):
        print(text)


def print_final_report(app: App) -> None:
    with app.lock:
        repos = sorted(
            app.repos,
            key=lambda repo: repo_sort_key(repo, app.configs.sort_mode),
            reverse=app.configs.sort_reverse,
        )
        repos = [repo for repo in repos if repo.state != "done"]

    colored = use_ansi_output()
    if not repos:
        message = "All repositories already synced with upstream"
        print(ansi(message, ANSI_GREEN, ANSI_BOLD) if colored else message)
        return

    header = f"Git fleet pull | root: {app.root}"
    print(ansi(header, ANSI_BOLD) if colored else header)
    print(ansi("-" * 72, ANSI_DIM) if colored else "-" * 72)
    for repo in repos:
        branch = repo.branch or "-"
        line = f"{repo.name} | {branch} | {repo.state} | {repo.detail}"
        if colored:
            line = (
                f"{ansi(repo.name, ANSI_BOLD)} | "
                f"{ansi(branch, ANSI_MAGENTA)} | "
                f"{ansi_for_state(repo.state, repo.state)} | "
                f"{ansi(repo.detail, ANSI_DIM)}"
            )
        print(line)


def plain_run(app: App) -> None:
    workers = [threading.Thread(target=app.worker, args=(i,), daemon=True) for i in range(MAX_JOBS)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    with app.lock:
        for repo in app.repos:
            print(f"{repo.name}: {repo.state} - {repo.detail}")


def curses_run(app: App) -> None:
    ui_poll_interval = 0.02

    def _main(stdscr: curses.window) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_WHITE, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)
            app.has_colors = True
        stdscr.nodelay(True)
        stdscr.keypad(True)

        workers = [threading.Thread(target=app.worker, args=(i,), daemon=True) for i in range(MAX_JOBS)]
        for worker in workers:
            worker.start()

        try:
            while True:
                height, _width = stdscr.getmaxyx()
                visible_rows = repo_list_visible_rows(height, len(app.slots), app.configs.show_workers)
                app.scroll_repos(0, visible_rows)
                draw(stdscr, app)

                try:
                    ch = stdscr.getch()
                except curses.error:
                    time.sleep(ui_poll_interval)
                    continue
                if ch == curses.KEY_RESIZE:
                    continue
                if ch == ord("s"):
                    app.cycle_sort_mode()
                    continue
                if ch == ord("r"):
                    app.toggle_sort_reverse()
                    continue
                if ch == ord("w"):
                    app.toggle_workers()
                    visible_rows = repo_list_visible_rows(height, len(app.slots), app.configs.show_workers)
                    app.scroll_repos(0, visible_rows)
                    continue
                if ch in (curses.KEY_UP, ord("k")):
                    app.scroll_repos(-1, visible_rows)
                    continue
                if ch in (curses.KEY_DOWN, ord("j")):
                    app.scroll_repos(1, visible_rows)
                    continue
                if ch in (curses.KEY_PPAGE,):
                    app.scroll_repos(-page_scroll_step(visible_rows), visible_rows)
                    continue
                if ch in (curses.KEY_NPAGE,):
                    app.scroll_repos(page_scroll_step(visible_rows), visible_rows)
                    continue
                if ch in (curses.KEY_HOME, ord("g")):
                    app.jump_repos(0, visible_rows)
                    continue
                if ch in (curses.KEY_END, ord("G")):
                    app.jump_repos(len(app.repos), visible_rows)
                    continue
                if ch in (ord("q"), ord("Q")):
                    app.stop.set()
                    break

                time.sleep(ui_poll_interval)
        finally:
            app.stop.set()
            for worker in workers:
                worker.join(timeout=1.0)
            try:
                draw(stdscr, app)
            except curses.error:
                pass
            time.sleep(0.05)

    curses.wrapper(_main)


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").expanduser().resolve()
    if not root.is_dir():
        print(f"root not found: {root}", file=sys.stderr)
        return 1

    repos = discover_repos(root)
    if not repos:
        print(f"no git repositories found under: {root}")
        return 0

    app = App(root, repos, Configs.load(CONFIG_PATH))
    try:
        if sys.stdout.isatty() and sys.stderr.isatty():
            try:
                curses_run(app)
            except curses.error:
                plain_run(app)
            else:
                print_final_report(app)
        else:
            plain_run(app)
    finally:
        app.configs.save(CONFIG_PATH)
        app.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
