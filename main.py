import json
import os
import re
import select
import socket
import sys
import telnetlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Iterable, List, Match, Optional, Sequence
import http.client

###############################################################################
# Environment configuration
###############################################################################

HOST = os.getenv("T2T_HOST", "t2tmud.org")
PORT = int(os.getenv("T2T_PORT", "9999"))

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "69.142.141.135")
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud")
OLLAMA_ENABLED = os.getenv("ENABLE_OLLAMA", "1").lower() not in {"0", "false", "no"}
OLLAMA_CONNECT_TIMEOUT = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "5.0"))
OLLAMA_READ_TIMEOUT = float(os.getenv("OLLAMA_READ_TIMEOUT", "90.0"))
OLLAMA_MAX_RETRIES = int(os.getenv("OLLAMA_MAX_RETRIES", "3"))

COLOR_OUTPUT = os.getenv("ENABLE_COLOR", "1").lower() not in {"0", "false", "no"}

###############################################################################
# Terminal rendering helpers
###############################################################################

ANSI_RESET = "\033[0m"
ANSI_COLORS = {
    "prompt": "\033[38;5;82m",
    "hint": "\033[38;5;220m",
    "help": "\033[38;5;39m",
    "more": "\033[38;5;213m",
    "event": "\033[38;5;208m",
    "ollama": "\033[38;5;207m",
    "error": "\033[38;5;196m",
}

PROMPT_PATTERN = re.compile(r"HP:\s*\d+\s+EP:\s*\d+>")
HINT_PATTERN = re.compile(r"^\*\*\* HINT \*\*\*")
HELP_HEADER_PATTERN = re.compile(r"^Help for ")
MORE_PATTERN = re.compile(r"--More--")
TRAVELTO_START_PATTERN = re.compile(r"Travelto:\s+Journey begun", re.IGNORECASE)
TRAVELTO_ABORT_PATTERN = re.compile(r"Travelto:\s+aborted", re.IGNORECASE)
TRAVELTO_RESUME_PATTERN = re.compile(r"Travelto:\s+resuming journey", re.IGNORECASE)
TRAVELTO_COMPLETE_PATTERN = re.compile(r"Travelto:\s+(?:Journey complete|arrived)", re.IGNORECASE)
NO_GOLD_PATTERN = re.compile(r"You don't have enough gold", re.IGNORECASE)
RENT_ROOM_REQUIRED_PATTERN = re.compile(r"You have not rented a room", re.IGNORECASE)
TRAVELTO_SIGNPOST_PATTERN = re.compile(r"Travelto can only be used at a signpost", re.IGNORECASE)
SEARCH_FAIL_PATTERN = re.compile(r"You search but fail to find anything of interest\.", re.IGNORECASE)
TARGET_LINE_PATTERN = re.compile(
    r"^\s*(?:An?|The)\s+(?P<name>[^\[]+?)\s*\[(?P<level>\d+)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)
GOLD_STATUS_PATTERN = re.compile(r"Gold:\s*(?P<amount>\d+)", re.IGNORECASE)
TARGET_MEMORY_SECONDS = 45.0


class TerminalDisplay:
    """Render telnet output while keeping prompts and metadata tidy."""

    def __init__(self, stream: Optional[Callable[[str], None]] = None):
        self._stream = stream or sys.stdout.write
        self._supports_color = self._detect_color_support()
        self._partial_line_pending = False
        self._lock = threading.Lock()

    def _detect_color_support(self) -> bool:
        if not COLOR_OUTPUT:
            return False
        term = os.getenv("TERM", "")
        return sys.stdout.isatty() and term.lower() not in {"", "dumb"}

    def _apply_color(self, line: str) -> str:
        if not self._supports_color:
            return line
        if PROMPT_PATTERN.search(line):
            color = ANSI_COLORS["prompt"]
        elif HINT_PATTERN.search(line):
            color = ANSI_COLORS["hint"]
        elif HELP_HEADER_PATTERN.search(line):
            color = ANSI_COLORS["help"]
        elif MORE_PATTERN.search(line):
            color = ANSI_COLORS["more"]
        elif line.startswith("[ollama]"):
            color = ANSI_COLORS["ollama"]
        elif line.startswith("[event]") or line.startswith("[input]"):
            color = ANSI_COLORS["event"]
        elif line.startswith("[error]"):
            color = ANSI_COLORS["error"]
        else:
            return line
        return f"{color}{line}{ANSI_RESET}"

    def feed(self, text: str):
        if not text:
            return
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        chunks = normalized.splitlines(keepends=True) or [normalized]
        with self._lock:
            for chunk in chunks:
                if not chunk:
                    continue
                if chunk.endswith("\n"):
                    line = chunk[:-1]
                    self._stream(self._apply_color(line))
                    self._stream("\n")
                    self._partial_line_pending = False
                else:
                    line = chunk
                    self._stream(self._apply_color(line))
                    if PROMPT_PATTERN.search(line):
                        self._stream("\n")
                        self._partial_line_pending = False
                    else:
                        self._partial_line_pending = True
            if normalized.endswith("\n"):
                self._partial_line_pending = False

    def emit(self, category: str, message: str):
        label = category.lower().strip()
        text = f"[{label}] {message}"
        if not text.endswith("\n"):
            text += "\n"
        self.feed(text)

    def ensure_newline(self):
        with self._lock:
            if self._partial_line_pending:
                self._stream("\n")
                self._partial_line_pending = False


###############################################################################
# Game knowledge shared with Ollama
###############################################################################


class GameKnowledge:
    """Provide a concise reference of useful commands and goals."""

    CORE_COMMANDS: Sequence[str] = (
        "look",
        "inventory",
        "score",
        "equipment",
        "skills",
        "who",
        "where",
        "weather",
        "time",
        "quests",
        "mission",
        "hint",
        "help commands",
        "help movement",
        "help combat",
        "help start",
        "help rules",
        "help list",
        "help newbie",
        "updates all",
        "news",
        "charinfo",
        "legendinfo",
        "map",
        "consider <target>",
        "travelto <destination>",
        "travelto resume",
        "exits",
        "search",
        "rest",
    )

    MOVEMENT_COMMANDS: Sequence[str] = (
        "north",
        "south",
        "east",
        "west",
        "northeast",
        "northwest",
        "southeast",
        "southwest",
        "up",
        "down",
        "enter",
        "leave",
        "go <direction>",
        "open <direction> door",
        "climb <object>",
        "travelto <destination>",
    )

    INTERACTION_COMMANDS: Sequence[str] = (
        "say <message>",
        "ask <npc> about <topic>",
        "read <object>",
        "read board",
        "read board all",
        "look board",
        "look <object>",
        "examine <object>",
        "get <item>",
        "get <item> from <container>",
        "drop <item>",
        "wear <item>",
        "wield <item>",
        "give <item> to <npc>",
        "give <amount> gold to <npc>",
        "ask <npc> about work",
        "ask <npc> about rumours",
        "ask <npc> about travel",
        "ask <npc> about jobs",
        "buy <item>",
        "sell <item>",
        "order <item>",
        "list",
        "value <item>",
        "hint",
        "help <topic>",
    )

    ECONOMY_COMMANDS: Sequence[str] = (
        "list",
        "value <item>",
        "buy <item>",
        "sell <item>",
        "order <item>",
        "rent room",
        "deposit <amount>",
        "withdraw <amount>",
        "get coins",
        "get all corpse",
        "get <item> from corpse",
        "give <amount> gold to <npc>",
        "offer",
        "pay <npc>",
        "sell <item>",
        "value <item>",
        "list",
    )

    COMBAT_COMMANDS: Sequence[str] = (
        "kill <target>",
        "flee",
        "consider <target>",
        "cast <spell>",
        "shield",
        "rescue <ally>",
        "get all corpse",
        "get coins",
    )

    STRATEGY_GUIDELINES: Sequence[str] = (
        "Always inspect rooms with 'look' and note available exits.",
        "Use 'hint' whenever progress seems unclear or a help prompt appears.",
        "Read message boards, signs, and help topics to gather objectives.",
        "Interact with NPCs using 'say' and 'ask <npc> about <topic>'.",
        "Follow signposts with 'travelto <destination>' and let the journey finish before issuing other commands; resume with 'travelto resume' if paused.",
        "Seek supplies in taverns: 'order <item>', 'rent room', and 'rest' recover resources faster when others are nearby.",
        "If gold is low, explore nearby streets, wilderness, or hunting grounds, search containers, loot corpses with 'get coins' or 'get all corpse', and sell excess gear before attempting large purchases.",
        "Before engaging, 'consider <target>' to judge difficulty, focus on low-level creatures or obvious foes, and be ready to 'flee' if health drops.",
        "After combat, loot coins and valuables, then visit shops or innkeepers to 'list', 'value', 'sell', or 'buy' needed items.",
        "Follow rumours, message boards, and NPC dialogue for hints about hunting grounds or profitable activities.",
        "When a shopkeeper or quest giver refuses to help, try other NPCs, different topics such as 'work', 'rumours', 'jobs', or explore outside to find creatures to hunt.",
        "If movement is blocked, pick another exit or resume 'travelto' journeys from the last signpost.",
        "Restock resources by resting, eating, or renting rooms when coins allow; gather more money before renting if refused.",
        "When help topics suggest more reading, queue follow-up 'help <topic>' calls.",
        "When '--More--' pagination appears, send a blank command to continue.",
        "Avoid repeating the same command rapidly if the game says you cannot do it.",
        "Do not log out or switch characters unless explicitly asked.",
        "Only send in-game commands; never respond with narrative text.",
    )

    SUPPORT_COMMANDS: Sequence[str] = (
        "rent room",
        "order <item>",
        "bribe <npc>",
        "comm on",
        "rest",
        "deposit <amount>",
        "withdraw <amount>",
        "give <amount> gold to <npc>",
        "travelto resume",
    )

    @classmethod
    def build_reference(cls) -> str:
        def fmt_section(title: str, entries: Iterable[str]) -> str:
            return f"{title}: " + ", ".join(entries)

        sections = [
            fmt_section("Core exploration", cls.CORE_COMMANDS),
            fmt_section("Movement", cls.MOVEMENT_COMMANDS),
            fmt_section("Interaction", cls.INTERACTION_COMMANDS),
            fmt_section("Economy", cls.ECONOMY_COMMANDS),
            fmt_section("Support", cls.SUPPORT_COMMANDS),
            fmt_section("Combat", cls.COMBAT_COMMANDS),
            "Guidelines: " + " ".join(cls.STRATEGY_GUIDELINES),
        ]
        return "\n".join(sections)


###############################################################################
# Ollama integration
###############################################################################


def _extract_json_fragment(text: str) -> Optional[str]:
    depth = 0
    start = None
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : index + 1]
    return None


class OllamaPlanner:
    """Collect transcript context and ask Ollama for next commands."""

    def __init__(
        self,
        *,
        send_callback: Callable[[str], None],
        knowledge_text: str,
        enabled: bool = True,
        max_context_chars: int = 6000,
        max_commands: int = 3,
    ):
        self._send_callback = send_callback
        self._knowledge_text = knowledge_text
        self.enabled = enabled
        self.max_context_chars = max_context_chars
        self.max_commands = max_commands
        self._transcript: Deque[str] = deque()
        self._commands: Deque[str] = deque(maxlen=40)
        self._manual_commands: Deque[str] = deque(maxlen=20)
        self._events: Deque[str] = deque(maxlen=20)
        self._lock = threading.Lock()
        self._pending_reason: Optional[str] = None
        self._request_event = threading.Event()
        self._active = False
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def shutdown(self):
        self._stop.set()
        self._request_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=1.0)

    def activate(self):
        if not self.enabled:
            return
        with self._lock:
            self._active = True
        self.request_commands("session start")

    def deactivate(self):
        with self._lock:
            self._active = False

    def reset(self):
        with self._lock:
            self._transcript.clear()
            self._commands.clear()
            self._manual_commands.clear()
            self._events.clear()
            self._pending_reason = None
        self._request_event.clear()

    def observe_output(self, text: str):
        if not (self.enabled and text):
            return
        cleaned = text.replace("\r", "")
        if not cleaned.strip():
            return
        with self._lock:
            self._append_transcript(cleaned)

    def record_command(self, command: str, source: str):
        if not self.enabled:
            return
        trimmed = command.strip()
        if not trimmed:
            return
        with self._lock:
            self._commands.append(f"{source}: {trimmed}")
            if source == "input":
                self._manual_commands.append(trimmed)
            self._append_transcript(f">>> {trimmed}\n")

    def note_event(self, message: str):
        if not self.enabled:
            return
        cleaned = message.strip()
        if not cleaned:
            return
        with self._lock:
            self._events.append(cleaned)
            self._append_transcript(f"[event] {cleaned}\n")

    def request_commands(self, reason: str):
        if not self.enabled:
            return
        with self._lock:
            if not self._active:
                return
            self._pending_reason = reason
        self._request_event.set()

    def _append_transcript(self, text: str):
        self._transcript.append(text)
        while True:
            total = sum(len(chunk) for chunk in self._transcript)
            if total <= self.max_context_chars:
                break
            self._transcript.popleft()

    def _worker_loop(self):
        while not self._stop.is_set():
            self._request_event.wait()
            if self._stop.is_set():
                break
            self._request_event.clear()
            payload = self._build_prompt()
            if not payload:
                continue
            response = self._query_ollama(payload)
            commands = self._extract_commands(response)
            if not commands:
                continue
            for command in commands:
                self._send_callback(command)
                time.sleep(0.35)

    def _build_prompt(self) -> Optional[str]:
        with self._lock:
            if not self._active:
                return None
            transcript = "".join(self._transcript)
            if not transcript.strip():
                return None
            recent_commands = list(self._commands)[-10:]
            manual = list(self._manual_commands)[-6:]
            events = list(self._events)[-8:]
            reason = self._pending_reason or ""
            self._pending_reason = None
        summary_lines = []
        if recent_commands:
            summary_lines.append("Recent commands: " + ", ".join(recent_commands))
        if manual:
            summary_lines.append("Player-entered commands: " + ", ".join(manual))
        if events:
            summary_lines.append("Notable events: " + "; ".join(events))
        if reason:
            summary_lines.append(f"Trigger: {reason}")
        summary = "\n".join(summary_lines)
        knowledge = self._knowledge_text
        prompt_parts = [
            "You are remotely controlling a character in The Two Towers (t2tmud.org) via telnet.",
            "Only output JSON with a 'commands' array (max three strings) and optional 'comment'.",
            "Do not include prose outside JSON.",
            "Game reference:",
            knowledge,
        ]
        if summary:
            prompt_parts.append("Context summary:\n" + summary)
        prompt_parts.append("Latest transcript:\n```\n" + transcript + "\n```")
        prompt_parts.append("Remember: respond with JSON only.")
        return "\n\n".join(prompt_parts)

    def _query_ollama(self, prompt: str) -> Optional[str]:
        if not self.enabled:
            return None
        body = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}).encode()
        headers = {"Content-Type": "application/json"}
        last_error: Optional[str] = None
        for attempt in range(1, OLLAMA_MAX_RETRIES + 2):
            try:
                connection = http.client.HTTPConnection(
                    OLLAMA_HOST,
                    OLLAMA_PORT,
                    timeout=OLLAMA_CONNECT_TIMEOUT,
                )
                connection.request("POST", "/api/generate", body=body, headers=headers)
                response = connection.getresponse()
                if connection.sock is not None:
                    connection.sock.settimeout(OLLAMA_READ_TIMEOUT)
                if response.status != 200:
                    last_error = f"HTTP {response.status}"
                    connection.close()
                    raise RuntimeError(last_error)
                payload = response.read()
                connection.close()
                return payload.decode("utf-8", errors="ignore")
            except socket.timeout:
                last_error = "timeout"
            except Exception as exc:  # pragma: no cover - network operations
                last_error = str(exc)
            time.sleep(1.0)
        if last_error:
            sys.stderr.write(f"[ollama] request failed: {last_error}\n")
        return None

    def _extract_commands(self, payload: Optional[str]) -> List[str]:
        if not payload:
            return []
        payload = payload.strip()
        if not payload:
            return []
        parsed: Optional[dict]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = None
            fragment = _extract_json_fragment(payload)
            if fragment:
                try:
                    parsed = json.loads(fragment)
                except json.JSONDecodeError:
                    parsed = None
        if isinstance(parsed, dict) and "response" in parsed and "commands" not in parsed:
            response_field = parsed.get("response", "")
            if isinstance(response_field, str):
                return self._extract_commands(response_field)
            return []
        if isinstance(parsed, dict):
            commands = parsed.get("commands")
            if isinstance(commands, list):
                result: List[str] = []
                for entry in commands:
                    if isinstance(entry, str):
                        cleaned = entry.strip()
                        if cleaned:
                            result.append(cleaned)
                    if len(result) >= self.max_commands:
                        break
                if result:
                    return result
        commands: List[str] = []
        for line in payload.splitlines():
            cleaned = line.strip().strip("#")
            if cleaned:
                commands.append(cleaned)
            if len(commands) >= self.max_commands:
                break
        return commands


###############################################################################
# Telnet session management
###############################################################################

USERNAME_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"By what name do you wish to be known\??",
        r"Enter your character name:",
        r"Enter your name:",
        r"Your name\??",
        r"Please enter the name 'new' if you are new to The Two Towers\.",
    )
]
PASSWORD_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"What is your password\??",
        r"Password:",
        r"Enter your password:",
        r"Your name\?.*Password:",
    )
]
MORE_PATTERN = re.compile(r"--More--")
PRESS_ENTER_PATTERN = re.compile(r"Press ENTER for next page", re.IGNORECASE)
DIRECTION_BLOCK_PATTERN = re.compile(r"You can't go that way!", re.IGNORECASE)
TARGET_MISSING_PATTERN = re.compile(r"You don't see that here\.", re.IGNORECASE)
TAKE_MISSING_PATTERN = re.compile(r"There is no [^\n]+ here to get\.", re.IGNORECASE)
STEALING_PATTERN = re.compile(r"That would be stealing!", re.IGNORECASE)
MULTI_TARGET_PATTERN = re.compile(r"You see more than one", re.IGNORECASE)
ASK_BLANK_PATTERN = re.compile(
    r"(?P<npc>[A-Z][\w' -]+) says in [^:]+: I don't know about that\.",
    re.IGNORECASE,
)
REST_START_PATTERN = re.compile(
    r"You sit back, relax, and enjoy a nice rest\.",
    re.IGNORECASE,
)
REST_INTERRUPT_PATTERN = re.compile(
    r"Your actions interrupt your rest\.",
    re.IGNORECASE,
)


@dataclass
class CharacterProfile:
    username: str
    password: str
    label: str


DEFAULT_PROFILES: Sequence[CharacterProfile] = (
    CharacterProfile("Marchos", "hello123", "Marchos"),
    CharacterProfile("Zesty", "poopie", "Zesty"),
)


class TelnetSession:
    def __init__(self, display: TerminalDisplay):
        self.display = display
        self.profile: Optional[CharacterProfile] = None
        self.connection: Optional[telnetlib.Telnet] = None
        self._listener: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._buffer = ""
        self._send_lock = threading.Lock()
        self._planner: Optional[OllamaPlanner] = None
        self._logged_in = False
        self._username_sent = False
        self._password_sent = False
        self._travel_active = False
        self._recent_targets: Dict[str, float] = {}
        self._last_gold_report: Optional[int] = None

    def attach_planner(self, planner: OllamaPlanner):
        self._planner = planner

    def connect(self, profile: CharacterProfile):
        self.profile = profile
        self._buffer = ""
        self._logged_in = False
        self._username_sent = False
        self._password_sent = False
        self._travel_active = False
        self._stop_event.clear()
        try:
            self.connection = telnetlib.Telnet(HOST, PORT)
        except OSError as exc:
            raise RuntimeError(f"Failed to connect: {exc}")
        self.display.emit("event", f"Connected to {HOST}:{PORT} as {profile.label}")
        if self._planner:
            self._planner.reset()
        self._listener = threading.Thread(target=self._listen_loop, daemon=True)
        self._listener.start()

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
        if self._planner:
            self._planner.deactivate()
        self.display.emit("event", "Connection closed")
        self._travel_active = False

    def send_command(self, command: str, *, source: str):
        if not self.connection:
            self.display.emit("error", "Cannot send command while disconnected")
            return
        payload = (command + "\n").encode("ascii", errors="ignore")
        with self._send_lock:
            self.connection.write(payload)
        if source == "ollama":
            self.display.emit("ollama", f">>> {command}")
        elif source == "input":
            self.display.emit("input", f">>> {command}")
        else:
            self.display.emit("event", f">>> {command}")
        if self._planner:
            self._planner.record_command(command, source)

    def send_blank(self):
        if not self.connection:
            return
        with self._send_lock:
            self.connection.write(b"\n")
        self.display.emit("event", ">>> (newline)")
        if self._planner:
            self._planner.record_command("", "system")

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
            self.display.feed(text)
            if self._planner:
                self._planner.observe_output(text)
            self._buffer += text
            if len(self._buffer) > 8192:
                self._buffer = self._buffer[-4096:]
            self._process_buffer()
        self._stop_event.set()
        self.connection = None

    def _process_buffer(self):
        profile = self.profile
        if profile is None:
            return
        if not self._username_sent:
            for pattern in USERNAME_PATTERNS:
                if pattern.search(self._buffer):
                    self.send_command(profile.username, source="system")
                    self._username_sent = True
                    self._consume(pattern)
                    return
        if not self._password_sent:
            for pattern in PASSWORD_PATTERNS:
                if pattern.search(self._buffer):
                    if not self._username_sent:
                        self.send_command(profile.username, source="system")
                        time.sleep(0.2)
                    self.send_command(profile.password, source="system")
                    self._password_sent = True
                    self._consume(pattern)
                    return
        if TRAVELTO_START_PATTERN.search(self._buffer):
            self._travel_active = True
            self.display.emit("event", "Travelto route engaged; awaiting arrival")
            if self._planner:
                self._planner.note_event("Travelto auto-travel engaged")
            self._consume(TRAVELTO_START_PATTERN)
            return
        if TRAVELTO_RESUME_PATTERN.search(self._buffer):
            self._travel_active = True
            self.display.emit("event", "Travelto route resumed")
            if self._planner:
                self._planner.note_event("Travelto route resumed")
            self._consume(TRAVELTO_RESUME_PATTERN)
            return
        if TRAVELTO_ABORT_PATTERN.search(self._buffer) or TRAVELTO_COMPLETE_PATTERN.search(self._buffer):
            self._travel_active = False
            self.display.emit("event", "Travelto route ended")
            if self._planner:
                self._planner.note_event("Travelto route ended")
                self._planner.request_commands("travelto ended")
            self._buffer = TRAVELTO_ABORT_PATTERN.sub("", self._buffer)
            self._buffer = TRAVELTO_COMPLETE_PATTERN.sub("", self._buffer)
            return
        match = NO_GOLD_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Purchase failed due to insufficient gold")
            if self._planner:
                self._planner.note_event("Not enough gold to buy; seek coins or items to sell")
                self._planner.request_commands("insufficient gold")
            self._remove_match(match)
            return
        match = RENT_ROOM_REQUIRED_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "A room rental is required before entering that area")
            if self._planner:
                self._planner.note_event("Need to rent a room before entering private quarters")
                self._planner.request_commands("rent room required")
            self._remove_match(match)
            return
        match = TRAVELTO_SIGNPOST_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Travelto can only be used at signposts; locate one first")
            if self._planner:
                self._planner.note_event("Travelto attempt failed away from signpost")
                self._planner.request_commands("travelto signpost needed")
            self._remove_match(match)
            return
        match = SEARCH_FAIL_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Search revealed nothing; try other rooms or targets")
            if self._planner:
                self._planner.note_event("Search failed; consider new area or target")
                self._planner.request_commands("search failed")
            self._remove_match(match)
            return
        match = DIRECTION_BLOCK_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Movement blocked; choose another direction or resume travelto")
            if self._planner:
                self._planner.note_event("Path blocked by obstacle")
                self._planner.request_commands("movement blocked")
            self._remove_match(match)
            return
        match = TARGET_MISSING_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Target not present; try examining the room or another NPC")
            if self._planner:
                self._planner.note_event("Attempted interaction failed; target missing")
                self._planner.request_commands("target missing")
            self._remove_match(match)
            return
        match = TAKE_MISSING_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "No such item to take here; search or hunt for loot")
            if self._planner:
                self._planner.note_event("Failed to pick up item")
                self._planner.request_commands("item missing")
            self._remove_match(match)
            return
        match = STEALING_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Stealing is not allowed here; find lawful ways to earn gold")
            if self._planner:
                self._planner.note_event("Stealing attempt blocked")
                self._planner.request_commands("stealing blocked")
            self._remove_match(match)
            return
        match = MULTI_TARGET_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Multiple targets found; specify which NPC or item to interact with")
            if self._planner:
                self._planner.note_event("Need to disambiguate multi-target selection")
                self._planner.request_commands("multiple targets")
            self._remove_match(match)
            return
        match = ASK_BLANK_PATTERN.search(self._buffer)
        if match:
            npc = "An NPC"
            if "npc" in match.groupdict():
                npc = match.group("npc").strip()
            self.display.emit("event", f"{npc} has no answer; try another topic or character")
            if self._planner:
                self._planner.note_event(f"{npc} offered no information")
                self._planner.request_commands("npc unhelpful")
            self._remove_match(match)
            return
        match = REST_START_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Resting to recover; monitor HP/EP before resuming hunts")
            if self._planner:
                self._planner.note_event("Rest started")
                self._planner.request_commands("resting")
            self._remove_match(match)
            return
        match = REST_INTERRUPT_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Rest interrupted; consider resuming or pursuing another action")
            if self._planner:
                self._planner.note_event("Rest interrupted")
                self._planner.request_commands("rest interrupted")
            self._remove_match(match)
            return
        enemy_match = TARGET_LINE_PATTERN.search(self._buffer)
        if enemy_match:
            raw_name = enemy_match.group("name").strip()
            level = enemy_match.group("level")
            normalized = re.sub(r"\s+", " ", raw_name).lower()
            now = time.monotonic()
            if now - self._recent_targets.get(normalized, 0.0) > TARGET_MEMORY_SECONDS:
                self._recent_targets[normalized] = now
                message = f"Potential foe spotted: {raw_name} (lvl {level})"
                self.display.emit("event", message)
                if self._planner:
                    self._planner.note_event(message)
                    self._planner.request_commands("enemy spotted")
            self._remove_match(enemy_match)
            return
        gold_matches = list(GOLD_STATUS_PATTERN.finditer(self._buffer))
        if gold_matches:
            last_match = gold_matches[-1]
            amount = int(last_match.group("amount"))
            if self._last_gold_report == amount:
                self._remove_match(last_match)
                return
            self._last_gold_report = amount
            if amount == 0:
                message = "Gold purse is empty; gather coins before shopping"
            else:
                message = f"Gold on hand: {amount}"
            self.display.emit("event", message)
            if self._planner:
                self._planner.note_event(message)
                if amount == 0:
                    self._planner.request_commands("gold depleted")
            self._remove_match(last_match)
            return
        if PROMPT_PATTERN.search(self._buffer):
            if self._travel_active:
                self._consume(PROMPT_PATTERN)
                return
            if not self._logged_in:
                self._logged_in = True
                self.display.emit("event", "Login successful; interactive prompt ready")
                if self._planner:
                    self._planner.activate()
                    self._planner.note_event("Reached status prompt")
            else:
                if self._planner:
                    self._planner.note_event("Status prompt available")
            if self._planner:
                self._planner.request_commands("prompt")
            self._consume(PROMPT_PATTERN)
            return
        if MORE_PATTERN.search(self._buffer) or PRESS_ENTER_PATTERN.search(self._buffer):
            self.display.emit("event", "Pagination prompt detected; sending newline")
            self.send_blank()
            if self._planner:
                self._planner.request_commands("pagination")
            self._buffer = ""

    def _consume(self, pattern: re.Pattern[str]):
        match = pattern.search(self._buffer)
        if not match:
            return
        self._buffer = self._buffer[match.end():]

    def _remove_match(self, match: Match[str]):
        self._buffer = self._buffer[: match.start()] + self._buffer[match.end():]


###############################################################################
# Console input handling
###############################################################################


class ConsoleInputThread(threading.Thread):
    def __init__(self, session: TelnetSession):
        super().__init__(daemon=True)
        self.session = session
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            except (KeyboardInterrupt, OSError, ValueError):
                break
            if self._stop.is_set():
                break
            if not ready:
                continue
            line = sys.stdin.readline()
            if line == "":
                break
            command = line.rstrip("\n")
            if command.strip().lower() in {":exit", ":quit"}:
                self.session.display.emit("event", "Local shutdown requested")
                self.session.disconnect()
                self._stop.set()
                break
            if command:
                self.session.send_command(command, source="input")
            else:
                self.session.send_blank()

    def stop(self):
        self._stop.set()


###############################################################################
# Application bootstrap
###############################################################################


def run_client():
    display = TerminalDisplay()
    knowledge_text = GameKnowledge.build_reference()
    session = TelnetSession(display)
    planner = OllamaPlanner(
        send_callback=lambda cmd: session.send_command(cmd, source="ollama"),
        knowledge_text=knowledge_text,
        enabled=OLLAMA_ENABLED,
    )
    session.attach_planner(planner)

    profile = DEFAULT_PROFILES[0]
    try:
        session.connect(profile)
    except RuntimeError as exc:
        display.emit("error", str(exc))
        planner.shutdown()
        return

    display.emit("event", "Type commands directly; use :exit to close locally.")
    if OLLAMA_ENABLED:
        display.emit("event", "Ollama automation is active and will respond after prompts.")
    else:
        display.emit("event", "Ollama automation is disabled via configuration.")

    input_thread = ConsoleInputThread(session)
    input_thread.start()

    try:
        while True:
            if not session._listener or not session._listener.is_alive():
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        display.emit("event", "Interrupted locally; closing session.")
    finally:
        input_thread.stop()
        input_thread.join(timeout=1.0)
        session.disconnect()
        planner.shutdown()
        display.ensure_newline()


if __name__ == "__main__":
    run_client()
