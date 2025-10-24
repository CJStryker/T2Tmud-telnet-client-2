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
from typing import Callable, Deque, Dict, Iterable, List, Match, Optional, Sequence, Tuple
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
PROMPT_STATUS_PATTERN = re.compile(r"HP:\s*(?P<hp>\d+)\s+EP:\s*(?P<ep>\d+)>")
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
SEARCH_SUCCESS_PATTERN = re.compile(
    r"You (?:find|discover|uncover|notice) (?P<discovery>[^.]+)\.",
    re.IGNORECASE,
)
TARGET_LINE_PATTERN = re.compile(
    r"^\s*(?:An?|The)\s+(?P<name>[^\[]+?)\s*\[(?P<level>\d+)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)
GOLD_STATUS_PATTERN = re.compile(r"Gold:\s*(?P<amount>\d+)", re.IGNORECASE)
INVENTORY_HEADER_PATTERN = re.compile(
    r"Gold:\s*(?P<gold>\d+)\s+Encumbrance:\s*(?P<encumbrance>[A-Za-z ]+)",
    re.IGNORECASE,
)
INVENTORY_EMPTY_PATTERN = re.compile(r"You are not carrying any items right now\.", re.IGNORECASE)
INVENTORY_LIST_PATTERN = re.compile(r"You are carrying the following on your person:", re.IGNORECASE)
MENU_HINT_PATTERN = re.compile(r"Try reading the menu\.", re.IGNORECASE)
BOARD_HELP_PATTERN = re.compile(r"Type 'help board'", re.IGNORECASE)
BLANK_RESPONSE_PATTERN = re.compile(r"looks at you blankly\.", re.IGNORECASE)
NO_STOCK_PATTERN = re.compile(r"does not have any of that\.", re.IGNORECASE)
NO_QUESTS_PATTERN = re.compile(r"No quests done yet\.", re.IGNORECASE)
NEWS_ALERT_PATTERN = re.compile(r"News in Arda!", re.IGNORECASE)
UPDATES_ALERT_PATTERN = re.compile(r"There are many new updates", re.IGNORECASE)
EMAIL_REMINDER_PATTERN = re.compile(r"You have not yet set your email address\.", re.IGNORECASE)
LOCATION_AT_PATTERN = re.compile(r"You are currently at (?P<location>[^.]+)\.", re.IGNORECASE)
ROOM_NAME_PATTERNS: Sequence[re.Pattern[str]] = (
    re.compile(r"This is the (?P<room>[^.]+)\.", re.IGNORECASE),
    re.compile(r"This room is (?P<room>[^.]+)\.", re.IGNORECASE),
    re.compile(r"This area is (?P<room>[^.]+)\.", re.IGNORECASE),
    re.compile(r"Welcome to (?P<room>[^.]+)\.", re.IGNORECASE),
)
EXITS_PATTERN = re.compile(r"The only obvious exits are (?P<exits>[^.]+)\.", re.IGNORECASE)
ALT_EXITS_PATTERN = re.compile(r"Obvious exits: (?P<exits>[^\r\n]+)", re.IGNORECASE)
STANDARD_EXITS_PATTERN = re.compile(
    r"Standard exits:(?P<block>(?:\n\s*[A-Za-z]+:[^\n]+)+)",
    re.IGNORECASE,
)
SKY_PATTERN = re.compile(r"The sky is (?P<sky>[^.]+)\.", re.IGNORECASE)
TIME_OF_DAY_PATTERN = re.compile(r"The sun (?:shines|has set|is) (?P<detail>[^.]+)\.", re.IGNORECASE)
PATH_CONTINUES_PATTERN = re.compile(r"The path continues (?P<directions>[^.]+)\.", re.IGNORECASE)
CITY_NEAR_PATTERN = re.compile(r"The City of (?P<city>[^.]+) is (?P<direction>[^.]+)\.", re.IGNORECASE)
FORGE_PATH_PATTERN = re.compile(r"It appears to lead to a forge", re.IGNORECASE)
CLOSED_DOOR_PATTERN = re.compile(r"The (?P<direction>[a-z]+) door is closed\.", re.IGNORECASE)
CORPSE_MISSING_PATTERN = re.compile(r"There is not a single corpse here to get\.", re.IGNORECASE)
NO_EFFECT_PATTERN = re.compile(r"You hit [^\n]+ but to no effect\.", re.IGNORECASE)
LOW_DAMAGE_PATTERN = re.compile(r"You scratch [^\n]+\.", re.IGNORECASE)
BUSY_ATTACK_PATTERN = re.compile(r"You are too busy to make an attack!", re.IGNORECASE)
ITEM_BELONGS_PATTERN = re.compile(r"belongs to the", re.IGNORECASE)
WIELD_WHAT_PATTERN = re.compile(r"Wield what\?", re.IGNORECASE)
CONSIDER_PATTERN = re.compile(
    r"(?P<target>[A-Z][\w' -]+) is (?P<assessment>[^.]+)\.",
    re.IGNORECASE,
)
SUGGEST_ACTION_PATTERN = re.compile(r"Perhaps it could be (?P<action>[a-z]+)", re.IGNORECASE)
MAP_SUGGESTION_PATTERN = re.compile(r"use the 'map' command", re.IGNORECASE)
HINT_SUGGESTION_PATTERN = re.compile(r"\*\*\* HINT \*\*\*\s*:\s*(?P<hint>.+)")
EXPERIENCE_GAIN_PATTERN = re.compile(r"You gain (?P<xp>\d+) experience", re.IGNORECASE)
COIN_GAIN_PATTERN = re.compile(
    r"You (?:get|receive) (?P<amount>\d+) gold(?: coins?)?",
    re.IGNORECASE,
)
ITEM_ACQUIRE_PATTERN = re.compile(
    r"You (?:get|take|obtain) (?P<item>[^.]+)\.",
    re.IGNORECASE,
)
LEVEL_UP_PATTERN = re.compile(
    r"You have advanced to level (?P<level>\d+)",
    re.IGNORECASE,
)
SKILL_IMPROVE_PATTERN = re.compile(
    r"Your (?:skill|ability) in (?P<skill>[^ ]+) (?:has )?improved",
    re.IGNORECASE,
)
REST_COMPLETE_PATTERN = re.compile(r"You feel rested", re.IGNORECASE)
DOOR_OPENED_PATTERN = re.compile(r"You open the (?P<direction>[a-z]+) door", re.IGNORECASE)
DOOR_LOCKED_PATTERN = re.compile(r"The (?P<direction>[a-z]+) door is locked", re.IGNORECASE)
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
        "help economy",
        "help hunting",
        "help guilds",
        "help banking",
        "updates all",
        "news",
        "read news",
        "charinfo",
        "legendinfo",
        "map",
        "exits",
        "consider <target>",
        "journal",
        "notes",
        "search",
        "rest",
        "missions",
        "practice",
        "train",
        "save",
        "quit",
    )

    TRAVEL_COMMANDS: Sequence[str] = (
        "travelto <destination>",
        "travelto resume",
        "map",
        "exits",
        "where",
        "follow road",
        "follow path",
        "listen",
        "scan",
        "track <target>",
        "search trail",
        "sneak <direction>",
        "camp",
        "light torch",
        "go <direction>",
        "climb <object>",
        "enter",
        "leave",
        "board <transport>",
        "disembark",
        "unlock <direction> door",
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
        "out",
        "go <direction>",
        "open <direction> door",
        "lift <object>",
        "push <object>",
        "pull <object>",
        "follow <path>",
        "sneak <direction>",
        "run <direction>",
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
        "read sign",
        "get <item>",
        "get <item> from <container>",
        "drop <item>",
        "wear <item>",
        "wield <item>",
        "equip <item>",
        "remove <item>",
        "give <item> to <npc>",
        "give <amount> gold to <npc>",
        "buy <item>",
        "sell <item>",
        "order <item>",
        "list",
        "value <item>",
        "appraise <item>",
        "lift shelf",
        "open door",
        "unlock <object>",
        "search <container>",
        "listen",
        "hint",
        "help <topic>",
        "updates all",
        "study <object>",
        "smell",
        "taste",
        "knock <door>",
        "observe <object>",
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
        "balance",
        "get coins",
        "get all corpse",
        "get <item> from corpse",
        "give <amount> gold to <npc>",
        "offer",
        "pay <npc>",
        "sell loot",
        "sell all corpse",
        "appraise <item>",
        "auction <item>",
        "collect bounty",
        "donate <amount>",
        "exchange <item>",
        "buy room",
        "tip <npc>",
        "haggle",
        "barter <item>",
        "pawn <item>",
        "collect reward",
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
        "wield <weapon>",
        "wear <armor>",
        "ready <item>",
        "skin <corpse>",
        "butcher <corpse>",
        "bash <target>",
        "kick <target>",
        "parry",
        "disarm <target>",
        "retreat",
        "target <enemy>",
        "assist <ally>",
        "charge <target>",
        "feint <target>",
        "shield bash",
        "guard <ally>",
        "bandage <ally>",
    )

    QUEST_COMMANDS: Sequence[str] = (
        "quests",
        "mission",
        "journal",
        "help quests",
        "ask <npc> about quest",
        "ask <npc> about job",
        "ask <npc> about work",
        "news",
        "read news",
        "updates all",
        "journal",
        "notes",
    )

    HUNTING_COMMANDS: Sequence[str] = (
        "consider <target>",
        "track <target>",
        "scan",
        "listen",
        "search",
        "get all corpse",
        "loot corpse",
        "skin <corpse>",
        "butcher <corpse>",
        "sell <loot>",
        "flee",
        "follow tracks",
        "ambush <target>",
        "hide",
        "sneak",
        "set trap",
        "stalk <target>",
        "aim",
        "track scent",
        "collect pelt",
    )

    SUPPORT_COMMANDS: Sequence[str] = (
        "rent room",
        "order <item>",
        "bribe <npc>",
        "comm on",
        "rest",
        "deposit <amount>",
        "withdraw <amount>",
        "balance",
        "give <amount> gold to <npc>",
        "travelto resume",
        "save",
        "quit",
        "comm off",
        "camp",
        "light torch",
        "fill waterskin",
        "drink <item>",
        "eat <item>",
        "prepare <item>",
        "memorize",
        "practice",
        "group",
        "assist <ally>",
    )

    SOCIAL_COMMANDS: Sequence[str] = (
        "wave",
        "smile",
        "bow",
        "thank <npc>",
        "apologize",
        "greet <npc>",
        "introduce <name>",
        "cheer",
        "laugh",
        "comm on",
        "comm off",
        "reply <message>",
        "emote <message>",
        "rp <message>",
        "sing",
        "dance",
    )

    CRAFTING_COMMANDS: Sequence[str] = (
        "forge",
        "smith",
        "repair <item>",
        "sharpen <weapon>",
        "polish <armor>",
        "enchant <item>",
        "cook <item>",
        "brew <potion>",
        "mix <ingredient>",
        "scribe <scroll>",
        "study recipe",
        "gather <resource>",
        "mine <ore>",
        "smelt <ore>",
        "tan <hide>",
        "sew <item>",
        "assemble <object>",
    )

    UTILITY_COMMANDS: Sequence[str] = (
        "diagnose",
        "consider <target>",
        "report",
        "score",
        "charinfo",
        "legendinfo",
        "config",
        "brief",
        "verbose",
        "colour on",
        "colour off",
        "channels",
        "alias",
        "unalias",
        "ignore <player>",
        "ping",
    )

    WILDERNESS_COMMANDS: Sequence[str] = (
        "forage",
        "gather herbs",
        "skin <corpse>",
        "butcher <corpse>",
        "set snare",
        "light campfire",
        "extinguish fire",
        "fish",
        "cast net",
        "track <target>",
        "hide",
        "listen",
        "scan",
        "scent",
        "survey",
    )

    TOWN_SERVICES: Sequence[str] = (
        "bank",
        "deposit <amount>",
        "withdraw <amount>",
        "balance",
        "rent room",
        "order <item>",
        "buy <item>",
        "sell <item>",
        "list",
        "value <item>",
        "appraise <item>",
        "heal",
        "pray",
        "train",
        "practice",
        "mission",
        "quests",
        "join <guild>",
        "stable horse",
    )

    PROGRESSION_CHECKS: Sequence[str] = (
        "score",
        "quests",
        "mission",
        "legendinfo",
        "charinfo",
        "skills",
        "train",
        "practice",
        "guilds",
        "journal",
        "notes",
        "killboard",
        "achievements",
    )

    PREPARATION_TIPS: Sequence[str] = (
        "Eat or drink in inns to recover faster before long hunts.",
        "Carry spare weapons and armor and equip upgrades immediately.",
        "Check message boards and news for job leads or bounties.",
        "Bank excess gold to avoid losing coins on death.",
        "Rent rooms when you have spare gold to unlock private rest areas.",
        "Stock up on food, drink, and torches before long expeditions.",
        "Keep rope, lockpicks, and light sources for dungeons and caves.",
        "Purchase bags or packs to increase carrying capacity before looting sprees.",
        "Carry healing draughts or bandages if you plan to tackle tougher foes.",
        "Study help files for each profession to unlock unique abilities.",
        "Maintain a list of profitable hunting spots and rotate between them.",
        "Store quest-critical items safely in the bank until needed.",
    )

    SAFETY_REMINDERS: Sequence[str] = (
        "Check HP and EP before every combat engagement.",
        "Abort travelto if HP drops dangerously low during the journey.",
        "Carry a light source in dark zones to avoid stumbling into hazards.",
        "Avoid stealing from NPCs to prevent guard retaliation.",
        "Retreat from creatures that your attacks cannot harm.",
        "Rest after long hunts so energy regenerates before the next fight.",
        "Deposit gold frequently to safeguard progress after successful hunts.",
        "Keep antidotes or cure poison potions for swamp regions.",
        "Use 'consider <target>' before starting fights with unknown foes.",
        "Head back to town when stamina or supplies run low.",
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
        "Only send in-game commands; never respond with narrative text.",
        "Record gold income and expenses; if funds drop to zero, hunt or sell items before attempting purchases.",
        "When NPCs refuse to help, leave the building and explore nearby paths for alternate opportunities.",
        "Follow time-of-day or weather cues to decide when to rest, travel, or hunt.",
        "Check 'quests' or 'mission' regularly to track objectives after completing tasks.",
        "Return to towns to resupply when inventory is empty or equipment is damaged.",
        "Use 'travelto' to reach fresh hunting grounds when current areas are exhausted.",
        "Loot corpses promptly to secure coins before they vanish.",
        "Visit banks to deposit earnings once you have more than a handful of coins.",
        "If rest is interrupted, find safer rooms or finish combat encounters before trying again.",
        "Read stable and travel signs to learn additional transportation commands.",
        "Use 'map' in wilderness areas to orient yourself toward nearby towns or paths.",
        "Lift, push, or pull suspicious objects when descriptions hint at hidden caches.",
        "Rotate through multiple exits if searches keep failing to avoid wasting energy.",
        "Prioritize foes that drop coins or sellable loot before attempting costly purchases.",
        "When damage is ineffective, retreat, re-equip stronger weapons, or target a weaker enemy.",
        "Keep track of encumbrance; deposit or sell items before it becomes burdensome.",
        "If automation stalls in one room, choose a new exit or resume 'travelto' to reach another hub.",
        "Look for interactive verbs like lift, press, or open in room descriptions and try them explicitly.",
        "Read forge or workshop signs for crafting opportunities and order materials before experimenting.",
        "When hints reference specific NPCs, go find them immediately before forgetting their names.",
        "Rest to recover energy before re-engaging in extended hunts or travelto journeys.",
        "Record helpful help topics and revisit them if similar issues arise later.",
        "Check 'legendinfo' and 'charinfo' for long-term goals or progress markers.",
    )

    @classmethod
    def build_reference(cls) -> str:
        def fmt_section(title: str, entries: Iterable[str]) -> str:
            return f"{title}: " + ", ".join(entries)

        sections = [
            fmt_section("Core exploration", cls.CORE_COMMANDS),
            fmt_section("Travel", cls.TRAVEL_COMMANDS),
            fmt_section("Movement", cls.MOVEMENT_COMMANDS),
            fmt_section("Interaction", cls.INTERACTION_COMMANDS),
            fmt_section("Utility", cls.UTILITY_COMMANDS),
            fmt_section("Economy", cls.ECONOMY_COMMANDS),
            fmt_section("Town services", cls.TOWN_SERVICES),
            fmt_section("Support", cls.SUPPORT_COMMANDS),
            fmt_section("Social", cls.SOCIAL_COMMANDS),
            fmt_section("Combat", cls.COMBAT_COMMANDS),
            fmt_section("Hunting", cls.HUNTING_COMMANDS),
            fmt_section("Wilderness", cls.WILDERNESS_COMMANDS),
            fmt_section("Crafting", cls.CRAFTING_COMMANDS),
            fmt_section("Quests", cls.QUEST_COMMANDS),
            fmt_section("Progress tracking", cls.PROGRESSION_CHECKS),
            "Preparation: " + ", ".join(cls.PREPARATION_TIPS),
            "Safety: " + ", ".join(cls.SAFETY_REMINDERS),
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
        self._issues: Deque[str] = deque(maxlen=20)
        self._opportunities: Deque[str] = deque(maxlen=12)
        self._lock = threading.Lock()
        self._pending_reason: Optional[str] = None
        self._request_event = threading.Event()
        self._active = False
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self._issue_counts: Dict[str, int] = {}
        self._last_hp: Optional[int] = None
        self._last_ep: Optional[int] = None
        self._last_gold: Optional[int] = None
        self._location: Optional[str] = None
        self._exits: Optional[str] = None
        self._environment: Optional[str] = None
        self._travel_state: Optional[str] = None
        self._rest_state: Optional[str] = None
        self._encumbrance: Optional[str] = None
        self._inventory_state: Optional[str] = None
        self._location_history: Deque[str] = deque(maxlen=16)
        self._location_streak: int = 0
        self._stagnation_flag: Optional[str] = None

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
            self._issues.clear()
            self._opportunities.clear()
            self._pending_reason = None
            self._issue_counts.clear()
            self._last_hp = None
            self._last_ep = None
            self._last_gold = None
            self._location = None
            self._exits = None
            self._environment = None
            self._travel_state = None
            self._rest_state = None
            self._encumbrance = None
            self._inventory_state = None
            self._location_history.clear()
            self._location_streak = 0
            self._stagnation_flag = None
        self._request_event.clear()

    def observe_output(self, text: str):
        if not (self.enabled and text):
            return
        cleaned = text.replace("\r", "")
        if not cleaned.strip():
            return
        with self._lock:
            self._append_transcript(cleaned)

    def update_vitals(self, hp: int, ep: int):
        if not self.enabled:
            return
        with self._lock:
            if self._last_hp == hp and self._last_ep == ep:
                return
            self._last_hp = hp
            self._last_ep = ep
            self._append_transcript(f"[status] HP:{hp} EP:{ep}\n")
            if hp <= 20:
                self._issues.append("Health is critically low; rest or heal")
            if ep <= 15:
                self._issues.append("Energy is running low; consider resting")

    def update_gold(self, amount: int):
        if not self.enabled:
            return
        with self._lock:
            if self._last_gold == amount:
                return
            self._last_gold = amount
            self._append_transcript(f"[status] Gold:{amount}\n")
            if amount == 0:
                self._issues.append("No gold available for purchases")

    def update_location(self, location: str):
        if not self.enabled:
            return
        cleaned = location.strip()
        if not cleaned:
            return
        with self._lock:
            if self._location == cleaned:
                self._location_streak += 1
                if self._location_streak <= 3:
                    self._append_transcript(f"[location-repeat] {cleaned} x{self._location_streak}\n")
            else:
                self._location = cleaned
                self._location_streak = 1
                self._stagnation_flag = None
                self._append_transcript(f"[location] {cleaned}\n")
            self._location_history.append(cleaned)

    def update_exits(self, exits: str):
        if not self.enabled:
            return
        cleaned = exits.strip()
        if not cleaned:
            return
        with self._lock:
            if self._exits == cleaned:
                return
            self._exits = cleaned
            self._append_transcript(f"[exits] {cleaned}\n")

    def update_environment(self, description: str):
        if not self.enabled:
            return
        cleaned = description.strip()
        if not cleaned:
            return
        with self._lock:
            if self._environment == cleaned:
                return
            self._environment = cleaned
            self._append_transcript(f"[environment] {cleaned}\n")

    def update_travel_state(self, state: str):
        if not self.enabled:
            return
        cleaned = state.strip()
        if not cleaned:
            return
        with self._lock:
            if self._travel_state == cleaned:
                return
            self._travel_state = cleaned
            self._append_transcript(f"[travel] {cleaned}\n")

    def update_rest_state(self, state: str):
        if not self.enabled:
            return
        cleaned = state.strip()
        if not cleaned:
            return
        with self._lock:
            if self._rest_state == cleaned:
                return
            self._rest_state = cleaned
            self._append_transcript(f"[rest] {cleaned}\n")

    def update_encumbrance(self, state: str):
        if not self.enabled:
            return
        cleaned = state.strip()
        if not cleaned:
            return
        with self._lock:
            normalized = cleaned.lower()
            if self._encumbrance == normalized:
                return
            self._encumbrance = normalized
            self._append_transcript(f"[encumbrance] {normalized}\n")

    def update_inventory_state(self, state: str):
        if not self.enabled:
            return
        cleaned = state.strip()
        if not cleaned:
            return
        with self._lock:
            normalized = cleaned.lower()
            if self._inventory_state == normalized:
                return
            self._inventory_state = normalized
            self._append_transcript(f"[inventory] {normalized}\n")

    def track_stagnation(self, location: str, streak: int):
        if not self.enabled:
            return
        if streak < 1:
            return
        cleaned = location.strip()
        if not cleaned:
            return
        with self._lock:
            if streak >= 4 and self._stagnation_flag != cleaned:
                self._stagnation_flag = cleaned
                self._issues.append(f"Still at {cleaned} after {streak} prompts; explore new actions")
            elif streak <= 1 and self._stagnation_flag == cleaned:
                self._stagnation_flag = None

    def record_issue(self, issue: str):
        if not self.enabled:
            return
        cleaned = issue.strip()
        if not cleaned:
            return
        with self._lock:
            count = self._issue_counts.get(cleaned, 0) + 1
            self._issue_counts[cleaned] = count
            if count == 1:
                self._issues.append(cleaned)
            elif count in {3, 5}:
                self._issues.append(f"{cleaned} (x{count})")

    def record_opportunity(self, message: str):
        if not self.enabled:
            return
        cleaned = message.strip()
        if not cleaned:
            return
        with self._lock:
            self._opportunities.append(cleaned)
            self._append_transcript(f"[opportunity] {cleaned}\n")

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
            issues = list(self._issues)[-8:]
            opportunities = list(self._opportunities)[-6:]
            reason = self._pending_reason or ""
            self._pending_reason = None
            hp = self._last_hp
            ep = self._last_ep
            gold = self._last_gold
            location = self._location
            exits = self._exits
            environment = self._environment
            travel_state = self._travel_state
            rest_state = self._rest_state
            encumbrance = self._encumbrance
            inventory_state = self._inventory_state
            location_history = list(self._location_history)
        summary_lines = []
        if recent_commands:
            summary_lines.append("Recent commands: " + ", ".join(recent_commands))
        if manual:
            summary_lines.append("Player-entered commands: " + ", ".join(manual))
        if events:
            summary_lines.append("Notable events: " + "; ".join(events))
        if opportunities:
            summary_lines.append("Opportunities: " + "; ".join(opportunities))
        if location_history:
            ordered: List[str] = []
            for entry in location_history:
                if entry not in ordered:
                    ordered.append(entry)
            if ordered:
                summary_lines.append("Recent locations: " + " -> ".join(ordered[-5:]))
        status_bits: List[str] = []
        if hp is not None and ep is not None:
            status_bits.append(f"HP {hp} / EP {ep}")
        if gold is not None:
            status_bits.append(f"Gold {gold}")
        if location:
            status_bits.append(f"Location: {location}")
        if exits:
            status_bits.append(f"Exits: {exits}")
        if environment:
            status_bits.append(f"Environment: {environment}")
        if travel_state:
            status_bits.append(f"Travel: {travel_state}")
        if rest_state:
            status_bits.append(f"Rest: {rest_state}")
        if encumbrance:
            status_bits.append(f"Encumbrance: {encumbrance}")
        if inventory_state:
            status_bits.append(f"Inventory: {inventory_state}")
        if status_bits:
            summary_lines.append("Status: " + "; ".join(status_bits))
        if issues:
            summary_lines.append("Recent issues: " + "; ".join(issues))
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
            raw = line.strip()
            if not raw:
                continue
            stripped = raw.strip("#").strip()
            if not stripped:
                continue
            if stripped in {"{", "}", "[", "]"}:
                continue
            if stripped[0] in "{}[]" or stripped[-1] in "{}[]":
                continue
            if ":" in stripped:
                continue
            if stripped.startswith('"') and stripped.endswith('"'):
                continue
            commands.append(stripped)
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
        self._last_command_sent: str = ""
        self._search_failures = 0
        self._gold_failures = 0
        self._blank_response_streak = 0
        self._last_prompt_status: Optional[Tuple[int, int]] = None
        self._last_location_summary: Optional[str] = None
        self._last_exits_summary: Optional[str] = None
        self._last_environment_summary: Optional[str] = None
        self._repeat_location_count = 0
        self._stagnation_notice_location: Optional[str] = None

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
        self._last_command_sent = ""
        self._search_failures = 0
        self._gold_failures = 0
        self._blank_response_streak = 0
        self._last_prompt_status = None
        self._last_location_summary = None
        self._last_exits_summary = None
        self._last_environment_summary = None
        self._repeat_location_count = 0
        self._stagnation_notice_location = None
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
        self._last_command_sent = command.strip()
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
        self._last_command_sent = ""
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
        status_match = PROMPT_STATUS_PATTERN.search(self._buffer)
        if status_match:
            hp = int(status_match.group("hp"))
            ep = int(status_match.group("ep"))
            current = (hp, ep)
            if self._planner:
                self._planner.update_vitals(hp, ep)
            if current != self._last_prompt_status:
                self._last_prompt_status = current
                if hp <= 20:
                    self.display.emit("event", "Health is low; rest or seek healing")
                    if self._planner:
                        self._planner.note_event("HP critically low")
                        self._planner.record_issue("HP critically low")
                if ep <= 15:
                    self.display.emit("event", "Energy is low; consider resting")
                    if self._planner:
                        self._planner.note_event("EP low")
                        self._planner.record_issue("EP reserves low")
            if self._planner:
                self._planner.update_rest_state("awake")
        if TRAVELTO_START_PATTERN.search(self._buffer):
            self._travel_active = True
            self.display.emit("event", "Travelto route engaged; awaiting arrival")
            if self._planner:
                self._planner.note_event("Travelto auto-travel engaged")
                self._planner.update_travel_state("auto-travel engaged")
            self._consume(TRAVELTO_START_PATTERN)
            return
        if TRAVELTO_RESUME_PATTERN.search(self._buffer):
            self._travel_active = True
            self.display.emit("event", "Travelto route resumed")
            if self._planner:
                self._planner.note_event("Travelto route resumed")
                self._planner.update_travel_state("auto-travel resumed")
            self._consume(TRAVELTO_RESUME_PATTERN)
            return
        if TRAVELTO_ABORT_PATTERN.search(self._buffer) or TRAVELTO_COMPLETE_PATTERN.search(self._buffer):
            self._travel_active = False
            self.display.emit("event", "Travelto route ended")
            if self._planner:
                self._planner.note_event("Travelto route ended")
                self._planner.update_travel_state("idle")
                self._planner.request_commands("travelto ended")
            self._buffer = TRAVELTO_ABORT_PATTERN.sub("", self._buffer)
            self._buffer = TRAVELTO_COMPLETE_PATTERN.sub("", self._buffer)
            return
        location_name: Optional[str] = None
        location_match = LOCATION_AT_PATTERN.search(self._buffer)
        if location_match:
            location_name = location_match.group("location").strip()
            self._remove_match(location_match)
        else:
            for pattern in ROOM_NAME_PATTERNS:
                name_match = pattern.search(self._buffer)
                if name_match:
                    location_name = name_match.group("room").strip()
                    self._remove_match(name_match)
                    break
        if location_name:
            if location_name == self._last_location_summary:
                self._repeat_location_count += 1
            else:
                self._last_location_summary = location_name
                self._search_failures = 0
                self._blank_response_streak = 0
                self._repeat_location_count = 1
                self._stagnation_notice_location = None
                self.display.emit("event", f"Location update: {location_name}")
                if self._planner:
                    self._planner.note_event(f"Now at {location_name}")
            if self._planner:
                self._planner.update_location(location_name)
                self._planner.track_stagnation(location_name, self._repeat_location_count)
            if (
                self._repeat_location_count >= 4
                and self._stagnation_notice_location != location_name
            ):
                self._stagnation_notice_location = location_name
                self.display.emit(
                    "event",
                    f"Still in {location_name}; explore new exits or objectives",
                )
                if self._planner:
                    self._planner.record_issue(
                        f"Still in {location_name} after several prompts"
                    )
                    self._planner.request_commands("stuck in location")
        exits_summary: Optional[str] = None
        exits_match = EXITS_PATTERN.search(self._buffer)
        if exits_match:
            exits_summary = exits_match.group("exits").strip()
            self._remove_match(exits_match)
        else:
            alt_match = ALT_EXITS_PATTERN.search(self._buffer)
            if alt_match:
                exits_summary = alt_match.group("exits").strip()
                self._remove_match(alt_match)
            else:
                standard_match = STANDARD_EXITS_PATTERN.search(self._buffer)
                if standard_match:
                    block = standard_match.group("block")
                    options = [
                        direction.strip()
                        for direction in re.findall(r"^\s*([A-Za-z]+):", block, flags=re.MULTILINE)
                    ]
                    exits_summary = ", ".join(options)
                    self._remove_match(standard_match)
        if exits_summary and exits_summary != self._last_exits_summary:
            self._last_exits_summary = exits_summary
            self.display.emit("event", f"Obvious exits: {exits_summary}")
            if self._planner:
                self._planner.update_exits(exits_summary)
        environment_parts: List[str] = []
        sky_match = SKY_PATTERN.search(self._buffer)
        if sky_match:
            environment_parts.append("Sky " + sky_match.group("sky").strip())
            self._remove_match(sky_match)
        time_match = TIME_OF_DAY_PATTERN.search(self._buffer)
        if time_match:
            environment_parts.append("Sun " + time_match.group("detail").strip())
            self._remove_match(time_match)
        if environment_parts:
            environment_summary = "; ".join(environment_parts)
            if environment_summary != self._last_environment_summary:
                self._last_environment_summary = environment_summary
                self.display.emit("event", f"Environment update: {environment_summary}")
                if self._planner:
                    self._planner.update_environment(environment_summary)
        path_match = PATH_CONTINUES_PATTERN.search(self._buffer)
        if path_match:
            directions = path_match.group("directions").strip()
            self.display.emit(
                "event",
                f"Path continues {directions}; explore to track new routes",
            )
            if self._planner:
                self._planner.note_event(f"Path continues {directions}")
                self._planner.record_opportunity(f"New route available {directions}")
            self._remove_match(path_match)
        city_match = CITY_NEAR_PATTERN.search(self._buffer)
        if city_match:
            city = city_match.group("city").strip()
            direction = city_match.group("direction").strip()
            self.display.emit(
                "event",
                f"Nearby city {city} lies {direction}; consider visiting for supplies",
            )
            if self._planner:
                self._planner.note_event(f"City {city} located {direction}")
                self._planner.record_opportunity(f"City {city} accessible {direction}")
            self._remove_match(city_match)
        forge_match = FORGE_PATH_PATTERN.search(self._buffer)
        if forge_match:
            self.display.emit(
                "event",
                "Forge entrance spotted; try 'path' or 'enter' to investigate",
            )
            if self._planner:
                self._planner.record_issue("Forge entrance available for crafting or trade")
                self._planner.request_commands("forge entrance")
                self._planner.record_opportunity("Forge nearby; craft or repair gear")
            self._remove_match(forge_match)
        action_hint_match = SUGGEST_ACTION_PATTERN.search(self._buffer)
        if action_hint_match:
            action = action_hint_match.group("action").strip()
            self.display.emit(
                "event",
                f"Room suggests you '{action}' something; experiment with that verb",
            )
            if self._planner:
                self._planner.record_issue(f"Try to {action} the highlighted object")
                self._planner.request_commands(f"try to {action}")
                self._planner.record_opportunity(f"Room encourages you to {action}")
            self._remove_match(action_hint_match)
        map_hint_match = MAP_SUGGESTION_PATTERN.search(self._buffer)
        if map_hint_match:
            self.display.emit("event", "Map hint detected; use 'map' to orient yourself")
            if self._planner:
                self._planner.note_event("Game suggested using map command")
                self._planner.request_commands("map hint")
            self._remove_match(map_hint_match)
        xp_match = EXPERIENCE_GAIN_PATTERN.search(self._buffer)
        if xp_match:
            xp = xp_match.group("xp")
            message = f"Experience gained: {xp}"
            self.display.emit("event", message)
            if self._planner:
                self._planner.note_event(message)
                self._planner.record_opportunity("Recent victory yielded experience; consider pressing the advantage")
                self._planner.request_commands("experience gained")
            self._remove_match(xp_match)
            return
        level_match = LEVEL_UP_PATTERN.search(self._buffer)
        if level_match:
            level = level_match.group("level")
            message = f"Level up! Now level {level}. Visit trainers for upgrades"
            self.display.emit("event", message)
            if self._planner:
                self._planner.note_event(message)
                self._planner.record_opportunity("Level increased; visit trainers or spend new skill points")
                self._planner.request_commands("level up")
            self._remove_match(level_match)
            return
        skill_match = SKILL_IMPROVE_PATTERN.search(self._buffer)
        if skill_match:
            skill = skill_match.group("skill").strip()
            message = f"Skill improved: {skill}"
            self.display.emit("event", message)
            if self._planner:
                self._planner.note_event(message)
                self._planner.record_opportunity(f"Skill {skill} improved; seek tougher challenges")
                self._planner.request_commands("skill improved")
            self._remove_match(skill_match)
            return
        coin_match = COIN_GAIN_PATTERN.search(self._buffer)
        if coin_match:
            amount = int(coin_match.group("amount"))
            message = f"Collected {amount} gold coins"
            self.display.emit("event", message)
            self._gold_failures = 0
            if self._planner:
                self._planner.note_event(message)
                self._planner.record_opportunity("Gold reserves growing; consider visiting shops or banks")
                self._planner.request_commands("gold collected")
            self._remove_match(coin_match)
            return
        item_match = ITEM_ACQUIRE_PATTERN.search(self._buffer)
        if item_match:
            item = item_match.group("item").strip()
            if item and "gold" not in item.lower():
                message = f"Acquired item: {item}"
                self.display.emit("event", message)
                if self._planner:
                    self._planner.note_event(message)
                    self._planner.record_opportunity(f"Evaluate newly acquired {item} for use or sale")
                    self._planner.request_commands("item acquired")
            self._remove_match(item_match)
            return
        search_success_match = SEARCH_SUCCESS_PATTERN.search(self._buffer)
        if search_success_match:
            discovery = search_success_match.group("discovery").strip()
            self._search_failures = 0
            message = f"Search uncovered {discovery}"
            self.display.emit("event", message)
            if self._planner:
                self._planner.note_event(message)
                self._planner.record_opportunity(f"Investigate discovered {discovery}")
                self._planner.request_commands("search success")
            self._remove_match(search_success_match)
            return
        rest_complete_match = REST_COMPLETE_PATTERN.search(self._buffer)
        if rest_complete_match:
            self.display.emit("event", "Rest completed; HP/EP refreshed")
            if self._planner:
                self._planner.note_event("Rest completed")
                self._planner.update_rest_state("rested")
                self._planner.record_opportunity("Recovered energy; resume exploration or hunting")
                self._planner.request_commands("rest complete")
            self._remove_match(rest_complete_match)
            return
        door_open_match = DOOR_OPENED_PATTERN.search(self._buffer)
        if door_open_match:
            direction = door_open_match.group("direction").lower()
            self.display.emit("event", f"{direction.title()} door opened; proceed through before it closes")
            if self._planner:
                self._planner.note_event(f"Opened {direction} door")
                self._planner.request_commands("door opened")
            self._remove_match(door_open_match)
            return
        door_locked_match = DOOR_LOCKED_PATTERN.search(self._buffer)
        if door_locked_match:
            direction = door_locked_match.group("direction").lower()
            self.display.emit("event", f"The {direction} door is locked; find a key or alternate route")
            if self._planner:
                self._planner.record_issue(f"{direction.title()} door locked")
                self._planner.request_commands("door locked")
            self._remove_match(door_locked_match)
            return
        hint_match = HINT_SUGGESTION_PATTERN.search(self._buffer)
        if hint_match:
            hint_text = hint_match.group("hint").strip()
            if self._planner:
                self._planner.record_issue(f"Hint: {hint_text}")
                self._planner.note_event(f"Hint observed: {hint_text}")
                self._planner.request_commands("hint observed")
            self._remove_match(hint_match)
        updates_match = UPDATES_ALERT_PATTERN.search(self._buffer)
        if updates_match:
            self.display.emit("event", "Updates available; run 'updates all' for the latest changes")
            if self._planner:
                self._planner.note_event("Game announced new updates")
                self._planner.record_issue("Review 'updates all' for new content")
                self._planner.request_commands("updates available")
            self._remove_match(updates_match)
        email_match = EMAIL_REMINDER_PATTERN.search(self._buffer)
        if email_match:
            self.display.emit("event", "Email not set; use 'chfn' if needed (optional)")
            self._remove_match(email_match)
        header_match = INVENTORY_HEADER_PATTERN.search(self._buffer)
        if header_match:
            amount = int(header_match.group("gold"))
            enc_state = header_match.group("encumbrance").strip()
            message = (
                "Gold purse is empty; gather coins before shopping"
                if amount == 0
                else f"Gold on hand: {amount}"
            )
            if self._last_gold_report != amount:
                self.display.emit("event", message)
                self._last_gold_report = amount
            if self._planner:
                self._planner.update_gold(amount)
                self._planner.update_encumbrance(enc_state)
                self._planner.update_inventory_state(f"encumbrance {enc_state}")
                self._planner.note_event(message)
            self._remove_match(header_match)
        empty_match = INVENTORY_EMPTY_PATTERN.search(self._buffer)
        if empty_match:
            self.display.emit("event", "Inventory empty; hunt or loot items to sell")
            if self._planner:
                self._planner.update_inventory_state("empty")
                self._planner.record_issue("Inventory empty; gather loot")
            self._remove_match(empty_match)
        carrying_match = INVENTORY_LIST_PATTERN.search(self._buffer)
        if carrying_match:
            if self._planner:
                self._planner.update_inventory_state("items carried")
            self._remove_match(carrying_match)
        match = NO_GOLD_PATTERN.search(self._buffer)
        if match:
            self._gold_failures += 1
            self.display.emit("event", "Purchase failed due to insufficient gold")
            if self._planner:
                self._planner.note_event("Not enough gold to buy; seek coins or items to sell")
                failure_summary = self._last_command_sent or "purchase"
                self._planner.record_issue(f"{failure_summary} blocked by empty purse")
                self._planner.request_commands("insufficient gold")
            if self._gold_failures >= 3:
                self.display.emit("event", "Repeated purchase failures; gather coins before shopping")
                if self._planner:
                    self._planner.note_event("Repeated purchase failures due to zero gold")
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
        menu_match = MENU_HINT_PATTERN.search(self._buffer)
        if menu_match:
            self.display.emit("event", "Menu hint detected; use 'read menu' then 'order <item>'")
            if self._planner:
                self._planner.note_event("Tavern suggested using menu")
                self._planner.record_issue("Need coins to order from menu")
                self._planner.request_commands("menu hint")
            self._remove_match(menu_match)
        board_match = BOARD_HELP_PATTERN.search(self._buffer)
        if board_match:
            self.display.emit("event", "Message board help available; try 'help board'")
            if self._planner:
                self._planner.note_event("Board suggested consulting help file")
                self._planner.record_issue("Use 'help board' for instructions")
                self._planner.request_commands("board help")
            self._remove_match(board_match)
        match = SEARCH_FAIL_PATTERN.search(self._buffer)
        if match:
            self._search_failures += 1
            self.display.emit("event", "Search revealed nothing; try other rooms or targets")
            if self._planner:
                self._planner.note_event("Search failed; consider new area or target")
                failure_summary = self._last_command_sent or "search"
                self._planner.record_issue(f"{failure_summary} yielded nothing")
                self._planner.request_commands("search failed")
            if self._search_failures >= 3:
                self.display.emit("event", "Multiple searches failed; explore new rooms or hunt creatures")
                if self._planner:
                    self._planner.note_event("Repeated search failures")
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
        door_match = CLOSED_DOOR_PATTERN.search(self._buffer)
        if door_match:
            direction = door_match.group("direction").lower()
            self.display.emit(
                "event",
                f"The {direction} door is closed; try 'open {direction} door' before moving",
            )
            if self._planner:
                self._planner.record_issue(f"{direction} door closed")
                self._planner.request_commands("door closed")
            self._remove_match(door_match)
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
        corpse_match = CORPSE_MISSING_PATTERN.search(self._buffer)
        if corpse_match:
            self.display.emit("event", "No corpse available yet; finish combat before looting")
            if self._planner:
                self._planner.record_issue("Attempted to loot before corpse existed")
                self._planner.request_commands("corpse missing")
            self._remove_match(corpse_match)
            return
        stock_match = NO_STOCK_PATTERN.search(self._buffer)
        if stock_match:
            self.display.emit("event", "Shop lacks that item; check 'list' or try another vendor")
            if self._planner:
                self._planner.note_event("Shop reported no stock")
                self._planner.record_issue("Requested item not sold here")
                self._planner.request_commands("item unavailable")
            self._remove_match(stock_match)
            return
        belongs_match = ITEM_BELONGS_PATTERN.search(self._buffer)
        if belongs_match:
            self.display.emit("event", "Item belongs to someone; avoid theft and seek legal loot")
            if self._planner:
                self._planner.record_issue("Item was owned; look for alternatives")
                self._planner.request_commands("item owned")
            self._remove_match(belongs_match)
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
        wield_match = WIELD_WHAT_PATTERN.search(self._buffer)
        if wield_match:
            self.display.emit("event", "Specify an item to wield; check inventory for weapons")
            if self._planner:
                self._planner.record_issue("Wield command missing target item")
                self._planner.request_commands("wield guidance")
            self._remove_match(wield_match)
            return
        news_match = NEWS_ALERT_PATTERN.search(self._buffer)
        if news_match:
            self.display.emit("event", "News available; use 'news' or 'read news'")
            if self._planner:
                self._planner.note_event("News bulletin advertised")
                self._planner.record_issue("Review latest news for quests")
                self._planner.request_commands("news alert")
            self._remove_match(news_match)
        match = ASK_BLANK_PATTERN.search(self._buffer)
        if match:
            npc = "An NPC"
            if "npc" in match.groupdict():
                npc = match.group("npc").strip()
            self.display.emit("event", f"{npc} has no answer; try another topic or character")
            if self._planner:
                self._planner.note_event(f"{npc} offered no information")
                self._planner.record_issue(f"{npc} could not help")
                self._planner.request_commands("npc unhelpful")
            self._remove_match(match)
            return
        blank_match = BLANK_RESPONSE_PATTERN.search(self._buffer)
        if blank_match:
            self._blank_response_streak += 1
            self.display.emit("event", "NPC is unresponsive; switch topics or explore elsewhere")
            if self._planner:
                self._planner.note_event("NPC looked blankly")
                self._planner.record_issue("NPC offered no clues")
                if self._blank_response_streak >= 2:
                    self._planner.request_commands("npc unresponsive")
            self._remove_match(blank_match)
            return
        match = REST_START_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Resting to recover; monitor HP/EP before resuming hunts")
            if self._planner:
                self._planner.note_event("Rest started")
                self._planner.update_rest_state("resting")
                self._planner.request_commands("resting")
            self._remove_match(match)
            return
        match = REST_INTERRUPT_PATTERN.search(self._buffer)
        if match:
            self.display.emit("event", "Rest interrupted; consider resuming or pursuing another action")
            if self._planner:
                self._planner.note_event("Rest interrupted")
                self._planner.update_rest_state("interrupted")
                self._planner.request_commands("rest interrupted")
            self._remove_match(match)
            return
        busy_match = BUSY_ATTACK_PATTERN.search(self._buffer)
        if busy_match:
            self.display.emit("event", "Busy fighting; wait for round to finish or issue defensive commands")
            if self._planner:
                self._planner.record_issue("Too busy to attack; avoid spamming commands")
            self._remove_match(busy_match)
            return
        no_effect_match = NO_EFFECT_PATTERN.search(self._buffer)
        if no_effect_match:
            self.display.emit("event", "Attacks have no effect; switch weapons or retreat")
            if self._planner:
                self._planner.record_issue("Attacks ineffective against foe")
                self._planner.request_commands("attack ineffective")
            self._remove_match(no_effect_match)
            return
        low_damage_match = LOW_DAMAGE_PATTERN.search(self._buffer)
        if low_damage_match:
            self.display.emit("event", "Damage is minimal; consider stronger weapons or new tactics")
            if self._planner:
                self._planner.record_issue("Dealing minimal damage")
            self._remove_match(low_damage_match)
        consider_match = CONSIDER_PATTERN.search(self._buffer)
        if consider_match:
            target = consider_match.group("target").strip()
            assessment = consider_match.group("assessment").strip()
            self.display.emit(
                "event",
                f"Assessment: {target} is {assessment}; choose fights accordingly",
            )
            if self._planner:
                self._planner.note_event(f"Considered {target}: {assessment}")
            self._remove_match(consider_match)
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
        quests_match = NO_QUESTS_PATTERN.search(self._buffer)
        if quests_match:
            self.display.emit("event", "No quests completed yet; explore towns for tasks")
            if self._planner:
                self._planner.note_event("Quest log empty")
                self._planner.record_issue("Seek quests or newbie jobs")
                self._planner.request_commands("quest search")
            self._remove_match(quests_match)
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
                self._planner.update_gold(amount)
                self._planner.note_event(message)
                if amount == 0:
                    self._planner.request_commands("gold depleted")
            if amount > 0:
                self._gold_failures = 0
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
        self.exit_requested = False

    def run(self):
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            except (KeyboardInterrupt, OSError, ValueError):
                self.exit_requested = True
                self._stop.set()
                break
            if self._stop.is_set():
                break
            if not ready:
                continue
            line = sys.stdin.readline()
            if line == "":
                self.exit_requested = True
                break
            command = line.rstrip("\n")
            if command.strip().lower() in {":exit", ":quit"}:
                self.session.display.emit("event", "Local shutdown requested")
                self.session.disconnect()
                self._stop.set()
                self.exit_requested = True
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
    if not DEFAULT_PROFILES:
        display.emit("error", "No character profiles configured.")
        display.ensure_newline()
        return

    knowledge_text = GameKnowledge.build_reference()
    session = TelnetSession(display)
    planner = OllamaPlanner(
        send_callback=lambda cmd: session.send_command(cmd, source="ollama"),
        knowledge_text=knowledge_text,
        enabled=OLLAMA_ENABLED,
    )
    session.attach_planner(planner)

    input_thread = ConsoleInputThread(session)
    input_thread.start()

    profile_index = 0
    instructions_shown = False

    try:
        while not input_thread.exit_requested:
            profile = DEFAULT_PROFILES[profile_index]
            try:
                session.connect(profile)
            except RuntimeError as exc:
                display.emit("error", str(exc))
                if input_thread.exit_requested:
                    break
                time.sleep(5.0)
                profile_index = (profile_index + 1) % len(DEFAULT_PROFILES)
                continue

            if not instructions_shown:
                display.emit("event", "Type commands directly; use :exit to close locally.")
                if OLLAMA_ENABLED:
                    display.emit("event", "Ollama automation is active and will respond after prompts.")
                else:
                    display.emit("event", "Ollama automation is disabled via configuration.")
                instructions_shown = True

            while not input_thread.exit_requested:
                listener = session._listener
                if not listener or not listener.is_alive():
                    break
                time.sleep(0.5)

            if session._listener is not None or session.connection is not None:
                session.disconnect()

            if input_thread.exit_requested:
                break

            profile_index = (profile_index + 1) % len(DEFAULT_PROFILES)
            time.sleep(3.0)
    except KeyboardInterrupt:
        display.emit("event", "Interrupted locally; closing session.")
    finally:
        input_thread.stop()
        input_thread.join(timeout=1.0)
        if session._listener is not None or session.connection is not None:
            session.disconnect()
        planner.shutdown()
        display.ensure_newline()


if __name__ == "__main__":
    run_client()
