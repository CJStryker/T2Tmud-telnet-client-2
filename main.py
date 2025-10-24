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
    "north",
    "look",
    "east",
    "look",
    "south",
    "look",
    "west",
    "look",
    "say Traveling onward.",
    "search",
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
USERNAME = "Marchos"
PASSWORD = "hello123"

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


def configure_client(client):
    client.set_automation(DEFAULT_AUTOMATION_COMMANDS, AUTOMATION_DELAY_SECONDS)

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
            client.send(USERNAME)

        def send_password(self):
            if not self._should_send(self.password_sent_at):
                return
            if self.username_sent_at == 0.0:
                self.username_sent_at = time.monotonic()
                client.send(USERNAME)
            client.send(PASSWORD)
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
            client.queue_script(["look map", "read map"])

        def ask_messenger(self, _match: Optional[re.Match[str]] = None):
            now = time.monotonic()
            if now - self.last_messenger < 45.0:
                return
            self.last_messenger = now
            client.queue_script(["ask messenger rumours"])

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

    login = LoginCoordinator()
    world = WorldInteractor()

    scenario_state = {"started": False}

    def start_scenario():
        if scenario_state["started"]:
            return
        scenario_state["started"] = True
        login.reset()
        client.queue_script(
            [
                "look",
                "say Greetings, everyone!",
                "read sign",
                "ask messenger rumours",
                "open east door",
                "east",
                "look",
                "search",
                "get all",
                "west",
                "say Does anyone need assistance?",
                "north",
                "look",
                "say I'm looking for adventure.",
                "west",
                "look",
                "southwest",
                "look",
            ]
        )
        client.start_automation()

    for prompt in USERNAME_PROMPTS:
        client.add_trigger(prompt, login.send_username, flags=re.IGNORECASE)
    for prompt in PASSWORD_PROMPTS:
        client.add_trigger(prompt, login.send_password, flags=re.IGNORECASE)

    client.add_trigger(LOGIN_SUCCESS_PATTERN, start_scenario, flags=re.IGNORECASE)
    client.add_trigger(
        r"Welcome to Arda,\s+%s!" % re.escape(USERNAME),
        start_scenario,
        flags=re.IGNORECASE,
    )
    client.add_trigger(
        r"Ragakh says in Westron: What is your name, young one\?",
        f"say {USERNAME}",
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


def create_configured_client():
    client = T2TMUDClient(HOST, PORT)
    configure_client(client)
    client.connect(print_out)
    return client


def main():
    client = create_configured_client()

    try:
        while True:
            cmd = input("")
            if cmd.strip() == "quit":
                client.close()  # Close the old connection
                client = create_configured_client()
            else:
                client.send(cmd)

    except (EOFError, KeyboardInterrupt):
        client.close()
        print("Disconnected.")

if __name__ == '__main__':
    main()
