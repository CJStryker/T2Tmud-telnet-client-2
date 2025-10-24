import http.client
import json
import os
import re
import socket
import sys
import telnetlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Sequence

HOST = "t2tmud.org"
PORT = 9999

USERNAME_PROMPTS = (
    r"By what name do you wish to be known\??",
    r"Enter your character name:",
    r"Enter your name:",
    r"Your name\??",
    r"Please enter the name 'new' if you are new to The Two Towers\.",
)
PASSWORD_PROMPTS = (
    r"What is your password\??",
    r"Password:",
    r"Enter your password:",
    r"Your name\?.*Password:",
)
LOGIN_SUCCESS_PATTERN = r"HP:\s*\d+\s+EP:\s*\d+>"
PROMPT_PATTERN = re.compile(LOGIN_SUCCESS_PATTERN)
MORE_PATTERN = re.compile(r"--More--")
EXITS_PATTERN = re.compile(
    r"(?:The only obvious exits are|Standard exits:)(?P<exits>.+)", re.IGNORECASE
)
HELP_HEADER_PATTERN = re.compile(r"^Help for (?P<topic>[^\(]+)")
HINT_PATTERN = re.compile(r"^\*\*\* HINT \*\*\*")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "69.142.141.135")
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
OLLAMA_CONNECT_TIMEOUT = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "10.0"))
OLLAMA_READ_TIMEOUT = float(os.getenv("OLLAMA_READ_TIMEOUT", "120.0"))
OLLAMA_MAX_ATTEMPTS = int(os.getenv("OLLAMA_MAX_ATTEMPTS", "4"))
OLLAMA_ENABLED = os.getenv("ENABLE_OLLAMA", "1").lower() not in {"0", "false", "no"}
OLLAMA_MAX_CONTEXT_CHARS = int(os.getenv("OLLAMA_CONTEXT", "6000"))
OLLAMA_COMMAND_SPACING = float(os.getenv("OLLAMA_COMMAND_SPACING", "0.4"))

COLOR_OUTPUT = os.getenv("ENABLE_COLOR", "1").lower() not in {"0", "false", "no"}
ANSI_RESET = "\033[0m"
ANSI_COLORS = {
    "prompt": "\033[38;5;82m",
    "hint": "\033[38;5;220m",
    "help": "\033[38;5;39m",
    "more": "\033[38;5;213m",
    "event": "\033[38;5;208m",
    "command": "\033[38;5;51m",
}
COLOR_PATTERNS = (
    (re.compile(r"^HP:\s*\d+\s+EP:\s*\d+>"), "prompt"),
    (re.compile(r"^\*\*\* HINT \*\*\*"), "hint"),
    (re.compile(r"^Help for "), "help"),
    (re.compile(r"--More--"), "more"),
    (re.compile(r"^\[command]"), "command"),
    (re.compile(r"^\[event]"), "event"),
    (re.compile(r"^\[ollama]"), "event"),
)


def supports_color() -> bool:
    if not COLOR_OUTPUT:
        return False
    term = os.getenv("TERM", "")
    return sys.stdout.isatty() and term.lower() not in {"", "dumb"}


COLOR_ENABLED = supports_color()


class ColorFormatter:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def format(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        lines = text.split("\n")
        formatted = [self._format_line(line) for line in lines]
        return "\n".join(formatted)

    @staticmethod
    def _format_line(line: str) -> str:
        for pattern, key in COLOR_PATTERNS:
            if pattern.search(line):
                color = ANSI_COLORS.get(key)
                if color:
                    return f"{color}{line}{ANSI_RESET}"
        return line


@dataclass
class CharacterProfile:
    username: str
    password: str
    intro_commands: Sequence[str] = ()


CHARACTER_PROFILES: Sequence[CharacterProfile] = (
    CharacterProfile(
        username="Marchos",
        password="hello123",
    ),
    CharacterProfile(
        username="Zesty",
        password="poopie",
    ),
)


class ProfileRotator:
    def __init__(self, profiles: Sequence[CharacterProfile]):
        self._profiles = list(profiles)
        self._index = 0

    def current(self) -> CharacterProfile:
        return self._profiles[self._index]

    def advance(self) -> CharacterProfile:
        if self._profiles:
            self._index = (self._index + 1) % len(self._profiles)
        return self.current()


class TranscriptBuffer:
    def __init__(self, max_chars: int):
        self.max_chars = max_chars
        self._buffer: Deque[str] = deque()
        self._length = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._buffer.append(text)
            self._length += len(text)
            while self._length > self.max_chars and self._buffer:
                removed = self._buffer.popleft()
                self._length -= len(removed)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._length = 0

    def snapshot(self) -> str:
        with self._lock:
            return "".join(self._buffer)


class OllamaController:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        model: str,
        enabled: bool,
        max_context_chars: int,
        connect_timeout: float,
        read_timeout: float,
        max_attempts: int,
        command_spacing: float,
    ):
        self.host = host
        self.port = port
        self.model = model
        self.enabled = enabled
        self.max_attempts = max(1, max_attempts)
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.command_spacing = max(command_spacing, 0.0)
        self._history = TranscriptBuffer(max_context_chars)
        self._command_history: Deque[str] = deque(maxlen=40)
        self._events: Deque[str] = deque(maxlen=40)
        self._lock = threading.Lock()
        self._pending = threading.Event()
        self._stop = threading.Event()
        self._waiting = False
        self._client: Optional["MUDSession"] = None
        self._thread: Optional[threading.Thread] = None
        self._current_character = ""
        self._last_error = ""
        if self.enabled:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def attach(self, client: "MUDSession") -> None:
        if not self.enabled:
            return
        with self._lock:
            self._client = client
            self._waiting = False

    def detach(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._client = None
            self._waiting = False

    def shutdown(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        self._pending.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def reset(self) -> None:
        if not self.enabled:
            return
        self._history.clear()
        with self._lock:
            self._command_history.clear()
            self._events.clear()
            self._last_error = ""

    def set_current_character(self, name: str) -> None:
        if not self.enabled:
            return
        self._current_character = name
        self.record_event(f"Connected as {name}")

    def record_output(self, text: str) -> None:
        if not self.enabled:
            return
        self._history.append(text)

    def record_command(self, command: str) -> None:
        if not self.enabled:
            return
        cleaned = command.strip()
        with self._lock:
            if cleaned:
                self._command_history.append(cleaned)
        self._history.append(f">>> {command}\n")

    def record_event(self, message: str) -> None:
        if not self.enabled:
            return
        cleaned = message.strip()
        if not cleaned:
            return
        with self._lock:
            self._events.append(cleaned)
        self._history.append(f"[event] {cleaned}\n")

    def record_error(self, message: str) -> None:
        if not self.enabled:
            return
        cleaned = message.strip()
        with self._lock:
            self._last_error = cleaned
        if cleaned:
            self._history.append(f"[event] Ollama error: {cleaned}\n")

    def notify_prompt(self, reason: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._waiting or self._client is None:
                return
            self._waiting = True
        self._pending.set()
        if reason:
            self.record_event(f"Requesting commands ({reason})")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._pending.wait()
            self._pending.clear()
            if self._stop.is_set():
                break
            commands, comment = self._generate_commands()
            with self._lock:
                self._waiting = False
            if not commands:
                continue
            client = self._client
            if not client:
                continue
            if comment:
                client.print_event(f"Ollama: {comment}")
            for command in commands:
                client.execute_remote_command(command)
                time.sleep(self.command_spacing)

    def _generate_commands(self) -> (List[str], Optional[str]):
        context = self._history.snapshot()
        if not context.strip():
            return [], None
        with self._lock:
            recent_commands = list(self._command_history)[-12:]
            events = list(self._events)[-8:]
            last_error = self._last_error
            character = self._current_character
        prompt = self._build_prompt(
            context=context,
            character=character,
            recent_commands=recent_commands,
            events=events,
            last_error=last_error,
        )
        response = self._request_ollama(prompt)
        if not response:
            return [], None
        return self._parse_response(response)

    def _build_prompt(
        self,
        *,
        context: str,
        character: str,
        recent_commands: Sequence[str],
        events: Sequence[str],
        last_error: str,
    ) -> str:
        header = (
            "You are controlling a player character connected to The Two Towers "
            "MUD through a telnet client. You must decide the next in-game "
            "commands. Respond strictly with JSON in the format {\"commands\": "
            "[\"cmd\", ...], \"comment\": \"optional brief note\"}. Include "
            "between one and three commands. Use an empty string command to press "
            "ENTER, e.g. to advance --More-- prompts. Avoid meta commentary."  
        )
        metadata_lines: List[str] = []
        if character:
            metadata_lines.append(f"Current character: {character}")
        if recent_commands:
            metadata_lines.append("Recent commands: " + ", ".join(recent_commands))
        if events:
            metadata_lines.append("Recent events: " + "; ".join(events))
        if last_error:
            metadata_lines.append("Last Ollama issue: " + last_error)
        metadata = "\n".join(metadata_lines)
        if metadata:
            metadata = f"\nContext summary:\n{metadata}\n"
        return (
            f"{header}{metadata}\n\nTranscript:\n```\n{context}\n```\n\n"
            "Return only valid JSON."
        )

    def _request_ollama(self, prompt: str) -> Optional[str]:
        payload = json.dumps({"model": self.model, "prompt": prompt, "stream": False})
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            try:
                conn = http.client.HTTPConnection(
                    self.host,
                    self.port,
                    timeout=max(self.connect_timeout, self.read_timeout),
                )
                conn.request(
                    "POST",
                    "/api/generate",
                    body=payload,
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                data = response.read()
                conn.close()
            except (socket.timeout, ConnectionError, OSError) as exc:
                message = f"attempt {attempt} failed: {exc}"
                print(f"[ollama] {message}", file=sys.stderr)
                self.record_error(message)
                time.sleep(min(5.0, 1.5 ** attempt))
                continue
            if response.status != 200:
                message = f"HTTP {response.status}"
                print(f"[ollama] {message}", file=sys.stderr)
                self.record_error(message)
                time.sleep(min(5.0, 1.5 ** attempt))
                continue
            try:
                parsed = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError as exc:
                message = f"invalid JSON from Ollama: {exc}"
                print(f"[ollama] {message}", file=sys.stderr)
                self.record_error(message)
                continue
            self.record_error("")
            return parsed.get("response", "")
        return None

    def _parse_response(self, text: str) -> (List[str], Optional[str]):
        stripped = text.strip()
        if not stripped:
            return [], None
        commands: List[str] = []
        comment: Optional[str] = None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            raw_commands = payload.get("commands")
            if isinstance(raw_commands, list):
                for entry in raw_commands:
                    if isinstance(entry, str):
                        commands.append(entry)
            raw_comment = payload.get("comment")
            if isinstance(raw_comment, str):
                comment = raw_comment.strip() or None
        if not commands:
            for line in stripped.splitlines():
                line = line.strip().strip("#").strip()
                if line:
                    commands.append(line)
                if len(commands) >= 3:
                    break
        return commands[:3], comment


class MUDSession:
    def __init__(
        self,
        host: str,
        port: int,
        profile: CharacterProfile,
        controller: OllamaController,
    ):
        self.host = host
        self.port = port
        self.profile = profile
        self.controller = controller
        self.telnet: Optional[telnetlib.Telnet] = None
        self.stop_event = threading.Event()
        self.reader_thread: Optional[threading.Thread] = None
        self.color = ColorFormatter(COLOR_ENABLED)
        self._buffer = ""
        self._username_sent = False
        self._password_sent = False
        self._logged_in = False
        self._last_prompt_signal = 0.0

    def run(self) -> None:
        self.controller.reset()
        self.connect()
        self.controller.attach(self)
        self.controller.set_current_character(self.profile.username)
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        try:
            while self.reader_thread.is_alive():
                self.reader_thread.join(timeout=0.5)
        finally:
            self.stop_event.set()
            self.controller.detach()
            self.disconnect()

    def connect(self) -> None:
        self.telnet = telnetlib.Telnet(self.host, self.port)
        self._username_sent = False
        self._password_sent = False
        self._logged_in = False
        self._buffer = ""
        self._last_prompt_signal = 0.0
        self.controller.record_event("Connected to server")

    def disconnect(self) -> None:
        if self.telnet is None:
            return
        try:
            self.telnet.close()
        except OSError:
            pass
        self.telnet = None
        self.controller.record_event("Disconnected from server")

    def _reader_loop(self) -> None:
        assert self.telnet is not None
        while not self.stop_event.is_set():
            try:
                data = self.telnet.read_very_eager()
            except EOFError:
                break
            except OSError:
                break
            if data:
                text = data.decode("utf-8", errors="ignore")
                self._handle_output(text)
            else:
                time.sleep(0.05)
        self.stop_event.set()

    def _handle_output(self, text: str) -> None:
        if not text:
            return
        self.controller.record_output(text)
        print(self.color.format(text), end="", flush=True)
        self._buffer += text
        if len(self._buffer) > 5000:
            self._buffer = self._buffer[-5000:]
        self._process_login_prompts()
        self._process_markers(text)

    def _process_login_prompts(self) -> None:
        buffer = self._buffer
        for pattern in USERNAME_PROMPTS:
            if re.search(pattern, buffer, re.IGNORECASE):
                self._send_username()
                break
        for pattern in PASSWORD_PROMPTS:
            if re.search(pattern, buffer, re.IGNORECASE):
                self._send_password()
                break

    def _send_username(self) -> None:
        if self.telnet is None:
            return
        self._username_sent = True
        self._password_sent = False
        message = self.profile.username
        self._write_line(message)
        self.controller.record_event(f"Sent username for {self.profile.username}")

    def _send_password(self) -> None:
        if self.telnet is None or not self._username_sent:
            return
        self._password_sent = True
        message = self.profile.password
        self._write_line(message)
        self.controller.record_event("Sent password")

    def _write_line(self, text: str) -> None:
        if self.telnet is None:
            return
        try:
            self.telnet.write((text + "\n").encode("utf-8"))
        except OSError:
            self.stop_event.set()

    def _process_markers(self, text: str) -> None:
        if PROMPT_PATTERN.search(text):
            self._on_prompt("status prompt")
            self._logged_in = True
        if MORE_PATTERN.search(text):
            self._on_prompt("pagination")
        for match in EXITS_PATTERN.finditer(text):
            exits = match.group("exits").strip()
            if exits:
                self.controller.record_event(f"Exits: {exits}")
        for line in text.splitlines():
            header_match = HELP_HEADER_PATTERN.match(line.strip())
            if header_match:
                topic = header_match.group("topic").strip()
                self.controller.record_event(f"Reading help: {topic}")
            if HINT_PATTERN.match(line.strip()):
                self.controller.record_event("Hint shown")

    def _on_prompt(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_prompt_signal < 0.3:
            return
        self._last_prompt_signal = now
        if self._logged_in:
            self.controller.notify_prompt(reason)

    def execute_remote_command(self, command: str) -> None:
        cmd = command
        if cmd is None:
            return
        display = cmd if cmd else "<ENTER>"
        self.print_command(display)
        if self.telnet is None:
            return
        try:
            if cmd:
                self.telnet.write((cmd + "\n").encode("utf-8"))
            else:
                self.telnet.write(b"\n")
        except OSError:
            self.stop_event.set()
            return
        self.controller.record_command(cmd)

    def print_command(self, command: str) -> None:
        print(self.color.format(f"[command] {command}"))

    def print_event(self, message: str) -> None:
        print(self.color.format(f"[event] {message}"))


def main() -> None:
    rotator = ProfileRotator(CHARACTER_PROFILES)
    controller = OllamaController(
        host=OLLAMA_HOST,
        port=OLLAMA_PORT,
        model=OLLAMA_MODEL,
        enabled=OLLAMA_ENABLED,
        max_context_chars=OLLAMA_MAX_CONTEXT_CHARS,
        connect_timeout=OLLAMA_CONNECT_TIMEOUT,
        read_timeout=OLLAMA_READ_TIMEOUT,
        max_attempts=OLLAMA_MAX_ATTEMPTS,
        command_spacing=OLLAMA_COMMAND_SPACING,
    )
    try:
        while True:
            profile = rotator.current()
            session = MUDSession(HOST, PORT, profile, controller)
            session.run()
            rotator.advance()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nInterrupted by user. Shutting down...")
    finally:
        controller.shutdown()


if __name__ == "__main__":
    main()
