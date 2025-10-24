"""Autonomous telnet client for The Two Towers controlled by an Ollama model.

This version delegates every in-game decision to the configured Ollama host
while keeping just enough local logic to log in, forward output, and relay the
model's commands.  The goal is to make remote control reliable: we buffer the
MUD transcript, colourise the console output for readability, and stream
requests to Ollama with generous timeouts plus retries so read timeouts no
longer interrupt play.
"""
from __future__ import annotations

import json
import os
import re
import sys
import telnetlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, List, Optional, Sequence, Tuple

import urllib3
from urllib3.exceptions import HTTPError, MaxRetryError, ReadTimeoutError
from urllib3.util import Retry

HOST = "t2tmud.org"
PORT = 9999

USERNAME_PROMPTS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"By what name do you wish to be known\??", re.IGNORECASE),
    re.compile(r"Enter your character name:", re.IGNORECASE),
    re.compile(r"Enter your name:", re.IGNORECASE),
    re.compile(r"Your name\??", re.IGNORECASE),
    re.compile(r"Please enter the name 'new' if you are new to The Two Towers\.", re.IGNORECASE),
)
PASSWORD_PROMPTS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"What is your password\??", re.IGNORECASE),
    re.compile(r"Password:", re.IGNORECASE),
    re.compile(r"Enter your password:", re.IGNORECASE),
    re.compile(r"Your name\?.*Password:", re.IGNORECASE),
)
HP_PROMPT = re.compile(r"^HP:\s*\d+\s+EP:\s*\d+>")
MORE_PROMPT = re.compile(r"--More--")
GENERIC_PROMPT = re.compile(r">\s*$")

DEFAULT_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "69.142.141.135")
DEFAULT_OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
DEFAULT_OLLAMA_CONNECT_TIMEOUT = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "6.0"))
DEFAULT_OLLAMA_READ_TIMEOUT = float(os.getenv("OLLAMA_READ_TIMEOUT", "120.0"))
DEFAULT_OLLAMA_MAX_RETRIES = int(os.getenv("OLLAMA_MAX_RETRIES", "3"))
MAX_CONTEXT_LINES = int(os.getenv("OLLAMA_CONTEXT_LINES", "240"))
MAX_COMMAND_HISTORY = 80

ENABLE_COLOR = os.getenv("ENABLE_COLOR", "1").lower() not in {"0", "false", "no"}
ANSI_RESET = "\033[0m"
ANSI_MAP = {
    "prompt": "\033[38;5;82m",
    "hint": "\033[38;5;220m",
    "help": "\033[38;5;39m",
    "more": "\033[38;5;213m",
    "event": "\033[38;5;208m",
    "error": "\033[38;5;196m",
}
COLOR_RULES: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^HP:\s*\d+\s+EP:\s*\d+>"), "prompt"),
    (re.compile(r"^\*\*\* HINT \*\*\*"), "hint"),
    (re.compile(r"^Help for "), "help"),
    (re.compile(r"--More--"), "more"),
    (re.compile(r"^\[event]"), "event"),
    (re.compile(r"^\[ollama error]"), "error"),
)


@dataclass(frozen=True)
class CharacterProfile:
    username: str
    password: str


PROFILES: Tuple[CharacterProfile, ...] = (
    CharacterProfile("Marchos", "hello123"),
    CharacterProfile("Zesty", "poopie"),
)


def _supports_color() -> bool:
    if not ENABLE_COLOR:
        return False
    term = os.getenv("TERM", "")
    return sys.stdout.isatty() and term.lower() not in {"", "dumb"}


COLOR_ENABLED = _supports_color()


def _colorize(line: str) -> str:
    if not COLOR_ENABLED:
        return line
    for pattern, color_key in COLOR_RULES:
        if pattern.search(line):
            colour = ANSI_MAP.get(color_key)
            if colour:
                return f"{colour}{line}{ANSI_RESET}"
    return line


class ProfileRotation:
    """Round-robin helper for switching between stored character profiles."""

    def __init__(self, profiles: Sequence[CharacterProfile]):
        if not profiles:
            raise ValueError("at least one profile is required")
        self._profiles: Tuple[CharacterProfile, ...] = tuple(profiles)
        self._index = 0
        self.lock = threading.Lock()

    @property
    def current(self) -> CharacterProfile:
        with self.lock:
            return self._profiles[self._index]

    def advance(self) -> CharacterProfile:
        with self.lock:
            self._index = (self._index + 1) % len(self._profiles)
            return self._profiles[self._index]


class OllamaController:
    """Wraps interaction with an Ollama server for command decisions."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_OLLAMA_HOST,
        port: int = DEFAULT_OLLAMA_PORT,
        model: str = DEFAULT_OLLAMA_MODEL,
        connect_timeout: float = DEFAULT_OLLAMA_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_OLLAMA_READ_TIMEOUT,
        max_retries: int = DEFAULT_OLLAMA_MAX_RETRIES,
        context_lines: int = MAX_CONTEXT_LINES,
        enabled: bool = True,
    ):
        self.host = host
        self.port = port
        self.model = model
        self.enabled = enabled
        self.context_lines = max(50, context_lines)
        self.history: Deque[str] = deque(maxlen=self.context_lines)
        self.command_history: Deque[str] = deque(maxlen=MAX_COMMAND_HISTORY)
        self._http: Optional[urllib3.PoolManager] = None
        self._timeout = urllib3.Timeout(connect=connect_timeout, read=read_timeout, total=None)
        self._retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            redirect=False,
            backoff_factor=0.6,
            status_forcelist=(500, 502, 503, 504),
            raise_on_status=False,
        )
        self._lock = threading.Lock()
        self._last_error: str = ""

    def reset(self, profile: CharacterProfile) -> None:
        with self._lock:
            self.history.clear()
            self.command_history.clear()
            self._last_error = ""
            self.history.append(f"[event] Connected as {profile.username}.")

    def note_output(self, text: str) -> None:
        if not text:
            return
        for line in text.splitlines():
            stripped = line.strip("\r")
            if stripped:
                with self._lock:
                    self.history.append(stripped)

    def note_event(self, message: str) -> None:
        with self._lock:
            self.history.append(f"[event] {message}")

    def note_command(self, command: str) -> None:
        if not command:
            command = "<ENTER>"
        with self._lock:
            self.command_history.append(command)

    def _build_prompt(self, reason: str, limit: int) -> str:
        with self._lock:
            transcript = list(self.history)[-limit:]
            recent_commands = list(self.command_history)[-12:]
            last_error = self._last_error
        transcript_text = "\n".join(transcript) if transcript else "(no previous transcript)"
        command_text = ", ".join(recent_commands) if recent_commands else "none yet"
        error_text = f"\nLast controller error: {last_error}" if last_error else ""
        instructions = (
            "You are piloting a telnet session for The Two Towers MUD. "
            "Respond ONLY with JSON matching this schema: {\"commands\": [command1, command2, ...]}.")
        guidance = (
            "Each command is a string exactly as it should be typed. Use at most two commands per reply. "
            "Send \"<ENTER>\" to press the return key for pagination prompts like --More--. "
            "If you need to wait for more output, respond with an empty command list (\"commands\": []). "
            "Avoid repeating the same command unless it is intentional."
        )
        prompt = (
            f"{instructions}\n{guidance}\nReason for request: {reason}{error_text}\n"
            f"Recent commands: {command_text}\n\n"
            f"Recent transcript (most recent last):\n```\n{transcript_text}\n```\n"
        )
        return prompt

    def _http_client(self) -> urllib3.PoolManager:
        if self._http is None:
            self._http = urllib3.PoolManager(timeout=self._timeout, retries=self._retry)
        return self._http

    def _post(self, prompt: str) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": True}
        encoded = json.dumps(payload).encode("utf-8")
        url = f"http://{self.host}:{self.port}/api/generate"
        try:
            response = self._http_client().request(
                "POST",
                url,
                body=encoded,
                headers={"Content-Type": "application/json"},
                preload_content=False,
            )
        except MaxRetryError as exc:  # pragma: no cover - network issue logging
            message = f"max retries reached contacting Ollama ({exc})"
            print(f"[ollama error] {message}", file=sys.stderr)
            with self._lock:
                self._last_error = message
            return ""
        except ReadTimeoutError as exc:  # pragma: no cover - network issue logging
            message = f"read timeout waiting for Ollama ({exc})"
            print(f"[ollama error] {message}", file=sys.stderr)
            with self._lock:
                self._last_error = message
            return ""
        except HTTPError as exc:  # pragma: no cover - network issue logging
            message = f"HTTP error contacting Ollama ({exc})"
            print(f"[ollama error] {message}", file=sys.stderr)
            with self._lock:
                self._last_error = message
            return ""
        except Exception as exc:  # pragma: no cover - safety net logging
            message = f"unexpected error contacting Ollama: {exc}"
            print(f"[ollama error] {message}", file=sys.stderr)
            with self._lock:
                self._last_error = message
            return ""

        chunks: List[str] = []
        try:
            for chunk in response.stream(amt=4096, decode_content=True):
                if not chunk:
                    continue
                if isinstance(chunk, bytes):
                    chunks.append(chunk.decode("utf-8", errors="ignore"))
                else:
                    chunks.append(str(chunk))
        finally:
            response.release_conn()
        raw = "".join(chunks).strip()
        if raw:
            with self._lock:
                self._last_error = ""
        return raw

    def _parse_commands(self, text: str) -> List[str]:
        if not text:
            return []
        text = text.strip()
        parsed: Optional[dict]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        commands: List[str] = []
        if isinstance(parsed, dict):
            raw_commands = parsed.get("commands")
            if isinstance(raw_commands, Iterable):
                for entry in raw_commands:
                    if isinstance(entry, str):
                        cleaned = entry.strip()
                        if cleaned:
                            commands.append(cleaned)
                return commands[:2]
        for line in text.splitlines():
            cleaned = line.strip().strip("#")
            if cleaned:
                commands.append(cleaned)
                if len(commands) >= 2:
                    break
        return commands

    def request_commands(self, reason: str) -> List[str]:
        if not self.enabled:
            return []
        context_sizes = (self.context_lines, max(60, self.context_lines // 2))
        for limit in context_sizes:
            prompt = self._build_prompt(reason, limit)
            raw = self._post(prompt)
            commands = self._parse_commands(raw)
            if commands or raw:
                return commands
        return []


class GameClient:
    """Manages the telnet connection, prompting Ollama when input is needed."""

    def __init__(self, rotation: ProfileRotation, controller: OllamaController):
        self.rotation = rotation
        self.controller = controller
        self.connection: Optional[telnetlib.Telnet] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.disconnected = threading.Event()
        self.awaiting_commands = threading.Lock()
        self.pending_reasons: Deque[str] = deque()
        self.partial_line = ""
        self.logged_in = False
        self.active_profile: Optional[CharacterProfile] = None

    def start(self) -> None:
        try:
            while not self.stop_event.is_set():
                if self.connection is None:
                    self._connect(self.rotation.current)
                disconnected = self.disconnected.wait(0.5)
                if disconnected:
                    self.disconnected.clear()
                    if self.stop_event.is_set():
                        break
                    next_profile = self.rotation.advance()
                    time.sleep(1.0)
                    self._connect(next_profile)
        except KeyboardInterrupt:
            print("\n[ event ] Keyboard interrupt received, shutting down.")
        finally:
            self.stop_event.set()
            self._close_connection()

    def _connect(self, profile: CharacterProfile) -> None:
        self._close_connection()
        print(_colorize(f"[event] Connecting to {HOST}:{PORT} as {profile.username}..."))
        try:
            self.connection = telnetlib.Telnet(HOST, PORT, timeout=10)
        except Exception as exc:
            print(_colorize(f"[event] Connection failed: {exc}"))
            self.connection = None
            time.sleep(3)
            return
        self.active_profile = profile
        self.partial_line = ""
        self.logged_in = False
        self.controller.reset(profile)
        self.controller.note_event(f"Awaiting login for {profile.username}.")
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def _close_connection(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None

    def _reader_loop(self) -> None:
        assert self.connection is not None
        conn = self.connection
        while not self.stop_event.is_set():
            try:
                raw = conn.read_very_eager()
            except EOFError:
                break
            except OSError:
                break
            if raw:
                text = raw.decode("utf-8", errors="ignore")
                self._handle_text(text)
            else:
                time.sleep(0.1)
        self.controller.note_event("Connection closed by remote host.")
        print(_colorize("[event] Disconnected."))
        self.disconnected.set()

    def _handle_text(self, text: str) -> None:
        self.controller.note_output(text)
        for ch in text:
            self.partial_line += ch
            if ch == "\n":
                line = self.partial_line.rstrip("\r\n")
                self.partial_line = ""
                self._process_line(line)
        stripped = self.partial_line.strip()
        if HP_PROMPT.match(stripped) or MORE_PROMPT.search(stripped):
            line = stripped
            self.partial_line = ""
            self._process_line(line)

    def _process_line(self, line: str) -> None:
        if not line:
            return
        print(_colorize(line))
        lower_line = line.lower()
        if any(pattern.search(line) for pattern in USERNAME_PROMPTS):
            self._send_credential(self.active_profile.username if self.active_profile else "")
            return
        if any(pattern.search(line) for pattern in PASSWORD_PROMPTS):
            self._send_credential(self.active_profile.password if self.active_profile else "")
            return
        if HP_PROMPT.match(line):
            if not self.logged_in:
                self.logged_in = True
                self.controller.note_event("Login confirmed.")
            self._queue_reason("character status prompt")
            return
        if MORE_PROMPT.search(line):
            self._queue_reason("pagination --More-- prompt")
            return
        if "connection closed" in lower_line:
            self.disconnected.set()
        if GENERIC_PROMPT.search(line) and self.logged_in:
            self._queue_reason("generic prompt")

    def _send_credential(self, value: str) -> None:
        if not self.connection:
            return
        safe_value = value or ""
        payload = (safe_value + "\n").encode("utf-8", errors="ignore")
        try:
            self.connection.write(payload)
        except Exception as exc:
            print(_colorize(f"[event] Failed to send credential: {exc}"))
        redacted = value if value else "<empty>"
        self.controller.note_event(f"Sent credential {redacted!r}.")

    def _queue_reason(self, reason: str) -> None:
        self.pending_reasons.append(reason)
        self._maybe_dispatch()

    def _maybe_dispatch(self) -> None:
        if self.stop_event.is_set():
            return
        if not self.pending_reasons:
            return
        if not self.connection:
            return
        if not self.awaiting_commands.acquire(blocking=False):
            return
        reason = self.pending_reasons.popleft()
        threading.Thread(target=self._dispatch_commands, args=(reason,), daemon=True).start()

    def _dispatch_commands(self, reason: str) -> None:
        try:
            commands = self.controller.request_commands(reason)
            if not commands:
                return
            for command in commands:
                self._send_command(command)
                time.sleep(0.25)
        finally:
            self.awaiting_commands.release()
            if self.pending_reasons:
                self._maybe_dispatch()

    def _send_command(self, command: str) -> None:
        if not self.connection:
            return
        normalized = command.strip()
        if not normalized:
            normalized = "<ENTER>"
        if normalized.upper() == "<ENTER>":
            payload = "\n"
            display = "<ENTER>"
        else:
            payload = normalized + "\n"
            display = normalized
        self.controller.note_command(display)
        print(_colorize(f"[event] Sending command: {display}"))
        try:
            self.connection.write(payload.encode("utf-8", errors="ignore"))
        except Exception as exc:
            print(_colorize(f"[event] Failed to send command '{display}': {exc}"))


def main() -> None:
    rotation = ProfileRotation(PROFILES)
    controller = OllamaController()
    client = GameClient(rotation, controller)
    client.start()


if __name__ == "__main__":
    main()
