from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


@dataclass
class GameState:
    hp: int = 100
    energy: int = 100
    gold: int = 20
    xp: int = 0
    level: int = 1
    zone: str = "Greenfields"
    autoplay: bool = True
    tick: int = 0
    allow_manual_every: int = 4
    log: Deque[str] = field(default_factory=lambda: deque(maxlen=120))


GAME = GameState()
LOCK = threading.Lock()
AUTO_THREAD: threading.Thread | None = None
STOP_EVENT = threading.Event()

ZONES = [
    "Greenfields",
    "Whispering Woods",
    "Old Watchtower",
    "Moonlit Crossing",
    "Crystal Shore",
]


def add_log(message: str) -> None:
    GAME.log.appendleft(message)


def level_threshold(level: int) -> int:
    return 30 + (level - 1) * 20


def maybe_level_up() -> None:
    needed = level_threshold(GAME.level)
    while GAME.xp >= needed:
        GAME.xp -= needed
        GAME.level += 1
        GAME.hp = min(100 + (GAME.level - 1) * 5, GAME.hp + 18)
        GAME.energy = min(100, GAME.energy + 22)
        add_log(f"⭐ Level up! You are now level {GAME.level}.")
        needed = level_threshold(GAME.level)


def automated_step() -> None:
    GAME.tick += 1

    if GAME.hp <= 0:
        GAME.hp = 35
        GAME.energy = 40
        add_log("☠️ You were defeated. Autoplay revived you at camp.")
        return

    roll = random.random()
    if roll < 0.45:
        damage = random.randint(2, 10)
        reward = random.randint(4, 11)
        gain = random.randint(6, 14)
        GAME.hp -= damage
        GAME.energy = max(0, GAME.energy - random.randint(3, 9))
        GAME.gold += reward
        GAME.xp += gain
        add_log(f"⚔️ Autoplay fought a roaming beast: -{damage} HP, +{reward} gold, +{gain} XP.")
    elif roll < 0.7:
        rest = random.randint(6, 14)
        focus = random.randint(5, 12)
        GAME.hp = min(100 + (GAME.level - 1) * 5, GAME.hp + rest)
        GAME.energy = min(100, GAME.energy + focus)
        add_log(f"🛌 Autoplay rested by a fire: +{rest} HP, +{focus} energy.")
    elif roll < 0.9:
        found = random.randint(3, 16)
        GAME.gold += found
        add_log(f"🪙 Autoplay scavenged the route and found {found} gold.")
    else:
        GAME.zone = random.choice(ZONES)
        add_log(f"🧭 Autoplay moved you to {GAME.zone}.")

    if GAME.tick % 5 == 0:
        add_log("⌨️ Manual window open: your next command can influence autoplay.")

    maybe_level_up()


def apply_manual_command(raw_command: str) -> str:
    command = raw_command.strip().lower()
    if not command:
        return "Please enter a command."

    if GAME.tick % GAME.allow_manual_every != 0:
        wait = GAME.allow_manual_every - (GAME.tick % GAME.allow_manual_every)
        return f"Autoplay is busy. Try again in ~{wait} tick(s)."

    if command in {"rest", "camp"}:
        recovered = random.randint(12, 20)
        GAME.hp = min(100 + (GAME.level - 1) * 5, GAME.hp + recovered)
        GAME.energy = min(100, GAME.energy + 10)
        add_log(f"🧘 You took control briefly to rest: +{recovered} HP.")
        return "You rested successfully."

    if command in {"hunt", "attack", "fight"}:
        reward = random.randint(10, 20)
        gain = random.randint(8, 18)
        cost = random.randint(4, 10)
        GAME.gold += reward
        GAME.xp += gain
        GAME.hp -= cost
        add_log(f"🗡️ You overrode autoplay to hunt: -{cost} HP, +{reward} gold, +{gain} XP.")
        maybe_level_up()
        return "You forced a hunt action."

    if command in {"shop", "buy potion", "potion"}:
        if GAME.gold < 12:
            add_log("🏪 You tried to buy a potion, but lacked enough gold.")
            return "Not enough gold for a potion (12)."
        GAME.gold -= 12
        GAME.hp = min(100 + (GAME.level - 1) * 5, GAME.hp + 18)
        add_log("🧪 You purchased and drank a potion: -12 gold, +18 HP.")
        return "Potion purchased."

    if command in {"north", "south", "east", "west", "travel"}:
        GAME.zone = random.choice(ZONES)
        add_log(f"🚶 You redirected the journey to {GAME.zone}.")
        return f"Route changed to {GAME.zone}."

    if command in {"autoplay on", "auto on"}:
        GAME.autoplay = True
        add_log("▶️ You ensured autoplay remains enabled.")
        return "Autoplay enabled."

    if command in {"autoplay off", "auto off", "pause"}:
        GAME.autoplay = False
        add_log("⏸️ You paused autoplay.")
        return "Autoplay paused."

    if command in {"help", "?"}:
        return "Try: rest, hunt, shop, north/south/east/west, autoplay on/off."

    add_log(f"❔ Unknown command '{raw_command}'.")
    return "Unknown command. Type 'help' for options."


def automation_loop() -> None:
    while not STOP_EVENT.is_set():
        with LOCK:
            if GAME.autoplay:
                automated_step()
        time.sleep(2.0)


@app.before_request
def ensure_automation_running() -> None:
    start_automation()



@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/action")
def action():
    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", ""))
    with LOCK:
        message = apply_manual_command(command)
        snapshot = state_payload(message)
    return jsonify(snapshot)


@app.get("/api/toggle")
def toggle_autoplay():
    with LOCK:
        GAME.autoplay = not GAME.autoplay
        add_log(f"{'▶️ Resumed' if GAME.autoplay else '⏸️ Paused'} autoplay from the UI.")
        snapshot = state_payload("Autoplay toggled.")
    return jsonify(snapshot)


@app.get("/api/state")
def state():
    with LOCK:
        return jsonify(state_payload())


def state_payload(message: str = "") -> Dict[str, object]:
    return {
        "message": message,
        "stats": {
            "hp": GAME.hp,
            "energy": GAME.energy,
            "gold": GAME.gold,
            "xp": GAME.xp,
            "next_level_xp": level_threshold(GAME.level),
            "level": GAME.level,
            "zone": GAME.zone,
            "tick": GAME.tick,
            "autoplay": GAME.autoplay,
            "manual_window": GAME.tick % GAME.allow_manual_every == 0,
        },
        "log": list(GAME.log),
    }


def start_automation() -> None:
    global AUTO_THREAD
    if AUTO_THREAD and AUTO_THREAD.is_alive():
        return
    AUTO_THREAD = threading.Thread(target=automation_loop, daemon=True)
    AUTO_THREAD.start()


if __name__ == "__main__":
    with LOCK:
        add_log("🎮 AutoQuest booted. Autoplay is active and running.")
        add_log("⌨️ You can influence the run during manual windows every 4 ticks.")
    start_automation()
    app.run(host="0.0.0.0", port=3333, debug=True)
