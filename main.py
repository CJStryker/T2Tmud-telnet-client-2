import sys
import telnetlib
import threading
import time
import re
from typing import Callable, List, Optional, Tuple, Union

DEFAULT_AUTOMATION_COMMANDS = (
    "look",
    "north",
    "east",
    "south",
    "west",
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

TriggerAction = Union[str, Callable[[], None]]
Trigger = Tuple[re.Pattern[str], TriggerAction, bool]
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

                self.log.append(('server', data.replace('\r', '')))
                if self.output:
                    self.output(data, None)
                self.check_triggers(data)
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

    def add_trigger(self, pattern: str, action: TriggerAction, *, flags: int = 0, once: bool = False):
        compiled = re.compile(pattern, flags)
        self.triggers.append((compiled, action, once))

    def check_triggers(self, data):
        indices_to_remove = []
        for idx, (pattern, action, once) in enumerate(self.triggers):
            if pattern.search(data):
                if callable(action):
                    action()
                else:
                    self.send(action)
                if once:
                    indices_to_remove.append(idx)
        for idx in reversed(indices_to_remove):
            del self.triggers[idx]

    def set_automation(self, commands, delay):
        self.automation_commands = list(commands)
        self.automation_delay = delay

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

    def _automation_loop(self):
        while self._automation_running.is_set():
            for command in self.automation_commands:
                if not self._automation_running.is_set():
                    break
                self.send(command)
                time.sleep(self.automation_delay)

def print_out(text, _):
    print(text, end='')


def configure_client(client):
    client.set_automation(DEFAULT_AUTOMATION_COMMANDS, AUTOMATION_DELAY_SECONDS)
    sent_username = False
    sent_password = False

    def send_username():
        nonlocal sent_username
        if sent_username:
            return
        client.send(USERNAME)
        sent_username = True

    def send_password():
        nonlocal sent_password
        if sent_password:
            return
        client.send(PASSWORD)
        sent_password = True

    for prompt in USERNAME_PROMPTS:
        client.add_trigger(prompt, send_username, flags=re.IGNORECASE, once=True)
    for prompt in PASSWORD_PROMPTS:
        client.add_trigger(prompt, send_password, flags=re.IGNORECASE, once=True)
    client.add_trigger(LOGIN_SUCCESS_PATTERN, client.start_automation, flags=re.IGNORECASE, once=True)


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
