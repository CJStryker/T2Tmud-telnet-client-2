import sys
import telnetlib
import threading
import time
import re
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Tuple, Union

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
    "help map",
    "help hint",
    "help guilds",
    "help quests",
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


class T2TMUDClient:
    def __init__(self, h, p):
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

    def send(self, cmd):
        if not self.connection:
            return
        self.connection.write(f"{cmd}\n".encode('ascii'))
        self.log.append(('client', cmd.strip()))

    def close(self):
        if not self.connection:
            return
        try:
            self.send('quit')
        finally:
            try:
                self.connection.close()
            except OSError:
                pass
        self.log.append(('client', 'Connection closed.'))
        self.stop_automation()
        self.connection = None

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
            command = self._next_automation_command()
            if not self._automation_running.is_set():
                break
            if command:
                self.send(command)
            time.sleep(self.automation_delay)


def print_out(text, _):
    cleaned = text.replace('\r\n', '\n').replace('\r', '\n')
    print(cleaned, end='')


def configure_client(client, profile: CharacterProfile):
    client.profile = profile
    automation_commands = profile.automation_commands or DEFAULT_AUTOMATION_COMMANDS
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
            self.profile = profile

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

    scenario_state = {"started": False}

    def start_scenario():
        if scenario_state["started"]:
            return
        scenario_state["started"] = True
        login.reset()
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

    def handle_reconnect():
        scenario_state["started"] = False
        login.reset()
        world.last_exit_options = ()
        world.last_exit_choice_index = -1
        client.queue_script(["look", "exits", "hint"])
        client.start_automation()

    for prompt in USERNAME_PROMPTS:
        client.add_trigger(prompt, login.send_username, flags=re.IGNORECASE)
    for prompt in PASSWORD_PROMPTS:
        client.add_trigger(prompt, login.send_password, flags=re.IGNORECASE)
    for prompt in RECONNECT_PROMPTS:
        client.add_trigger(prompt, handle_reconnect, flags=re.IGNORECASE | re.MULTILINE)

    client.add_trigger(LOGIN_SUCCESS_PATTERN, start_scenario, flags=re.IGNORECASE)
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


def create_configured_client(profile: CharacterProfile):
    client = T2TMUDClient(HOST, PORT)
    configure_client(client, profile)
    client.connect(print_out)
    return client


def main():
    profile_index = 0
    current_profile = CHARACTER_PROFILES[profile_index]
    client = create_configured_client(current_profile)

    try:
        while True:
            cmd = input("")
            stripped = cmd.strip().lower()
            if stripped == "quit":
                client.close()
                client = create_configured_client(current_profile)
            elif stripped == "switch":
                client.close()
                profile_index = (profile_index + 1) % len(CHARACTER_PROFILES)
                current_profile = CHARACTER_PROFILES[profile_index]
                client = create_configured_client(current_profile)
            else:
                client.send(cmd)

    except (EOFError, KeyboardInterrupt):
        client.close()
        print("Disconnected.")

if __name__ == '__main__':
    main()
