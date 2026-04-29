#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import download_x_media


APP_NAME = "X Media Downloader"


def default_app_support_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def default_output_dir() -> Path:
    return Path.home() / "Downloads" / APP_NAME


def resolve_gui_toolchain_dir() -> Path:
    if getattr(sys, "frozen", False):
        bundled_dir = Path(sys.executable).resolve().parent / "_toolchain"
        if bundled_dir.exists():
            return bundled_dir

    bundled_dir = Path(__file__).resolve().parent / "_toolchain"
    if bundled_dir.exists():
        return bundled_dir

    return default_app_support_dir() / ".venv"


def reveal_path(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


APP_SUPPORT_DIR = default_app_support_dir()
CONFIG_PATH = APP_SUPPORT_DIR / "settings.json"
DEFAULT_OUTPUT_DIR = default_output_dir()


class QueueWriter:
    """Buffer redirected output and forward it to the UI queue line by line."""

    def __init__(self, callback):
        self.callback = callback
        self.buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0

        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.callback(line)
        return len(text)

    def flush(self) -> None:
        if self.buffer:
            self.callback(self.buffer)
            self.buffer = ""


class DownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("980x760")
        self.root.minsize(820, 620)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.running = False
        self.latest_target_dir: Path | None = None
        self.latest_summary_path: Path | None = None

        self.profile_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.cookie_mode_var = tk.StringVar(value="auto")
        self.browser_var = tk.StringVar(value="edge")
        self.cookies_file_var = tk.StringVar()
        self.concurrency_var = tk.StringVar(value="3")
        self.retries_var = tk.StringVar(value="3")
        self.timeout_var = tk.StringVar(value="60")
        self.include_retweets_var = tk.BooleanVar(value=False)
        self.include_quoted_var = tk.BooleanVar(value=False)
        self.exclude_pinned_var = tk.BooleanVar(value=False)
        self.write_info_json_var = tk.BooleanVar(value=False)
        self.verify_auth_var = tk.BooleanVar(value=False)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.skip_bootstrap_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")

        self.build_ui()
        self.load_settings()
        self.update_cookie_controls()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_events)

    def build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass

        container = ttk.Frame(self.root, padding=16)
        container.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(4, weight=1)

        intro = ttk.Label(
            container,
            text="Download original-quality media from a Twitter/X profile.",
            wraplength=900,
            justify="left",
        )
        intro.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        form = ttk.LabelFrame(container, text="Download Settings", padding=12)
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Username or profile URL").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(form, textvariable=self.profile_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(form, text="Save downloads to").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        ttk.Entry(form, textvariable=self.output_dir_var).grid(row=1, column=1, sticky="ew", pady=(10, 0))
        ttk.Button(form, text="Browse", command=self.browse_output_dir).grid(
            row=1, column=2, sticky="ew", padx=(10, 0), pady=(10, 0)
        )

        auth = ttk.LabelFrame(container, text="Authentication", padding=12)
        auth.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        auth.columnconfigure(0, weight=1)

        mode_row = ttk.Frame(auth)
        mode_row.grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            mode_row,
            text="Auto detect browser cookies",
            value="auto",
            variable=self.cookie_mode_var,
            command=self.update_cookie_controls,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            mode_row,
            text="Use browser cookies",
            value="browser",
            variable=self.cookie_mode_var,
            command=self.update_cookie_controls,
        ).grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Radiobutton(
            mode_row,
            text="Use cookies file",
            value="file",
            variable=self.cookie_mode_var,
            command=self.update_cookie_controls,
        ).grid(row=0, column=2, sticky="w", padx=(16, 0))

        self.auth_details = ttk.Frame(auth)
        self.auth_details.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.auth_details.columnconfigure(0, weight=1)

        self.browser_row = ttk.Frame(self.auth_details)
        self.browser_row.columnconfigure(1, weight=1)
        self.browser_label = ttk.Label(self.browser_row, text="Browser")
        self.browser_label.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.browser_combo = ttk.Combobox(
            self.browser_row,
            textvariable=self.browser_var,
            values=list(download_x_media.AUTO_COOKIE_BROWSERS),
            state="readonly",
        )
        self.browser_combo.grid(row=0, column=1, sticky="ew")

        self.cookies_row = ttk.Frame(self.auth_details)
        self.cookies_row.columnconfigure(1, weight=1)
        self.cookies_label = ttk.Label(self.cookies_row, text="Cookies file")
        self.cookies_label.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.cookies_entry = ttk.Entry(self.cookies_row, textvariable=self.cookies_file_var)
        self.cookies_entry.grid(row=0, column=1, sticky="ew")
        self.cookies_button = ttk.Button(self.cookies_row, text="Browse", command=self.browse_cookies_file)
        self.cookies_button.grid(row=0, column=2, sticky="ew", padx=(10, 0))

        options = ttk.LabelFrame(container, text="Options", padding=12)
        options.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        for column in range(3):
            options.columnconfigure(column, weight=1)

        ttk.Checkbutton(
            options, text="Include retweets", variable=self.include_retweets_var
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            options, text="Include quoted tweets", variable=self.include_quoted_var
        ).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(
            options, text="Exclude pinned tweet", variable=self.exclude_pinned_var
        ).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(
            options, text="Write info JSON files", variable=self.write_info_json_var
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            options, text="Verify auth before download", variable=self.verify_auth_var
        ).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            options, text="Dry run only", variable=self.dry_run_var
        ).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            options, text="Skip tool bootstrap", variable=self.skip_bootstrap_var
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Concurrency").grid(row=3, column=0, sticky="w", pady=(12, 0))
        ttk.Label(options, text="Retries").grid(row=3, column=1, sticky="w", pady=(12, 0))
        ttk.Label(options, text="Timeout (seconds)").grid(row=3, column=2, sticky="w", pady=(12, 0))
        ttk.Entry(options, textvariable=self.concurrency_var, width=10).grid(
            row=4, column=0, sticky="w"
        )
        ttk.Entry(options, textvariable=self.retries_var, width=10).grid(
            row=4, column=1, sticky="w"
        )
        ttk.Entry(options, textvariable=self.timeout_var, width=10).grid(
            row=4, column=2, sticky="w"
        )

        log_frame = ttk.LabelFrame(container, text="Activity", padding=12)
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = ScrolledText(log_frame, wrap="word", height=20, font=("Menlo", 11))
        if os.name == "nt":
            self.log_text.configure(font=("Consolas", 11))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

        actions = ttk.Frame(container)
        actions.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(actions, text="Start Download", command=self.start_download)
        self.start_button.grid(row=0, column=0, sticky="w")
        self.open_folder_button = ttk.Button(
            actions, text="Open Latest Folder", command=self.open_latest_folder, state="disabled"
        )
        self.open_folder_button.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.open_summary_button = ttk.Button(
            actions, text="Open Latest Summary", command=self.open_latest_summary, state="disabled"
        )
        self.open_summary_button.grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Button(actions, text="Clear Log", command=self.clear_log).grid(
            row=0, column=3, sticky="e", padx=(10, 0)
        )

        status = ttk.Label(container, textvariable=self.status_var, anchor="w")
        status.grid(row=6, column=0, sticky="ew", pady=(10, 0))

    def browse_output_dir(self) -> None:
        path = filedialog.askdirectory(
            title="Choose a download folder",
            initialdir=self.output_dir_var.get() or str(DEFAULT_OUTPUT_DIR),
        )
        if path:
            self.output_dir_var.set(path)

    def browse_cookies_file(self) -> None:
        path = filedialog.askopenfilename(title="Choose a cookies.txt file")
        if path:
            self.cookies_file_var.set(path)

    def update_cookie_controls(self) -> None:
        mode = self.cookie_mode_var.get()
        show_browser = mode == "browser"
        show_file = mode == "file"

        browser_state = "readonly" if show_browser else "disabled"
        file_state = "normal" if show_file else "disabled"
        button_state = "normal" if show_file else "disabled"
        self.browser_combo.configure(state=browser_state)
        self.cookies_entry.configure(state=file_state)
        self.cookies_button.configure(state=button_state)

        if show_browser or show_file:
            self.auth_details.grid()
        else:
            self.auth_details.grid_remove()

        if show_browser:
            self.browser_row.grid(row=0, column=0, sticky="ew")
        else:
            self.browser_row.grid_remove()

        if show_file:
            self.cookies_row.grid(row=0, column=0, sticky="ew")
        else:
            self.cookies_row.grid_remove()

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, line.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def parse_positive_int(self, value: str, label: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be a whole number.") from exc
        if parsed < 1:
            raise ValueError(f"{label} must be at least 1.")
        return parsed

    def build_cli_args(self) -> tuple[list[str], Path, Path]:
        profile = self.profile_var.get().strip()
        if not profile:
            raise ValueError("Enter a Twitter/X username or profile URL.")

        handle, _ = download_x_media.normalize_profile(profile)
        output_value = self.output_dir_var.get().strip()
        if not output_value:
            raise ValueError("Choose a download folder.")
        output_root = Path(output_value).expanduser()

        concurrency = self.parse_positive_int(self.concurrency_var.get().strip(), "Concurrency")
        retries = self.parse_positive_int(self.retries_var.get().strip(), "Retries")
        timeout = self.parse_positive_int(self.timeout_var.get().strip(), "Timeout")

        APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        output_root.mkdir(parents=True, exist_ok=True)

        args = [
            profile,
            "--output-dir",
            str(output_root.resolve()),
            "--venv-dir",
            str(resolve_gui_toolchain_dir().resolve()),
            "--concurrency",
            str(concurrency),
            "--retries",
            str(retries),
            "--timeout",
            str(timeout),
        ]

        mode = self.cookie_mode_var.get()
        if mode == "browser":
            browser = self.browser_var.get().strip()
            if not browser:
                raise ValueError("Choose a browser for cookie loading.")
            args.extend(["--cookies-browser", browser])
        elif mode == "file":
            cookies_value = self.cookies_file_var.get().strip()
            if not cookies_value:
                raise ValueError("Choose a cookies file.")
            cookies_file = Path(cookies_value).expanduser()
            if not cookies_file.exists():
                raise ValueError("The selected cookies file does not exist.")
            args.extend(["--cookies-file", str(cookies_file.resolve())])

        if self.include_retweets_var.get():
            args.append("--include-retweets")
        if self.include_quoted_var.get():
            args.append("--include-quoted")
        if self.exclude_pinned_var.get():
            args.append("--exclude-pinned")
        if self.write_info_json_var.get():
            args.append("--write-info-json")
        if self.verify_auth_var.get():
            args.append("--verify-auth")
        if self.dry_run_var.get():
            args.append("--dry-run")
        if self.skip_bootstrap_var.get():
            args.append("--skip-bootstrap")

        target_dir = output_root.resolve() / handle
        summary_path = target_dir / download_x_media.SUMMARY_FILENAME
        return args, target_dir, summary_path

    def start_download(self) -> None:
        if self.running:
            return

        try:
            cli_args, target_dir, summary_path = self.build_cli_args()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return

        self.save_settings()
        self.latest_target_dir = target_dir
        self.latest_summary_path = summary_path
        self.open_folder_button.configure(state="disabled")
        self.open_summary_button.configure(state="disabled")
        self.clear_log()
        self.append_log(f"[gui] starting download for {self.profile_var.get().strip()}")
        self.status_var.set("Downloading...")
        self.start_button.configure(state="disabled")
        self.running = True

        self.worker = threading.Thread(
            target=self.run_download_worker,
            args=(cli_args, target_dir, summary_path),
            daemon=True,
        )
        self.worker.start()

    def run_download_worker(self, cli_args: list[str], target_dir: Path, summary_path: Path) -> None:
        writer = QueueWriter(lambda line: self.events.put(("log", line)))
        exit_code = 1

        try:
            with redirect_stdout(writer), redirect_stderr(writer):
                exit_code = int(download_x_media.main(cli_args))
        except Exception:
            writer.flush()
            self.events.put(("log", traceback.format_exc().rstrip()))
            exit_code = 1
        finally:
            writer.flush()
            self.events.put(
                (
                    "done",
                    {
                        "exit_code": exit_code,
                        "target_dir": target_dir,
                        "summary_path": summary_path,
                    },
                )
            )

    def poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self.append_log(str(payload))
                elif event == "done":
                    self.finish_download(payload)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.poll_events)

    def finish_download(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        exit_code = int(data.get("exit_code", 1))
        target_dir = data.get("target_dir")
        summary_path = data.get("summary_path")
        status_message = "Download completed." if exit_code == 0 else "Download finished with errors."

        summary_status = None
        if isinstance(summary_path, Path) and summary_path.exists():
            try:
                with summary_path.open("r", encoding="utf-8") as handle:
                    summary = json.load(handle)
                summary_status = summary.get("status")
            except (OSError, json.JSONDecodeError):
                summary_status = None

        if summary_status == "partial":
            status_message = "Download completed with some failed files."
        elif summary_status == "error":
            status_message = "Download failed."

        self.running = False
        self.status_var.set(status_message)
        self.start_button.configure(state="normal")

        if isinstance(target_dir, Path) and target_dir.exists():
            self.open_folder_button.configure(state="normal")
        if isinstance(summary_path, Path) and summary_path.exists():
            self.open_summary_button.configure(state="normal")

    def open_latest_folder(self) -> None:
        if self.latest_target_dir and self.latest_target_dir.exists():
            reveal_path(self.latest_target_dir)

    def open_latest_summary(self) -> None:
        if self.latest_summary_path and self.latest_summary_path.exists():
            reveal_path(self.latest_summary_path)

    def save_settings(self) -> None:
        try:
            APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "profile": self.profile_var.get(),
                "output_dir": self.output_dir_var.get(),
                "cookie_mode": self.cookie_mode_var.get(),
                "browser": self.browser_var.get(),
                "cookies_file": self.cookies_file_var.get(),
                "concurrency": self.concurrency_var.get(),
                "retries": self.retries_var.get(),
                "timeout": self.timeout_var.get(),
                "include_retweets": self.include_retweets_var.get(),
                "include_quoted": self.include_quoted_var.get(),
                "exclude_pinned": self.exclude_pinned_var.get(),
                "write_info_json": self.write_info_json_var.get(),
                "verify_auth": self.verify_auth_var.get(),
                "dry_run": self.dry_run_var.get(),
                "skip_bootstrap": self.skip_bootstrap_var.get(),
            }
            with CONFIG_PATH.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
        except OSError:
            pass

    def load_settings(self) -> None:
        if not CONFIG_PATH.exists():
            return

        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return

        self.profile_var.set(str(payload.get("profile", "")))
        self.output_dir_var.set(str(payload.get("output_dir", DEFAULT_OUTPUT_DIR)))
        self.cookie_mode_var.set(str(payload.get("cookie_mode", "auto")))
        self.browser_var.set(str(payload.get("browser", "edge")))
        self.cookies_file_var.set(str(payload.get("cookies_file", "")))
        self.concurrency_var.set(str(payload.get("concurrency", "3")))
        self.retries_var.set(str(payload.get("retries", "3")))
        self.timeout_var.set(str(payload.get("timeout", "60")))
        self.include_retweets_var.set(bool(payload.get("include_retweets", False)))
        self.include_quoted_var.set(bool(payload.get("include_quoted", False)))
        self.exclude_pinned_var.set(bool(payload.get("exclude_pinned", False)))
        self.write_info_json_var.set(bool(payload.get("write_info_json", False)))
        self.verify_auth_var.set(bool(payload.get("verify_auth", False)))
        self.dry_run_var.set(bool(payload.get("dry_run", False)))
        self.skip_bootstrap_var.set(bool(payload.get("skip_bootstrap", False)))

    def on_close(self) -> None:
        if self.running:
            confirm = messagebox.askyesno(
                APP_NAME,
                "A download is still running. Close the app anyway?",
                parent=self.root,
            )
            if not confirm:
                return

        self.save_settings()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    DownloaderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
