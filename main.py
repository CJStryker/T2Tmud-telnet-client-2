import json
import os
import sys
import telnetlib
import threading
import time
import re
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Tuple, Union

import urllib3

DEFAULT_AUTOMATION_COMMANDS = (
    "look",
    "inventory",
    "score",
    "equipment",
    "skills",
    "who",
    "where",
    "weather",
    "time",
    "news",
    "updates all",
    "exits",
    "map",
    "search",
    "hint",
    "help commands",
    "help movement",
    "help combat",
    "help start",
    "help list",
    "help rules",
    "help concepts",
    "help theme",
    "help help",
    "charinfo",
    "legendinfo",
    "quests",
    "mission",
    "hint random",
)
AUTOMATION_DELAY_SECONDS = 2.5
LOGIN_SUCCESS_PATTERN = r"HP:\s*\d+\s+EP:\s*\d+>"
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
RECONNECT_PROMPTS = (
    r"^Reconnected\.\s*$",
    r"^Connection restored\.\s*$",
    r"^Connection established\.\s*$",
)
HOST, PORT = 't2tmud.org', 9999

OLLAMA_HOST = os.getenv('OLLAMA_HOST', '69.142.141.135')
OLLAMA_PORT = int(os.getenv('OLLAMA_PORT', '11434'))
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3')
OLLAMA_ENABLED = os.getenv('ENABLE_OLLAMA', '1').lower() not in {'0', 'false', 'no'}

ENEMY_KEYWORDS = (
    "slug",
    "beetle",
    "rat",
    "wolf",
    "brigand",
    "bandit",
    "thief",
    "cutthroat",
    "goblin",
)

TALKATIVE_NPC_KEYWORDS = {
    "corsair": ("work", "rumours"),
    "messenger": ("rumours", "news"),
    "butler": ("overseer", "town"),
    "trainer": ("training", "lessons"),
    "ragakh": ("training", "camp"),
    "driver": ("travel", "help"),
}


@dataclass
class CharacterProfile:
    username: str
    password: str
    intro_script: Tuple[str, ...] = ()
    automation_commands: Optional[Tuple[str, ...]] = None


CHARACTER_PROFILES: Tuple[CharacterProfile, ...] = (
    CharacterProfile(
        username="Marchos",
        password="hello123",
        intro_script=(
            "who",
            "where",
            "weather",
            "equipment",
            "skills",
            "quests",
            "help newbie",
            "help commands",
            "help movement",
            "rumours",
            "look board",
            "read board",
            "updates all",
            "hint",
        ),
    ),
    CharacterProfile(
        username="Zesty",
        password="poopie",
        intro_script=(
            "who",
            "where",
            "equipment",
            "skills",
            "score",
            "map",
            "help hint",
            "help combat",
            "rumours",
            "hint",
        ),
    ),
)

BASE_SCENARIO_SCRIPT: Tuple[str, ...] = (
    "look",
    "say Greetings, everyone!",
    "hint",
    "help",
    "help newbie",
    "help commands",
    "help combat",
    "help survival",
    "help movement",
    "help start",
    "help help",
    "help rules",
    "help concepts",
    "help theme",
    "help list",
    "help map",
    "help hint",
    "help guilds",
    "help quests",
    "help faq",
    "faq",
    "score",
    "inventory",
    "equipment",
    "skills",
    "hint",
    "read sign",
    "look sign",
    "map",
    "read map",
    "ask messenger about rumours",
    "ask messenger about news",
    "ask messenger about jobs",
    "exits",
    "east",
    "look",
    "search",
    "get all",
    "look board",
    "read board",
    "look map",
    "read map",
    "help travel",
    "rumours",
    "news",
    "updates all",
    "weather",
    "where",
    "west",
    "say Does anyone need assistance?",
    "north",
    "look",
    "say I'm looking for adventure.",
    "ask messenger about rumours",
    "ask messenger about news",
    "ask corsair about rumours",
    "ask corsair about jobs",
    "help movement",
    "west",
    "look",
    "southwest",
    "look",
    "hint",
)

TriggerAction = Union[str, Callable[[], None], Callable[[re.Match[str]], None]]


@dataclass
class Trigger:
    pattern: re.Pattern[str]
    action: TriggerAction
    once: bool
    use_match: bool
OutputHandler = Callable[[str, Optional[str]], None]


def compose_output_handlers(*handlers: Optional[OutputHandler]) -> OutputHandler:
    valid_handlers = [handler for handler in handlers if handler]

    def _composed(text: str, meta: Optional[str]):
        for handler in valid_handlers:
            handler(text, meta)

    return _composed


class OllamaCommandController:
    def __init__(
        self,
        client: 'T2TMUDClient',
        *,
        host: str,
        port: int,
        model: str,
        enabled: bool = True,
        max_context_chars: int = 4000,
    ):
        self.client = client
        self.host = host
        self.port = port
        self.model = model
        self.enabled = enabled
        self.max_context_chars = max_context_chars
        self._history: Deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._http: Optional[urllib3.PoolManager] = None
        self._pending_request = threading.Event()
        self._stop_event = threading.Event()
        self._cooldown_until = 0.0
        self._worker: Optional[threading.Thread] = None
        if self.enabled:
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()

    def shutdown(self):
        if not self.enabled:
            return
        self._stop_event.set()
        self._pending_request.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=0.5)

    def reset_context(self):
        if not self.enabled:
            return
        with self._lock:
            self._history.clear()

    def handle_output(self, text: str, _meta: Optional[str]):
        if not self.enabled:
            return
        cleaned = text.replace('\r', '')
        if not cleaned.strip():
            return
        with self._lock:
            self._history.append(cleaned)

    def request_commands(self):
        if not self.enabled:
            return
        now = time.monotonic()
        if now < self._cooldown_until:
            return
        self._pending_request.set()

    def _worker_loop(self):
        while not self._stop_event.is_set():
            self._pending_request.wait()
            if self._stop_event.is_set():
                break
            self._pending_request.clear()
            commands = self._generate_commands()
            if commands:
                self.client.queue_script(commands)
                self.client.start_automation()
                self._cooldown_until = time.monotonic() + 1.5

    def _generate_commands(self) -> List[str]:
        context = self._collect_context()
        if not context:
            return []
        prompt = self._build_prompt(context)
        response_text = self._query_ollama(prompt)
        if not response_text:
            return []
        return self._extract_commands(response_text)

    def _collect_context(self) -> str:
        with self._lock:
            if not self._history:
                return ""
            joined = ''.join(self._history)
        if len(joined) > self.max_context_chars:
            return joined[-self.max_context_chars :]
        return joined

    def _build_prompt(self, context: str) -> str:
        guidance = (
            "You are controlling a player character connected via telnet to The Two "
            "Towers (t2tmud.org). Review the most recent game output delimited by "
            "triple backticks and decide what to do next. Focus on exploring rooms, "
            "reading descriptions, inspecting items, opening doors, asking NPCs "
            "questions, finding quests, and preparing for combat when it is "
            "sensible. When you respond, return JSON with a `commands` array of up "
            "to three sequential MUD commands you wish to issue next. Optionally "
            "include a `comment` field to briefly explain the plan. Do not include "
            "any other text outside the JSON."
        )
        return f"{guidance}\n\nRecent output:\n```\n{context}\n```\n\nRemember: respond with JSON only."

    def _query_ollama(self, prompt: str) -> str:
        try:
            if self._http is None:
                self._http = urllib3.PoolManager()
            url = f"http://{self.host}:{self.port}/api/generate"
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
            }
            encoded = json.dumps(payload).encode('utf-8')
            response = self._http.request(
                'POST',
                url,
                body=encoded,
                headers={'Content-Type': 'application/json'},
                timeout=urllib3.Timeout(connect=2.0, read=20.0),
            )
        except Exception as exc:  # pragma: no cover - network failure logging
            print(f"[ollama] request failed: {exc}", file=sys.stderr)
            return ""

        if response.status != 200:
            print(f"[ollama] HTTP {response.status}", file=sys.stderr)
            return ""

        try:
            data = json.loads(response.data.decode('utf-8'))
        except json.JSONDecodeError:
            print("[ollama] invalid JSON response", file=sys.stderr)
            return ""

        return data.get('response', '')

    def _extract_commands(self, text: str) -> List[str]:
        commands: List[str] = []
        stripped = text.strip()
        if not stripped:
            return commands
        parsed: Optional[dict] = None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            pass

        if isinstance(parsed, dict):
            raw_commands = parsed.get('commands')
            if isinstance(raw_commands, list):
                for entry in raw_commands:
                    if isinstance(entry, str):
                        cleaned = entry.strip()
                        if cleaned:
                            commands.append(cleaned)
        if commands:
            return commands[:3]

        for line in stripped.splitlines():
            cleaned = line.strip().strip('#').strip()
            if not cleaned:
                continue
            commands.append(cleaned)
            if len(commands) >= 3:
                break
        return commands

class T2TMUDClient:
    def __init__(self, h, p, *, on_disconnect: Optional[Callable[[bool], None]] = None):
        self.host = h
        self.port = p
        self.connection: Optional[telnetlib.Telnet] = None
        self.triggers: List[Trigger] = []
        self.log = []
        self.output: Optional[OutputHandler] = None
        self.automation_commands = list(DEFAULT_AUTOMATION_COMMANDS)
        self.automation_delay = AUTOMATION_DELAY_SECONDS
        self._automation_running = threading.Event()
        self._automation_thread = None
        self._trigger_buffer = ""
        self._automation_script: Deque[str] = deque()
        self._automation_cycle_index = 0
        self._automation_lock = threading.Lock()
        self.profile: Optional[CharacterProfile] = None
        self.on_disconnect = on_disconnect
        self._closing = False
        self._pause_condition: Optional[Callable[[], bool]] = None
        self.ollama_controller: Optional[OllamaCommandController] = None

    def connect(self, output: OutputHandler):
        self.output = output
        try:
            self.connection = telnetlib.Telnet(self.host, self.port)
        except ConnectionRefusedError:
            print(f"Error: could not connect to {self.host}:{self.port}")
            sys.exit()
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self):
        if not self.connection:
            print("Error: connection is not established")
            return

        try:
            while True:
                raw = self.connection.read_very_eager()
                if not raw:
                    time.sleep(0.05)
                    continue

                data = raw.decode('ascii', errors='ignore')
                if not data:
                    continue

                self._trigger_buffer += data
                if len(self._trigger_buffer) > 8192:
                    self._trigger_buffer = self._trigger_buffer[-4096:]

                self.log.append(('server', data.replace('\r', '')))
                if self.output:
                    self.output(data, None)
                self.check_triggers()
        except EOFError:
            self.log.append(('error', 'Connection closed.'))
            if self.output:
                self.output("Connection closed.\n", None)
        except OSError:
            self.log.append(('error', 'Connection closed.'))
            if self.output:
                self.output("Connection closed.\n", None)
        finally:
            self.stop_automation()
            self.connection = None
            on_disconnect = self.on_disconnect
            closing = self._closing
            self._closing = False
        if on_disconnect:
            on_disconnect(not closing)

    def send(self, cmd):
        if not self.connection:
            return
        self.connection.write(f"{cmd}\n".encode('ascii'))
        self.log.append(('client', cmd.strip()))

    def close(self, send_quit: bool = True):
        if not self.connection:
            if self.ollama_controller:
                self.ollama_controller.shutdown()
            return
        try:
            self._closing = True
            if send_quit:
                self.send('quit')
        finally:
            try:
                self.connection.close()
            except OSError:
                pass
        self.log.append(('client', 'Connection closed.'))
        self.stop_automation()
        self.connection = None
        if self.ollama_controller:
            self.ollama_controller.shutdown()

    def add_trigger(
        self,
        pattern: str,
        action: TriggerAction,
        *,
        flags: int = 0,
        once: bool = False,
        use_match: bool = False,
    ):
        compiled = re.compile(pattern, flags)
        self.triggers.append(Trigger(compiled, action, once, use_match))

    def check_triggers(self):
        indices_to_remove = []
        triggered = False
        for idx, trigger in enumerate(self.triggers):
            match = trigger.pattern.search(self._trigger_buffer)
            if match:
                action = trigger.action
                if callable(action):
                    if trigger.use_match:
                        action(match)  # type: ignore[arg-type]
                    else:
                        action()  # type: ignore[call-arg]
                else:
                    self.send(action)
                triggered = True
                if trigger.once:
                    indices_to_remove.append(idx)
        for idx in reversed(indices_to_remove):
            del self.triggers[idx]
        if triggered:
            self._trigger_buffer = ""

    def set_automation(self, commands, delay):
        with self._automation_lock:
            self.automation_commands = list(commands)
            self._automation_cycle_index = 0
            self.automation_delay = delay

    def set_pause_condition(self, func: Optional[Callable[[], bool]]):
        self._pause_condition = func

    def queue_script(self, commands):
        with self._automation_lock:
            for command in commands:
                if command:
                    self._automation_script.append(command)

    def start_automation(self):
        if self._automation_running.is_set() or not self.automation_commands:
            return
        self._automation_running.set()
        self._automation_thread = threading.Thread(target=self._automation_loop, daemon=True)
        self._automation_thread.start()

    def stop_automation(self):
        if not self._automation_running.is_set():
            return
        self._automation_running.clear()
        if (
            self._automation_thread
            and self._automation_thread.is_alive()
            and threading.current_thread() is not self._automation_thread
        ):
            self._automation_thread.join(timeout=0.1)

    def _next_automation_command(self) -> Optional[str]:
        with self._automation_lock:
            if self._automation_script:
                return self._automation_script.popleft()
            if not self.automation_commands:
                return None
            command = self.automation_commands[self._automation_cycle_index]
            if self.automation_commands:
                self._automation_cycle_index = (
                    self._automation_cycle_index + 1
                ) % len(self.automation_commands)
            return command

    def _automation_loop(self):
        while self._automation_running.is_set():
            if self._pause_condition and self._pause_condition():
                time.sleep(0.25)
                continue
            command = self._next_automation_command()
            if not self._automation_running.is_set():
                break
            if command:
                self.send(command)
            time.sleep(self.automation_delay)

def print_out(text, _):
    cleaned = text.replace('\r\n', '\n').replace('\r', '\n')
    print(cleaned, end='')


def configure_client(
    client,
    profile: CharacterProfile,
    ollama_controller: Optional[OllamaCommandController] = None,
):
    client.profile = profile
    automation_commands = profile.automation_commands or DEFAULT_AUTOMATION_COMMANDS
    if ollama_controller and ollama_controller.enabled:
        automation_commands = ("",)
    client.set_automation(automation_commands, AUTOMATION_DELAY_SECONDS)

    class LoginCoordinator:
        def __init__(self):
            self.username_sent_at = 0.0
            self.password_sent_at = 0.0

        def _should_send(self, last_sent_at: float) -> bool:
            now = time.monotonic()
            return now - last_sent_at >= 1.0

        def send_username(self):
            if not self._should_send(self.username_sent_at):
                return
            self.username_sent_at = time.monotonic()
            client.send(profile.username)

        def send_password(self):
            if not self._should_send(self.password_sent_at):
                return
            if self.username_sent_at == 0.0:
                self.username_sent_at = time.monotonic()
                client.send(profile.username)
            client.send(profile.password)
            self.password_sent_at = time.monotonic()

        def reset(self):
            self.username_sent_at = 0.0
            self.password_sent_at = 0.0

    class WorldInteractor:
        def __init__(self):
            self.profile = profile
            self.ollama = ollama_controller
            self._reset_state()

        def _reset_state(self):
            now = time.monotonic()
            self.last_greeting: Dict[str, float] = {}
            self.last_sign_seen = 0.0
            self.last_map_check = 0.0
            self.last_messenger = 0.0
            self.last_enemy: Dict[str, float] = {}
            self.last_exit_options: Tuple[str, ...] = ()
            self.last_exit_choice_index = -1
            self.blocked_attempts = 0
            self.unknown_command_count = 0
            self.last_help_request = 0.0
            self.last_item_inspect: Dict[str, float] = {}
            self.last_npc_interaction: Dict[str, float] = {}
            self.last_board_check = 0.0
            self.last_hint_follow_up = 0.0
            self.last_hint_text = ""
            self.last_hint_seen = 0.0
            self.last_search_fail = 0.0
            self.last_empty_take = 0.0
            self.last_helpful_prompt = 0.0
            self.awaiting_more = False
            self.last_more_prompt = 0.0
            self.help_topics_read = 0
            self.help_topics_seen: Dict[str, float] = {}
            self.session_started_at = now
            self.quit_scheduled = False
            self.last_quit_check = 0.0
            self.last_not_found = 0.0
            self.no_help_topic_recent = 0.0

        def reset_for_new_session(self):
            self._reset_state()
            if self.ollama:
                self.ollama.reset_context()

        def greet(self, match: re.Match[str]):
            speaker = match.group('speaker').strip()
            key = speaker.lower()
            now = time.monotonic()
            if now - self.last_greeting.get(key, 0.0) < 25.0:
                return
            self.last_greeting[key] = now
            client.queue_script([f"say Greetings, {speaker}!"])

        def open_door(self, match: re.Match[str]):
            direction = match.group('direction').lower()
            client.queue_script([f"open {direction} door", direction, "look"])

        def read_sign(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.last_sign_seen < 20.0:
                return
            self.last_sign_seen = now
            client.queue_script(["read sign"])

        def inspect_map(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.last_map_check < 30.0:
                return
            self.last_map_check = now
            client.queue_script(
                [
                    "look sign",
                    "read sign",
                    "map",
                    "look map",
                    "read map",
                ]
            )

        def ask_messenger(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.last_messenger < 45.0:
                return
            self.last_messenger = now
            client.queue_script(["ask messenger about rumours"])

        def inspect_item(self, match: re.Match[str]):
            item = match.group('item').strip()
            normalized = re.sub(r"\s*\[[^\]]+\]\s*$", "", item).strip().lower()
            if not normalized or any(keyword in normalized for keyword in ("door", "exit", "obvious")):
                return
            if any(keyword in normalized for keyword in ENEMY_KEYWORDS):
                return
            now = time.monotonic()
            if now - self.last_item_inspect.get(normalized, 0.0) < 30.0:
                return
            self.last_item_inspect[normalized] = now

            base_item = re.sub(r"\s*\[[^\]]+\]\s*$", "", item).strip()
            raw_tokens = [token for token in re.split(r"\s+", base_item) if token]
            filtered_tokens = [
                token for token in raw_tokens if token.lower() not in {"a", "an", "the"}
            ]
            target_tokens = filtered_tokens or raw_tokens
            target = target_tokens[-1].lower() if target_tokens else normalized

            commands: List[str] = []
            if 'sign' in normalized or 'map' in normalized:
                commands.extend(["look sign", "read sign", "map", "look map"])
            else:
                commands.extend([f"look {target}", f"examine {target}"])

            for keyword, topics in TALKATIVE_NPC_KEYWORDS.items():
                if keyword in normalized:
                    if time.monotonic() - self.last_npc_interaction.get(keyword, 0.0) < 40.0:
                        break
                    self.last_npc_interaction[keyword] = time.monotonic()
                    name_token = target
                    greeting_name = target_tokens[0] if target_tokens else keyword
                    commands.append(f"say Greetings, {greeting_name}!")
                    for topic in topics:
                        commands.append(f"ask {name_token} about {topic}")
                    break

            if commands:
                client.queue_script(commands)

        def consider_enemy(self, match: re.Match[str]):
            creature = match.group('creature').strip()
            normalized = creature.lower()
            if not any(keyword in normalized for keyword in ENEMY_KEYWORDS):
                return
            now = time.monotonic()
            if now - self.last_enemy.get(normalized, 0.0) < 15.0:
                return
            self.last_enemy[normalized] = now
            target = normalized.split()[-1]
            target = target.replace("'", "")
            client.queue_script([f"kill {target}"])

        def _request_help(self, topic: str):
            now = time.monotonic()
            if now - self.last_help_request < 45.0:
                return
            self.last_help_request = now
            client.queue_script([f"help {topic}", "hint"])

        def handle_hint_line(self, match: re.Match[str]):
            hint_text = match.group('hint').strip()
            if not hint_text:
                return
            normalized = re.sub(r"\s+", " ", hint_text.lower())
            now = time.monotonic()
            if normalized == self.last_hint_text and now - self.last_hint_seen < 20.0:
                return
            self.last_hint_text = normalized
            self.last_hint_seen = now

            commands: List[str] = []
            for topic in re.findall(r"help\s+([a-z]+)", normalized):
                commands.append(f"help {topic}")
            if "map" in normalized:
                commands.extend(["map", "look map", "read map"])
            if "ask" in normalized and "about" in normalized:
                commands.append("hint")
            if "talk" in normalized or "speak" in normalized:
                commands.append("say How can I help?")
            if not commands:
                commands.append("hint")
            else:
                commands.append("hint")
            client.queue_script(commands)
            if self.ollama:
                self.ollama.request_commands()

        def handle_unknown_command(self, _match: Optional[re.Match[str]] = None):
            self.unknown_command_count += 1
            if self.unknown_command_count == 1:
                client.queue_script(["hint"])
            elif self.unknown_command_count >= 2:
                self._request_help("commands")

        def handle_blocked_path(self, _match: Optional[re.Match[str]] = None):
            self.blocked_attempts += 1
            self.queue_next_exit(alternate=True)
            if self.blocked_attempts >= 2:
                self._request_help("movement")

        def handle_ask_prompt(self, _match: Optional[re.Match[str]] = None):
            self._request_help("ask")

        def respond_to_help_me(self, match: re.Match[str]):
            npc = match.group('npc').strip()
            now = time.monotonic()
            key = npc.lower()
            if now - self.last_helpful_prompt < 15.0 and self.last_hint_text:
                return
            if now - self.last_npc_interaction.get(key, 0.0) < 20.0:
                return
            self.last_helpful_prompt = now
            self.last_npc_interaction[key] = now
            name_token = re.split(r"\s+", npc)[0].lower()
            client.queue_script(
                [
                    f"say How can I help you, {npc}?",
                    f"ask {name_token} about help",
                    f"ask {name_token} about work",
                ]
            )

        def handle_board(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.last_board_check < 30.0:
                return
            self.last_board_check = now
            client.queue_script(["look board", "read board", "read board all"])

        def handle_shop_direction(self, match: re.Match[str]):
            direction = match.group('direction').lower()
            client.queue_script(self._commands_for_direction(direction))

        def handle_help_suggestion(self, match: re.Match[str]):
            topic = match.group('topic').strip().lower()
            now = time.monotonic()
            if now - self.last_hint_follow_up < 20.0:
                return
            self.last_hint_follow_up = now
            client.queue_script([f"help {topic}", "hint"])

        def handle_wake_up(self, _match: Optional[re.Match[str]] = None):
            client.queue_script(["look", "inventory", "score"])

        def handle_travel_advice(self, match: re.Match[str]):
            npc = match.group('npc').strip().lower()
            topic = re.split(r"\s+", npc)[0]
            client.queue_script([f"help {topic}", f"where {topic}", "hint"])

        def thank_driver(self, _match: Optional[re.Match[str]] = None):
            client.queue_script(["say Thank you, driver.", "wave driver"])

        def handle_empty_search(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.last_search_fail < 20.0:
                return
            self.last_search_fail = now
            self.queue_next_exit(alternate=True)
            client.queue_script(["look", "hint"])

        def handle_empty_take(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.last_empty_take < 20.0:
                return
            self.last_empty_take = now
            client.queue_script(["inventory", "search", "hint"])

        def handle_not_found(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.last_not_found < 20.0:
                return
            self.last_not_found = now
            self.queue_next_exit(alternate=True)
            client.queue_script(["look", "hint"])

        def handle_no_help_topic(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.no_help_topic_recent < 20.0:
                return
            self.no_help_topic_recent = now
            client.queue_script(["help list", "hint"])

        def handle_status_prompt(self, _match: Optional[re.Match[str]] = None):
            self.awaiting_more = False
            self.schedule_quit_if_ready()
            if self.ollama and not self.awaiting_more:
                self.ollama.request_commands()

        def handle_help_header(self, match: re.Match[str]):
            self.awaiting_more = True
            self.last_more_prompt = time.monotonic()
            self.help_topics_read += 1
            self.schedule_quit_if_ready()
            if self.ollama:
                self.ollama.request_commands()

        def handle_help_bullet(self, match: re.Match[str]):
            topic = match.group('topic').strip()
            topic = re.sub(r"\s*-.*$", "", topic).strip()
            if not topic:
                return
            normalized = re.sub(r"\s+", " ", topic.lower())
            now = time.monotonic()
            if now - self.help_topics_seen.get(normalized, 0.0) < 60.0:
                return
            self.help_topics_seen[normalized] = now
            client.queue_script([f"help {topic.lower()}"])
            if self.ollama:
                self.ollama.request_commands()

        def handle_more_prompt(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            self.awaiting_more = True
            previous = self.last_more_prompt
            self.last_more_prompt = now
            if now - previous > 0.35:
                client.send("")

        def handle_more_error(self, _match: Optional[re.Match[str]] = None):
            self.awaiting_more = True
            self.last_more_prompt = time.monotonic()
            client.send("")

        def handle_press_enter_prompt(self, _match: Optional[re.Match[str]] = None):
            self.awaiting_more = True
            self.last_more_prompt = time.monotonic()
            client.send("")

        def should_pause_automation(self) -> bool:
            if not self.awaiting_more:
                return False
            if time.monotonic() - self.last_more_prompt > 10.0:
                self.awaiting_more = False
                return False
            return True

        def schedule_quit_if_ready(self):
            if self.quit_scheduled or self.awaiting_more:
                return
            now = time.monotonic()
            if now - self.session_started_at < 60.0 and self.help_topics_read < 5:
                return
            if now - self.last_quit_check < 5.0:
                return
            self.last_quit_check = now
            self.quit_scheduled = True
            client.queue_script(
                [
                    "say I should check on another path.",
                    "hint",
                    "save",
                    "quit",
                ]
            )

        def queue_next_exit(self, alternate: bool = False):
            if not self.last_exit_options:
                return
            if alternate:
                if len(self.last_exit_options) <= 1:
                    return
                next_index = (self.last_exit_choice_index + 1) % len(
                    self.last_exit_options
                )
            else:
                next_index = self.last_exit_choice_index
            if next_index == -1:
                next_index = 0
            self.last_exit_choice_index = next_index
            direction = self.last_exit_options[self.last_exit_choice_index]
            client.queue_script(self._commands_for_direction(direction))

        def handle_exits(self, match: re.Match[str]):
            exits_text = match.group('exits')
            cleaned = exits_text.replace(' and ', ', ')
            options = [
                option.strip().lower()
                for option in re.split(r",|/", cleaned)
                if option.strip()
            ]
            if not options:
                return
            self.unknown_command_count = 0
            self.blocked_attempts = 0
            normalized_options = tuple(options)
            if normalized_options == self.last_exit_options:
                next_index = (self.last_exit_choice_index + 1) % len(options)
            else:
                next_index = 0
            self.last_exit_options = normalized_options
            self.last_exit_choice_index = next_index
            direction = options[next_index]
            client.queue_script(self._commands_for_direction(direction))

        def _commands_for_direction(self, direction: str) -> List[str]:
            commands: List[str] = []
            if direction in {"up", "down"}:
                commands.append(f"go {direction}")
            elif direction in {"stairs", "ladder"}:
                commands.append(f"climb {direction}")
            commands.append(direction)
            commands.append("look")
            return commands

    login = LoginCoordinator()
    world = WorldInteractor()

    client.set_pause_condition(world.should_pause_automation)

    scenario_state = {"started": False}

    def start_scenario():
        if scenario_state["started"]:
            return
        scenario_state["started"] = True
        login.reset()
        world.reset_for_new_session()
        script: List[str] = list(BASE_SCENARIO_SCRIPT)
        script.extend(profile.intro_script)
        deduped: List[str] = []
        seen: set[str] = set()
        for command in script:
            normalized = command.strip().lower()
            if not normalized:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(command)
        client.queue_script(deduped)
        client.start_automation()
        if ollama_controller:
            ollama_controller.request_commands()

    def handle_reconnect():
        scenario_state["started"] = False
        login.reset()
        world.reset_for_new_session()
        client.queue_script(["look", "exits", "hint"])
        if ollama_controller:
            ollama_controller.request_commands()
        client.start_automation()

    for prompt in USERNAME_PROMPTS:
        client.add_trigger(prompt, login.send_username, flags=re.IGNORECASE)
    for prompt in PASSWORD_PROMPTS:
        client.add_trigger(prompt, login.send_password, flags=re.IGNORECASE)
    for prompt in RECONNECT_PROMPTS:
        client.add_trigger(prompt, handle_reconnect, flags=re.IGNORECASE | re.MULTILINE)

    client.add_trigger(LOGIN_SUCCESS_PATTERN, start_scenario, flags=re.IGNORECASE)
    client.add_trigger(LOGIN_SUCCESS_PATTERN, world.handle_status_prompt, flags=re.IGNORECASE)
    client.add_trigger(
        r"Welcome to Arda,\s+%s!" % re.escape(profile.username),
        start_scenario,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"Ragakh says in Westron: What is your name, young one\?",
        f"say {profile.username}",
        flags=re.IGNORECASE,
        once=True,
    )
    client.add_trigger(
        r"(?P<speaker>[A-Z][\w' -]+) says in Westron:",
        world.greet,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"The (?P<direction>north|south|east|west|northeast|northwest|southeast|southwest) door is closed\.",
        world.open_door,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"A sign with the map of Azrakadar",
        world.inspect_map,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"(?m)^\s*A sign\b",
        world.read_sign,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"A lean orc messenger",
        world.ask_messenger,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"(?m)^\s*A (?P<creature>[^\[]+?)(?:\s*\[[0-9]+\])?\s*$",
        world.consider_enemy,
        flags=re.IGNORECASE | re.MULTILINE,
        use_match=True,
    )
    client.add_trigger(
        r"(?m)^\s*An (?P<creature>[^\[]+?)(?:\s*\[[0-9]+\])?\s*$",
        world.consider_enemy,
        flags=re.IGNORECASE | re.MULTILINE,
        use_match=True,
    )
    client.add_trigger(
        r"(?m)^\s*(?:An|A|The) (?P<item>[^\[]+?)(?:\s*\[[0-9]+\])?\s*$",
        world.inspect_item,
        flags=re.IGNORECASE | re.MULTILINE,
        use_match=True,
    )
    client.add_trigger(
        r"The only obvious exits are (?P<exits>[^.]+)\.",
        world.handle_exits,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"Obvious exits: (?P<exits>[^\r\n]+)",
        world.handle_exits,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(r"\bWhat\?\s*$", world.handle_unknown_command, flags=re.MULTILINE)
    client.add_trigger(
        r"You can't go that way!",
        world.handle_blocked_path,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"You don't see that here\.",
        world.handle_not_found,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"Ask <who> about <what>\?",
        world.handle_ask_prompt,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"Hint:\s*(?P<hint>.+)",
        world.handle_hint_line,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"Tip:\s*(?P<hint>.+)",
        world.handle_hint_line,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"(?i)notice ?board",
        world.handle_board,
    )
    client.add_trigger(
        r"There seems to be a shop to the (?P<direction>north|south|east|west)",
        world.handle_shop_direction,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r'Type "help (?P<topic>[^"]+)"',
        world.handle_help_suggestion,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"(?P<npc>[A-Z][\w' -]+) says in Westron: Hey you!\s*Maybe you can help me\?",
        world.respond_to_help_me,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"You regain consciousness\.",
        world.handle_wake_up,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"you should see (?P<npc>[^.]+) after you've grown up a bit",
        world.handle_travel_advice,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"The driver turns to you",
        world.thank_driver,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"You search but fail to find anything of interest\.",
        world.handle_empty_search,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"There is nothing here to get\.",
        world.handle_empty_take,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"There is no help available on that topic\.",
        world.handle_no_help_topic,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"--More--",
        world.handle_more_prompt,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"Press ENTER for next page",
        world.handle_press_enter_prompt,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"Unrecognized \"More\" command",
        world.handle_more_error,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"Help for (?P<topic>[^\n]+)",
        world.handle_help_header,
        flags=re.IGNORECASE,
        use_match=True,
    )
    client.add_trigger(
        r"(?m)^\s*\*\s*help\s+(?P<topic>[a-z][\w' -]*)",
        world.handle_help_bullet,
        flags=re.IGNORECASE,
        use_match=True,
    )


class SessionManager:
    def __init__(self):
        self.profile_index = 0
        self.client: Optional[T2TMUDClient] = None
        self.lock = threading.Lock()
        self.pending_action: Optional[str] = None
        self.shutdown_requested = False

    def start(self):
        self._connect_current()

    def _connect_current(self):
        profile = CHARACTER_PROFILES[self.profile_index]
        print(f"Connecting as {profile.username}...", flush=True)
        client = create_configured_client(profile, on_disconnect=self._handle_disconnect)
        with self.lock:
            self.client = client

    def _handle_disconnect(self, by_server: bool):
        with self.lock:
            if self.shutdown_requested:
                return
            action = self.pending_action
            self.pending_action = None
            rotate = False
            if action == "shutdown":
                self.shutdown_requested = True
                return
            if action == "switch":
                rotate = True
            elif action == "reconnect":
                rotate = False
            elif by_server:
                rotate = True
            if rotate:
                self.profile_index = (self.profile_index + 1) % len(CHARACTER_PROFILES)
        if self.shutdown_requested:
            return
        time.sleep(1.0)
        self._connect_current()

    def request_switch(self):
        with self.lock:
            client = self.client
            if not client:
                return
            self.pending_action = "switch"
        client.close()

    def request_reconnect(self):
        with self.lock:
            client = self.client
            if not client:
                return
            self.pending_action = "reconnect"
        client.close()

    def send(self, command: str):
        with self.lock:
            client = self.client
        if client:
            client.send(command)

    def shutdown(self):
        with self.lock:
            client = self.client
            self.client = None
            self.pending_action = "shutdown"
            self.shutdown_requested = True
        if client:
            client.close()


def create_configured_client(
    profile: CharacterProfile, *, on_disconnect: Optional[Callable[[bool], None]] = None
):
    client = T2TMUDClient(HOST, PORT, on_disconnect=on_disconnect)
    ollama_controller: Optional[OllamaCommandController] = None
    if OLLAMA_ENABLED:
        ollama_controller = OllamaCommandController(
            client,
            host=OLLAMA_HOST,
            port=OLLAMA_PORT,
            model=OLLAMA_MODEL,
            enabled=OLLAMA_ENABLED,
        )
        client.ollama_controller = ollama_controller
    configure_client(client, profile, ollama_controller=ollama_controller)
    output_handler = compose_output_handlers(
        print_out,
        ollama_controller.handle_output if ollama_controller else None,
    )
    client.connect(output_handler)
    return client


def main():
    manager = SessionManager()
    manager.start()

    try:
        while True:
            cmd = input("")
            stripped = cmd.strip().lower()
            if stripped == "switch":
                manager.request_switch()
            elif stripped in {"reconnect", "reset"}:
                manager.request_reconnect()
            elif stripped in {"shutdown", "exit"}:
                manager.shutdown()
                break
            else:
                manager.send(cmd)
    except (EOFError, KeyboardInterrupt):
        manager.shutdown()
        print("Disconnected.")
    else:
        print("Disconnected.")

if __name__ == '__main__':
    main()
