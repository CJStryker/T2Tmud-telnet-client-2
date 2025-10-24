import json
import os
import queue
import re
import sys
import telnetlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Sequence

import urllib3
from urllib3 import Timeout
from urllib3.exceptions import HTTPError, MaxRetryError, ReadTimeoutError
from urllib3.util import Retry

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

HOST = os.getenv("T2T_HOST", "t2tmud.org")
PORT = int(os.getenv("T2T_PORT", "9999"))

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "69.142.141.135")
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
OLLAMA_CONNECT_TIMEOUT = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "10"))
OLLAMA_READ_TIMEOUT = float(os.getenv("OLLAMA_READ_TIMEOUT", "90"))
OLLAMA_MAX_ATTEMPTS = int(os.getenv("OLLAMA_MAX_ATTEMPTS", "3"))
OLLAMA_ENABLED = os.getenv("ENABLE_OLLAMA", "1").lower() not in {"0", "false", "no"}

COMMAND_DELAY_SECONDS = float(os.getenv("COMMAND_DELAY", "0.4"))
MAX_CONTEXT_CHARS = int(os.getenv("CONTEXT_LIMIT", "6000"))

COLOR_OUTPUT = os.getenv("ENABLE_COLOR", "1").lower() not in {"0", "false", "no"}

# ----------------------------------------------------------------------------
# ANSI Colour helpers
# ----------------------------------------------------------------------------

ANSI_RESET = "\033[0m"
ANSI_COLOURS = {
    "prompt": "\033[38;5;82m",
    "hint": "\033[38;5;220m",
    "help": "\033[38;5;39m",
    "more": "\033[38;5;213m",
    "error": "\033[38;5;196m",
    "info": "\033[38;5;111m",
}

COLOUR_RULES = (
    (re.compile(r"^HP:\s*\d+\s+EP:\s*\d+>"), "prompt"),
    (re.compile(r"^\*\*\* HINT \*\*\*"), "hint"),
    (re.compile(r"^Help for "), "help"),
    (re.compile(r"--More--"), "more"),
    (re.compile(r"^\[error]"), "error"),
    (re.compile(r"^\[info]"), "info"),
)


def supports_colour() -> bool:
    if not COLOR_OUTPUT:
        return False
    term = os.getenv("TERM", "")
    return sys.stdout.isatty() and term.lower() not in {"", "dumb"}


COLOUR_ENABLED = supports_colour()


def colourise(text: str) -> str:
    if not COLOUR_ENABLED:
        return text
    output_lines = []
    for line in text.splitlines(keepends=True):
        applied = line
        for pattern, colour in COLOUR_RULES:
            if pattern.search(line):
                applied = f"{ANSI_COLOURS[colour]}{line}{ANSI_RESET}"
                break
        output_lines.append(applied)
    return "".join(output_lines)


# ----------------------------------------------------------------------------
# Character profiles
# ----------------------------------------------------------------------------


@dataclass
class CharacterProfile:
    username: str
    password: str


CHARACTER_PROFILES: Sequence[CharacterProfile] = (
    CharacterProfile("Marchos", "hello123"),
    CharacterProfile("Zesty", "poopie"),
)

# ----------------------------------------------------------------------------
# Ollama integration
# ----------------------------------------------------------------------------


class OllamaController:
    def __init__(self, *, enabled: bool, host: str, port: int, model: str, context_limit: int):
        self.enabled = enabled
        self.host = host
        self.port = port
        self.model = model
        self.context_limit = max(2000, context_limit)
        self._history: Deque[str] = deque(maxlen=600)
        self._command_history: Deque[str] = deque(maxlen=120)
        self._lock = threading.Lock()
        self._http: Optional[urllib3.PoolManager] = None
        self._pending_request = threading.Event()
        self._response_queue: "queue.Queue[List[str]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        if self.enabled:
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        if not self.enabled:
            return
        self._stop_event.set()
        self._pending_request.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1.0)
        if self._http:
            self._http.clear()

    def reset(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._history.clear()
            self._command_history.clear()

    # ------------------------------------------------------------------
    # Recording information
    # ------------------------------------------------------------------

    def record_output(self, text: str) -> None:
        if not self.enabled:
            return
        cleaned = text.replace("\r", "")
        if not cleaned.strip():
            return
        with self._lock:
            for line in cleaned.splitlines(keepends=True):
                self._history.append(line)

    def record_command(self, command: str) -> None:
        if not self.enabled:
            return
        cleaned = command.strip()
        if not cleaned:
            return
        with self._lock:
            self._command_history.append(cleaned)
            self._history.append(f">>> {cleaned}\n")

    # ------------------------------------------------------------------
    # Command generation
    # ------------------------------------------------------------------

    def request_commands(self) -> Optional[List[str]]:
        if not self.enabled:
            return []
        self._pending_request.set()
        try:
            return self._response_queue.get(timeout=180)
        except queue.Empty:
            return []

    # Internal helpers -------------------------------------------------

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._pending_request.wait()
            if self._stop_event.is_set():
                break
            self._pending_request.clear()
            commands = self._generate_commands()
            self._response_queue.put(commands)

    def _generate_commands(self) -> List[str]:
        prompt = self._build_prompt()
        if not prompt:
            return []
        for attempt in range(1, OLLAMA_MAX_ATTEMPTS + 1):
            try:
                response = self._post_request(prompt)
            except ReadTimeoutError:
                sys.stdout.write(colourise("[error] Ollama read timeout, retrying...\n"))
                sys.stdout.flush()
                continue
            except (HTTPError, MaxRetryError, ConnectionError) as exc:  # type: ignore[arg-type]
                sys.stdout.write(colourise(f"[error] Ollama request failed: {exc}\n"))
                sys.stdout.flush()
                return []
            if not response:
                return []
            commands = self._parse_response(response)
            if commands:
                return commands
            if attempt < OLLAMA_MAX_ATTEMPTS:
                time.sleep(1.0)
        return []

    def _build_prompt(self) -> str:
        with self._lock:
            if not self._history:
                return ""
            history_text = "".join(self._history)
            command_tail = list(self._command_history)[-10:]
        if len(history_text) > self.context_limit:
            history_text = history_text[-self.context_limit :]
        instructions = (
            "You are remotely controlling a player in The Two Towers MUD via telnet. "
            "Review the most recent transcript between triple backticks and decide "
            "what to do next. Prefer meaningful interactions: move between rooms, "
            "inspect descriptions, talk to NPCs, read signs, seek quests, manage "
            "combat, and use help files when useful. Always return strict JSON of "
            "the shape {\"commands\": [<strings>], \"comment\": <optional string>} "
            "with between one and three commands. Do not include prose outside JSON."
        )
        metadata = "\n".join(f"- recent command: {cmd}" for cmd in command_tail)
        prompt = f"{instructions}\n\nRecent commands:\n{metadata}\n\nTranscript:\n```\n{history_text}\n```\n"
        return prompt

    def _post_request(self, prompt: str) -> str:
        if not self._http:
            timeout = Timeout(connect=OLLAMA_CONNECT_TIMEOUT, read=OLLAMA_READ_TIMEOUT)
            retries = Retry(
                total=OLLAMA_MAX_ATTEMPTS,
                connect=OLLAMA_MAX_ATTEMPTS,
                read=0,
                redirect=0,
                status=0,
                raise_on_status=False,
                backoff_factor=0.0,
            )
            self._http = urllib3.PoolManager(timeout=timeout, retries=retries)
        url = f"http://{self.host}:{self.port}/api/generate"
        body = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "keep_alive": 0,
                "options": {
                    "temperature": 0.6,
                    "num_predict": 96,
                },
            }
        ).encode("utf-8")
        response = self._http.request(
            "POST",
            url,
            body=body,
            headers={"Content-Type": "application/json"},
            preload_content=True,
        )
        try:
            data = response.data.decode("utf-8")
        finally:
            response.release_conn()
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return data
        return parsed.get("response", "") if isinstance(parsed, dict) else ""

    def _parse_response(self, text: str) -> List[str]:
        text = text.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        commands = payload.get("commands")
        if not isinstance(commands, list):
            return []
        result: List[str] = []
        for item in commands:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    result.append(cleaned)
        return result


# ----------------------------------------------------------------------------
# Telnet client
# ----------------------------------------------------------------------------


USERNAME_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"By what name do you wish to be known\??",
        r"Enter your character name:",
        r"Enter your name:",
        r"Your name\??",
        r"Please enter the name 'new' if you are new to The Two Towers\.",
    )
]

PASSWORD_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"What is your password\??",
        r"Password:",
        r"Enter your password:",
        r"Your name\?.*Password:",
    )
]

PROMPT_PATTERN = re.compile(r"HP:\s*\d+\s+EP:\s*\d+>")
MORE_PATTERN = re.compile(r"--More--")
RECONNECTED_PATTERN = re.compile(r"^Reconnected\.$", re.MULTILINE)


class TelnetController:
    def __init__(
        self,
        profiles: Sequence[CharacterProfile],
        controller: OllamaController,
    ):
        self.profiles = profiles
        self.controller = controller
        self.profile_index = 0
        self.connection: Optional[telnetlib.Telnet] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.output_queue: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.awaiting_commands = False
        self._buffer = ""
        self._current_profile: Optional[CharacterProfile] = None
        self._command_worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Telnet lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                profile = self.profiles[self.profile_index]
                self.profile_index = (self.profile_index + 1) % len(self.profiles)
                self._run_profile(profile)
                if self.stop_event.is_set():
                    break
                time.sleep(2.0)
        finally:
            self.controller.shutdown()

    def _run_profile(self, profile: CharacterProfile) -> None:
        self._current_profile = profile
        self.awaiting_commands = False
        self._buffer = ""
        self.controller.reset()
        sys.stdout.write(colourise(f"[info] Connecting as {profile.username}\n"))
        sys.stdout.flush()
        try:
            self.connection = telnetlib.Telnet(HOST, PORT, timeout=10)
        except OSError as exc:
            sys.stdout.write(colourise(f"[error] Connection failed: {exc}\n"))
            sys.stdout.flush()
            time.sleep(5.0)
            return
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        try:
            while self.connection and not self.stop_event.is_set():
                try:
                    chunk = self.output_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._handle_server_output(chunk)
        finally:
            if self.connection:
                try:
                    self.connection.close()
                except OSError:
                    pass
            self.connection = None
            if self.reader_thread and self.reader_thread.is_alive():
                self.reader_thread.join(timeout=0.5)
            self.reader_thread = None

    def stop(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------------
    # Reading and processing output
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        assert self.connection is not None
        telnet = self.connection
        while not self.stop_event.is_set():
            try:
                data = telnet.read_very_eager()
            except EOFError:
                break
            if data:
                try:
                    decoded = data.decode("utf-8", errors="ignore")
                except Exception:
                    decoded = data.decode("latin-1", errors="ignore")
                self.output_queue.put(decoded)
            else:
                time.sleep(0.1)
        self.output_queue.put("\nConnection closed.\n")

    def _handle_server_output(self, text: str) -> None:
        sys.stdout.write(colourise(text))
        sys.stdout.flush()
        self.controller.record_output(text)
        self._buffer += text
        self._buffer = self._buffer[-4000:]
        if self._current_profile:
            if any(pattern.search(self._buffer) for pattern in USERNAME_PATTERNS):
                self._send(self._current_profile.username)
                self._clear_buffer()
                return
            if any(pattern.search(self._buffer) for pattern in PASSWORD_PATTERNS):
                self._send(self._current_profile.password)
                self._clear_buffer()
                return
        if RECONNECTED_PATTERN.search(self._buffer):
            self.controller.reset()
        if PROMPT_PATTERN.search(self._buffer):
            self._clear_buffer()
            self._request_commands()
        elif MORE_PATTERN.search(self._buffer):
            # Allow Ollama to decide how to continue, but highlight state
            self._request_commands()

    def _clear_buffer(self) -> None:
        self._buffer = ""

    # ------------------------------------------------------------------
    # Sending commands
    # ------------------------------------------------------------------

    def _request_commands(self) -> None:
        if self.awaiting_commands or not self.connection:
            return
        self.awaiting_commands = True

        def worker() -> None:
            try:
                commands = self.controller.request_commands() or []
                for command in commands:
                    if not self.connection:
                        break
                    self._send(command)
                    time.sleep(COMMAND_DELAY_SECONDS)
            finally:
                self.awaiting_commands = False

        self._command_worker = threading.Thread(target=worker, daemon=True)
        self._command_worker.start()

    def _send(self, command: str) -> None:
        if not self.connection:
            return
        payload = f"{command}\n".encode("ascii", errors="ignore")
        try:
            self.connection.write(payload)
        except OSError:
            return
        self.controller.record_command(command)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def main() -> None:
    controller = OllamaController(
        enabled=OLLAMA_ENABLED,
        host=OLLAMA_HOST,
        port=OLLAMA_PORT,
        model=OLLAMA_MODEL,
        context_limit=MAX_CONTEXT_CHARS,
    )
    telnet_client = TelnetController(CHARACTER_PROFILES, controller)
    try:
        telnet_client.run()
    except KeyboardInterrupt:
        telnet_client.stop()
    finally:
        controller.shutdown()


if __name__ == "__main__":
    main()
