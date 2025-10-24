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
import http.client

HOST = os.getenv("T2T_HOST", "t2tmud.org")
PORT = int(os.getenv("T2T_PORT", "9999"))

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "69.142.141.135")
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
OLLAMA_CONNECT_TIMEOUT = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "5.0"))
OLLAMA_READ_TIMEOUT = float(os.getenv("OLLAMA_READ_TIMEOUT", "120.0"))
OLLAMA_MAX_RETRIES = int(os.getenv("OLLAMA_MAX_RETRIES", "3"))
OLLAMA_ENABLED = os.getenv("ENABLE_OLLAMA", "1").lower() not in {"0", "false", "no"}

COLOR_OUTPUT = os.getenv("ENABLE_COLOR", "1").lower() not in {"0", "false", "no"}

ANSI_RESET = "\033[0m"
ANSI_COLORS = {
    "prompt": "\033[38;5;82m",
    "hint": "\033[38;5;220m",
    "help": "\033[38;5;39m",
    "more": "\033[38;5;213m",
    "event": "\033[38;5;208m",
    "error": "\033[38;5;196m",
}

PROMPT_PATTERN = re.compile(r"HP:\s*\d+\s+EP:\s*\d+>")
USERNAME_PATTERNS = [
    re.compile(p)
    for p in (
        r"By what name do you wish to be known\??",
        r"Enter your character name:",
        r"Enter your name:",
        r"Your name\??",
        r"Please enter the name 'new' if you are new to The Two Towers\.",
    )
]
PASSWORD_PATTERNS = [
    re.compile(p)
    for p in (
        r"What is your password\??",
        r"Password:",
        r"Enter your password:",
        r"Your name\?.*Password:",
    )
]
MORE_PATTERN = re.compile(r"--More--")
HELP_HEADER_PATTERN = re.compile(r"^Help for ")
HINT_PATTERN = re.compile(r"^\*\*\* HINT \*\*\*")


def supports_color() -> bool:
    if not COLOR_OUTPUT:
        return False
    term = os.getenv("TERM", "")
    return sys.stdout.isatty() and term.lower() not in {"", "dumb"}


COLOR_ENABLED = supports_color()


def apply_color(line: str) -> str:
    if not COLOR_ENABLED:
        return line
    if PROMPT_PATTERN.search(line):
        color = ANSI_COLORS["prompt"]
    elif HINT_PATTERN.search(line):
        color = ANSI_COLORS["hint"]
    elif HELP_HEADER_PATTERN.search(line):
        color = ANSI_COLORS["help"]
    elif MORE_PATTERN.search(line):
        color = ANSI_COLORS["more"]
    elif line.startswith("[event]"):
        color = ANSI_COLORS["event"]
    elif line.startswith("[error]"):
        color = ANSI_COLORS["error"]
    else:
        return line
    return f"{color}{line}{ANSI_RESET}"


def print_output(text: str):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    for line in lines[:-1]:
        print(apply_color(line))
    if lines[-1]:
        print(apply_color(lines[-1]), end="")
    else:
        sys.stdout.flush()


@dataclass
class CharacterProfile:
    username: str
    password: str
    label: str


DEFAULT_PROFILES: Sequence[CharacterProfile] = (
    CharacterProfile("Marchos", "hello123", "Marchos"),
    CharacterProfile("Zesty", "poopie", "Zesty"),
)


class OllamaAgent:
    def __init__(
        self,
        *,
        send_callback,
        host: str,
        port: int,
        model: str,
        enabled: bool,
        connect_timeout: float,
        read_timeout: float,
        max_retries: int,
        context_limit: int = 8000,
    ):
        self._send_callback = send_callback
        self.host = host
        self.port = port
        self.model = model
        self.enabled = enabled
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        self.context_limit = context_limit
        self._transcript: Deque[str] = deque()
        self._commands: Deque[str] = deque(maxlen=32)
        self._pending = threading.Event()
        self._active = threading.Event()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def activate(self):
        if not self.enabled:
            return
        self._active.set()
        self.request_commands("session start")

    def deactivate(self):
        self._active.clear()

    def reset(self):
        with self._lock:
            self._transcript.clear()
            self._commands.clear()
        self._pending.clear()
        self._active.clear()

    def observe(self, text: str):
        if not self.enabled:
            return
        cleaned = text.replace("\r", "")
        if not cleaned:
            return
        with self._lock:
            self._transcript.append(cleaned)
            while sum(len(chunk) for chunk in self._transcript) > self.context_limit:
                self._transcript.popleft()

    def record_command(self, command: str):
        if not self.enabled:
            return
        if not self._active.is_set():
            return
        trimmed = command.strip()
        if trimmed:
            with self._lock:
                self._commands.append(trimmed)

    def request_commands(self, reason: str = ""):
        if not self.enabled:
            return
        if not self._active.is_set():
            return
        self._pending.set()
        if reason:
            print_output(f"[event] Requesting commands ({reason})\n")

    def on_more_prompt(self):
        if not self.enabled:
            return
        self.request_commands("pagination")

    def _worker_loop(self):
        while True:
            self._pending.wait()
            self._pending.clear()
            if not self.enabled or not self._active.is_set():
                continue
            prompt = self._build_prompt()
            if not prompt:
                continue
            response = self._query_ollama(prompt)
            commands = self._parse_response(response)
            if not commands:
                continue
            for cmd in commands:
                self._send_callback(cmd)
                self.record_command(cmd)
                time.sleep(0.3)

    def _build_prompt(self) -> Optional[str]:
        with self._lock:
            if not self._transcript:
                return None
            transcript = "".join(self._transcript)
            recent = list(self._commands)[-10:]
        guidance = (
            "You are remotely controlling a character in The Two Towers MUD via "
            "telnet. Decide the next up to three commands to issue. When "
            "pagination 'More' prompts appear, send an empty string command to "
            "continue. Return only JSON with a `commands` list (strings) and an "
            "optional `comment`."
        )
        if recent:
            history = "Recent commands: " + ", ".join(recent)
        else:
            history = ""
        payload = f"{guidance}\n{history}\nLatest transcript:\n```\n{transcript}\n```"
        return payload

    def _query_ollama(self, prompt: str) -> Optional[str]:
        if not self.enabled:
            return None
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False}).encode()
        headers = {"Content-Type": "application/json"}
        last_error: Optional[str] = None
        for attempt in range(1, self.max_retries + 2):
            try:
                conn = http.client.HTTPConnection(
                    self.host,
                    self.port,
                    timeout=self.connect_timeout,
                )
                conn.request("POST", "/api/generate", body=body, headers=headers)
                response = conn.getresponse()
                if conn.sock is not None:
                    conn.sock.settimeout(self.read_timeout)
                if response.status != 200:
                    last_error = f"HTTP {response.status}"
                    conn.close()
                    raise RuntimeError(last_error)
                raw = response.read()
                conn.close()
                return raw.decode("utf-8", errors="ignore")
            except socket.timeout:
                last_error = "timeout"
            except Exception as exc:  # pragma: no cover - network interaction
                last_error = str(exc)
            time.sleep(1.5)
        if last_error:
            print_output(f"[error] Ollama request failed: {last_error}\n")
        return None

    def _parse_response(self, payload: Optional[str]) -> List[str]:
        if not payload:
            return []
        payload = payload.strip()
        if not payload:
            return []
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            print_output("[error] Ollama returned invalid JSON\n")
            return []
        commands = data.get("commands")
        if not isinstance(commands, list):
            return []
        result: List[str] = []
        for item in commands[:3]:
            if isinstance(item, str):
                result.append(item)
        return result


class TelnetClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.connection: Optional[telnetlib.Telnet] = None
        self.profile: Optional[CharacterProfile] = None
        self.agent: Optional[OllamaAgent] = None
        self._listener: Optional[threading.Thread] = None
        self._buffer = ""
        self._logged_in = False
        self._stop_event = threading.Event()
        self._send_lock = threading.Lock()

    def connect(self, profile: CharacterProfile, agent: OllamaAgent):
        self.profile = profile
        self.agent = agent
        self._buffer = ""
        self._logged_in = False
        self._stop_event.clear()
        agent.reset()
        try:
            self.connection = telnetlib.Telnet(self.host, self.port)
        except OSError as exc:
            raise RuntimeError(f"Failed to connect: {exc}") from exc
        self._listener = threading.Thread(target=self._listen_loop, daemon=True)
        self._listener.start()
        print_output(f"[event] Connected as {profile.label}\n")

    def disconnect(self):
        self._stop_event.set()
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
            self.connection = None
        if self._listener and self._listener.is_alive():
            self._listener.join(timeout=1.0)
        self._listener = None
        if self.agent:
            self.agent.deactivate()
        print_output("[event] Connection closed\n")

    def send(self, command: str):
        if self.connection is None:
            return
        to_send = command + "\n"
        with self._send_lock:
            self.connection.write(to_send.encode("ascii", errors="ignore"))
        if self.agent:
            self.agent.record_command(command)
        if command:
            print_output(f"[event] >>> {command}\n")
        else:
            print_output("[event] >>> (newline)\n")

    def _listen_loop(self):
        assert self.connection is not None
        while not self._stop_event.is_set():
            try:
                raw = self.connection.read_very_eager()
            except EOFError:
                break
            except OSError:
                break
            if not raw:
                time.sleep(0.05)
                continue
            text = raw.decode("ascii", errors="ignore")
            print_output(text)
            if self.agent:
                self.agent.observe(text)
            self._buffer += text
            if len(self._buffer) > 8192:
                self._buffer = self._buffer[-8192:]
            self._process_buffer()
        self._stop_event.set()
        self.connection = None

    def _process_buffer(self):
        if self.profile is None:
            return
        profile = self.profile
        for pattern in USERNAME_PATTERNS:
            if pattern.search(self._buffer):
                self.send(profile.username)
                self._consume(pattern)
                return
        for pattern in PASSWORD_PATTERNS:
            if pattern.search(self._buffer):
                if "Your name" in pattern.pattern:
                    self.send(profile.username)
                    time.sleep(0.2)
                self.send(profile.password)
                self._consume(pattern)
                return
        if PROMPT_PATTERN.search(self._buffer):
            self._logged_in = True
            if self.agent:
                self.agent.activate()
                self.agent.request_commands("prompt")
            self._consume(PROMPT_PATTERN)
            return
        if MORE_PATTERN.search(self._buffer):
            if self.agent:
                self.agent.on_more_prompt()
            self._consume(MORE_PATTERN)
            return

    def _consume(self, pattern: re.Pattern[str]):
        match = pattern.search(self._buffer)
        if not match:
            return
        end = match.end()
        self._buffer = self._buffer[end:]


class SessionManager:
    def __init__(self, profiles: Sequence[CharacterProfile]):
        if not profiles:
            raise ValueError("At least one profile is required")
        self.profiles = list(profiles)
        self.index = 0
        self.client = TelnetClient(HOST, PORT)
        self.agent = OllamaAgent(
            send_callback=self.client.send,
            host=OLLAMA_HOST,
            port=OLLAMA_PORT,
            model=OLLAMA_MODEL,
            enabled=OLLAMA_ENABLED,
            connect_timeout=OLLAMA_CONNECT_TIMEOUT,
            read_timeout=OLLAMA_READ_TIMEOUT,
            max_retries=OLLAMA_MAX_RETRIES,
        )

    def current_profile(self) -> CharacterProfile:
        return self.profiles[self.index]

    def rotate_profile(self):
        self.index = (self.index + 1) % len(self.profiles)

    def run(self):
        while True:
            profile = self.current_profile()
            try:
                self.client.connect(profile, self.agent)
            except RuntimeError as exc:
                print_output(f"[error] {exc}\n")
                time.sleep(5)
                continue
            self._session_loop()
            self.client.disconnect()
            self.rotate_profile()
            time.sleep(3)

    def _session_loop(self):
        while True:
            if self.client.connection is None:
                return
            if not self.client._listener or not self.client._listener.is_alive():
                return
            time.sleep(0.5)


def main():
    manager = SessionManager(DEFAULT_PROFILES)
    try:
        manager.run()
    except KeyboardInterrupt:
        print_output("\n[event] Interrupted by user\n")
        manager.client.disconnect()


if __name__ == "__main__":
    main()
