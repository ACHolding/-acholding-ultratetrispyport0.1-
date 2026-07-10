#!/usr/bin/env python3
# ultra!tetris 0.1 — Game Boy DMG base • files = off • python 3.14 • pygame • 60 fps

from __future__ import annotations

import array
from dataclasses import dataclass, field

import pygame

FILES_OFF = True
APP_TITLE = "ultra!tetris 0.1"
TARGET_FPS = 60
GB_FPS = 59.73  # DMG refresh rate (ROM-accurate timing scaled to 60 fps)

# Game Boy playfield: 10 × 18 visible (+ 2 spawn rows above), files = off
CELL = 22
BOARD_W = 10
BOARD_VISIBLE_H = 18
BOARD_HIDDEN_H = 2
BOARD_H = BOARD_VISIBLE_H + BOARD_HIDDEN_H
PANEL_W = 132
MARGIN = 28
GAME_W = MARGIN * 2 + BOARD_W * CELL + PANEL_W
GAME_H = MARGIN * 2 + BOARD_VISIBLE_H * CELL + 16
MENU_W, MENU_H = 480, 432
GB_MAX_LEVEL = 20
GB_MAX_SCORE = 999_999

# DMG LCD 4-shade palette (procedural, files = off)
COL_GB_0 = (15, 56, 15)      # #0F380F darkest — locked blocks
COL_GB_1 = (48, 98, 48)      # #306230 dark
COL_GB_2 = (139, 172, 15)    # #8BAC0F light
COL_GB_3 = (155, 188, 15)    # #9BBC0F lightest — background
COL_BG = COL_GB_3
COL_FIELD = COL_GB_2
COL_BLOCK = COL_GB_0
COL_ACTIVE = COL_GB_1
COL_TEXT = COL_GB_0
COL_DIM = COL_GB_1
COL_ACCENT = COL_GB_0
COL_MENU_SEL = COL_GB_0
COL_BORDER = COL_GB_1

# ROM $1B06 — gravity bytes (stored as frames−1; Rev A World ROM, files = off)
ROM_GRAVITY_RAW = bytes((
    0x34, 0x30, 0x2C, 0x28, 0x24, 0x20, 0x1B, 0x15, 0x10, 0x0A,
    0x09, 0x08, 0x07, 0x06, 0x05, 0x05, 0x04, 0x04, 0x03, 0x03, 0x02,
))
ROM_GRAVITY = tuple(b + 1 for b in ROM_GRAVITY_RAW)

# ROM timing constants (@ 59.73 Hz — scaled per frame via RomClock)
ROM_ARE = 2
ROM_LINE_CLEAR = 93
ROM_DAS_DELAY = 23
ROM_DAS_REPEAT = 9
ROM_DIV_PER_FRAME = 274  # 70224 T-states / 256 @ 59.73 Hz

# DMG scoring — 40/100/300/1200 × (level + 1); soft drop not multiplied
GB_SCORE_TABLE = (0, 40, 100, 300, 1200)

# ROM piece IDs — L,J,I,Z,O,S,T ×4 (low 2 bits = rotation in WRAM $C213)
GB_KIND_TO_ROM = {"L": 0, "J": 4, "I": 8, "Z": 12, "O": 16, "S": 20, "T": 24}
GB_ROM_TO_KIND = {v: k for k, v in GB_KIND_TO_ROM.items()}
GB_KIND_ORDER = ("L", "J", "I", "Z", "O", "S", "T")


@dataclass
class RomClock:
    """59.73 Hz DMG frame accumulator (no rounding drift)."""
    carry: float = 0.0

    def pulse(self) -> int:
        self.carry += TARGET_FPS / GB_FPS
        ticks = int(self.carry)
        self.carry -= ticks
        return ticks

    def reset(self) -> None:
        self.carry = 0.0


class GBRomCpu:
    """LR35902 opcode paths used by Tetris (Rev A), files = off."""

    def __init__(self) -> None:
        self.div = 0x00  # HRAM $FF04

    def tick_frame(self) -> None:
        self.div = (self.div + ROM_DIV_PER_FRAME) & 0xFF

    @staticmethod
    def _opcode_div_sample(div: int) -> int:
        """ROM $2043–$2054 — map DIV register to piece id nibble."""
        b = div & 0xFF
        a = 0
        while True:
            b = (b - 1) & 0xFF
            if b == 0:
                return a
            a = (a + 4) & 0xFF
            if a == 0x1C:
                a = 0

    def op_randomizer(self, lock_id: int, preview_id: int) -> int:
        """ROM $2041–$2063 — Henk Rogers OR-retry randomizer (3 attempts)."""
        c = lock_id & 0xFC
        attempts = 3
        while True:
            d = self._opcode_div_sample(self.div)
            self.div = (self.div + 1) & 0xFF
            attempts -= 1
            if attempts == 0 or ((preview_id | d | c) & 0xFC) != c:
                return d
            self.div = (self.div + 17) & 0xFF  # fresh DIV read between retries

    @staticmethod
    def op_l0166_add_bcd(score: bytearray, addend: int) -> None:
        """ROM $0166 — BCD add DE to 3-byte score, cap 999999."""
        current = GBRomCpu.bcd_to_int(score)
        current = min(GB_MAX_SCORE, current + addend)
        score[:] = GBRomCpu.int_to_bcd(current)

    @staticmethod
    def bcd_to_int(score: bytearray) -> int:
        b0, b1, b2 = score[0], score[1], score[2]
        return (b2 >> 4) * 100000 + (b2 & 0xF) * 10000 + (b1 >> 4) * 1000 + (b1 & 0xF) * 100 + (b0 >> 4) * 10 + (b0 & 0xF)

    @staticmethod
    def int_to_bcd(value: int) -> bytearray:
        value = max(0, min(value, GB_MAX_SCORE))
        s = f"{value:06d}"
        return bytearray((
            (int(s[4]) << 4) | int(s[5]),
            (int(s[2]) << 4) | int(s[3]),
            (int(s[0]) << 4) | int(s[1]),
        ))

    @staticmethod
    def op_l1afa_gravity(level: int) -> int:
        """ROM $1AFA — load frames/row from table @ $1B06."""
        level = max(0, min(level, GB_MAX_LEVEL))
        return ROM_GRAVITY[level]

# Game Boy tetromino rotation states (no wall kicks — ROM behavior)
SHAPES: dict[str, list[tuple[tuple[int, int], ...]]] = {
    "I": (((0, 1), (1, 1), (2, 1), (3, 1)), ((2, 0), (2, 1), (2, 2), (2, 3))),
    "O": (((1, 0), (2, 0), (1, 1), (2, 1)),),
    "T": (
        ((1, 0), (0, 1), (1, 1), (2, 1)),
        ((1, 0), (1, 1), (2, 1), (1, 2)),
        ((0, 1), (1, 1), (2, 1), (1, 2)),
        ((1, 0), (0, 1), (1, 1), (1, 2)),
    ),
    "S": (
        ((1, 0), (2, 0), (0, 1), (1, 1)),
        ((1, 0), (1, 1), (2, 1), (2, 2)),
    ),
    "Z": (
        ((0, 0), (1, 0), (1, 1), (2, 1)),
        ((2, 0), (1, 1), (2, 1), (1, 2)),
    ),
    "J": (
        ((0, 0), (0, 1), (1, 1), (2, 1)),
        ((1, 0), (2, 0), (1, 1), (1, 2)),
        ((0, 1), (1, 1), (2, 1), (2, 2)),
        ((1, 0), (1, 1), (0, 2), (1, 2)),
    ),
    "L": (
        ((2, 0), (0, 1), (1, 1), (2, 1)),
        ((1, 0), (1, 1), (1, 2), (2, 2)),
        ((0, 1), (1, 1), (2, 1), (0, 2)),
        ((0, 0), (1, 0), (1, 1), (1, 2)),
    ),
}

GB_SPAWN_X = {"I": 3, "O": 4, "T": 4, "S": 4, "Z": 4, "J": 4, "L": 4}

# Korobeiniki Music A — DMG 1:33 loop (93 s), files = off
GB_MUSIC_LOOP_MS = 93_000
GB_MUSIC_BPM = 76           # DMG A-Type playback feel (slower than folk 150)
GB_NOTE_GAP_MS = 28
GB_PITCH = 0.5
GB_DUTY = 0.25

NOTE = {
    "A3": 220.00, "B3": 246.94, "C4": 261.63, "D4": 293.66, "E4": 329.63,
    "F4": 349.23, "G4": 392.00, "A4": 440.00, "B4": 493.88,
    "C5": 523.25, "D5": 587.33, "E5": 659.25, "F5": 698.46, "G5": 783.99,
    "A5": 880.00, "B5": 987.77, "R": 0.0,
}

# Full Game Boy A-Type arrangement (A–A–B–A form, multiple strains)
_THEME_A = [
    ("E5", 8), ("B4", 4), ("C5", 4), ("D5", 4), ("E5", 4), ("D5", 4), ("C5", 4), ("B4", 4),
    ("A4", 8), ("A4", 4), ("C5", 4), ("E5", 8), ("D5", 4), ("C5", 4), ("B4", 8),
    ("C5", 4), ("D5", 4), ("E5", 4), ("C5", 4), ("A4", 4), ("A4", 8),
    ("D5", 12), ("F5", 4), ("A5", 8), ("G5", 4), ("F5", 4), ("E5", 12),
    ("C5", 4), ("E5", 4), ("D5", 4), ("C5", 4), ("B4", 8), ("B4", 4), ("C5", 4), ("D5", 4),
    ("E5", 4), ("C5", 4), ("A4", 4), ("A4", 8),
]

_THEME_B = [
    ("G4", 8), ("G4", 4), ("A4", 4), ("B4", 8), ("C5", 4), ("D5", 4), ("E5", 8),
    ("F5", 4), ("E5", 4), ("D5", 4), ("C5", 8), ("B4", 4), ("A4", 4), ("G4", 8),
    ("A4", 4), ("B4", 4), ("C5", 4), ("D5", 4), ("E5", 4), ("F5", 4), ("E5", 4), ("D5", 8),
    ("E5", 12), ("G5", 4), ("B5", 8), ("A5", 4), ("G5", 4), ("F5", 12),
    ("E5", 4), ("D5", 4), ("C5", 4), ("B4", 8), ("A4", 4), ("G4", 4), ("F4", 8),
    ("E4", 8), ("G4", 4), ("A4", 4), ("B4", 8), ("C5", 4), ("D5", 4), ("E5", 8),
]

_REST = lambda n: [("R", n)]


def _build_korobeiniki() -> list[tuple[str, int]]:
    """Assemble DMG A-Type loop; pad rests to exact 1:33 @ GB_MUSIC_BPM."""
    target_units = int(GB_MUSIC_LOOP_MS * GB_MUSIC_BPM * 16 / 60_000)
    phrase_rest = 56
    seq: list[tuple[str, int]] = []
    pattern = (
        lambda: _THEME_A + _REST(phrase_rest),
        lambda: _THEME_A + _REST(phrase_rest),
        lambda: _THEME_B + _REST(phrase_rest),
        lambda: _THEME_A + _REST(phrase_rest),
    )
    while sum(d for _, d in seq) < target_units:
        for part in pattern:
            seq += part()
            if sum(d for _, d in seq) >= target_units:
                break
    total = sum(d for _, d in seq)
    if total < target_units:
        seq += _REST(target_units - total)
    elif total > target_units:
        trim = total - target_units
        while trim > 0 and seq and seq[-1][0] == "R":
            note, dur = seq[-1]
            cut = min(dur, trim)
            seq[-1] = (note, dur - cut)
            trim -= cut
            if seq[-1][1] <= 0:
                seq.pop()
    return seq


KOROBEINIKI = _build_korobeiniki()
MUSIC_TOTAL_UNITS = sum(d for _, d in KOROBEINIKI)
MUSIC_MS_PER_UNIT = GB_MUSIC_LOOP_MS / MUSIC_TOTAL_UNITS


def tempo_ms(frames: int) -> int:
    return max(1, int(frames * MUSIC_MS_PER_UNIT))


def music_loop_label() -> str:
    sec = GB_MUSIC_LOOP_MS // 1000
    return f"{sec // 60}:{sec % 60:02d}"


def gb_gravity_frames(level: int) -> int:
    return GBRomCpu.op_l1afa_gravity(level)


def gb_level_from_lines(total_lines: int, start_level: int = 0) -> int:
    """A-TYPE Rev A — +10 lines/level, L9→10 needs 100 lines, then +20."""
    level = start_level
    needed = start_level * 10 + 10
    while level < GB_MAX_LEVEL and total_lines >= needed:
        level += 1
        if level == 9:
            needed += 100
        elif level > 9:
            needed += 20
        else:
            needed += 10
    return level


def gb_score(lines_cleared: int, level: int) -> int:
    if lines_cleared < 1 or lines_cleared > 4:
        return 0
    return GB_SCORE_TABLE[lines_cleared] * (level + 1)


@dataclass
class Piece:
    kind: str
    rot: int = 0
    x: int = 4
    y: int = 0

    def cells(self) -> tuple[tuple[int, int], ...]:
        offsets = SHAPES[self.kind][self.rot % len(SHAPES[self.kind])]
        return tuple((self.x + ox, self.y + oy) for ox, oy in offsets)

    def rotated(self) -> Piece:
        n = len(SHAPES[self.kind])
        return Piece(self.kind, (self.rot + 1) % n, self.x, self.y)


@dataclass
class GameState:
    board: list[list[bool]] = field(default_factory=list)
    current: Piece | None = None
    next_kind: str = "I"
    score_bcd: bytearray = field(default_factory=lambda: bytearray(3))
    lines: int = 0
    level: int = 0
    start_level: int = 0
    drop_timer: int = 0
    alive: bool = True
    paused: bool = False
    are_timer: int = 0
    line_clear_timer: int = 0
    soft_drop: bool = False
    das_dir: int = 0
    das_timer: int = 0
    rom: GBRomCpu = field(default_factory=GBRomCpu)
    clock: RomClock = field(default_factory=RomClock)
    lock_rom_id: int = 0
    preview_rom_id: int = 0
    next_rom_id: int = 0

    @property
    def score(self) -> int:
        return GBRomCpu.bcd_to_int(self.score_bcd)

    def reset(self, start_level: int = 0):
        self.board = [[False] * BOARD_W for _ in range(BOARD_H)]
        self.score_bcd = bytearray(3)
        self.lines = 0
        self.start_level = max(0, min(start_level, 9))
        self.level = self.start_level
        self.drop_timer = 0
        self.alive = True
        self.paused = False
        self.are_timer = 0
        self.line_clear_timer = 0
        self.soft_drop = False
        self.das_dir = 0
        self.das_timer = 0
        self.rom = GBRomCpu()
        self.clock = RomClock()
        self.lock_rom_id = 0
        self.preview_rom_id = 0
        self.next_rom_id = self.rom.op_randomizer(0, 0)
        self.next_kind = GB_ROM_TO_KIND[self.next_rom_id]
        self._spawn_piece()

    def _spawn_piece(self):
        kind = self.next_kind
        cur_preview = self.next_rom_id
        new_id = self.rom.op_randomizer(self.lock_rom_id, cur_preview)
        self.preview_rom_id = cur_preview
        self.next_rom_id = new_id
        self.next_kind = GB_ROM_TO_KIND[new_id]
        sx = GB_SPAWN_X.get(kind, 4)
        self.current = Piece(kind, 0, sx, 0)
        self.drop_timer = 0
        self.are_timer = ROM_ARE
        self.soft_drop = False
        if self.collides(self.current):
            self.alive = False

    def collides(self, piece: Piece, dx: int = 0, dy: int = 0) -> bool:
        for x, y in piece.cells():
            nx, ny = x + dx, y + dy
            if nx < 0 or nx >= BOARD_W or ny >= BOARD_H:
                return True
            if ny >= 0 and self.board[ny][nx]:
                return True
        return False

    def lock_piece(self):
        if not self.current:
            return
        self.lock_rom_id = GB_KIND_TO_ROM[self.current.kind]
        for x, y in self.current.cells():
            if 0 <= y < BOARD_H and 0 <= x < BOARD_W:
                self.board[y][x] = True
        cleared = self.clear_lines()
        self.current = None
        if cleared:
            self.rom.op_l0166_add_bcd(self.score_bcd, gb_score(cleared, self.level))
            self.lines += cleared
            self.level = gb_level_from_lines(self.lines, self.start_level)
            self.line_clear_timer = ROM_LINE_CLEAR
        else:
            self._spawn_piece()

    def clear_lines(self) -> int:
        keep: list[list[bool]] = []
        cleared = 0
        for row in self.board:
            if all(row):
                cleared += 1
            else:
                keep.append(row)
        while len(keep) < BOARD_H:
            keep.insert(0, [False] * BOARD_W)
        self.board = keep
        return cleared

    def try_move(self, dx: int, dy: int) -> bool:
        if not self.current or not self.alive or self.are_timer > 0 or self.line_clear_timer > 0:
            return False
        if not self.collides(self.current, dx, dy):
            self.current.x += dx
            self.current.y += dy
            return True
        return False

    def try_rotate(self) -> bool:
        if not self.current or not self.alive or self.are_timer > 0 or self.line_clear_timer > 0:
            return False
        rotated = self.current.rotated()
        if not self.collides(rotated):
            self.current = rotated
            return True
        return False

    def gravity_interval(self) -> int:
        g = gb_gravity_frames(self.level)
        return max(1, g // 3) if self.soft_drop else g

    def tick_gravity(self, pulses: int):
        if not self.current or not self.alive or self.are_timer > 0 or self.line_clear_timer > 0:
            return
        interval = self.gravity_interval()
        self.drop_timer += pulses
        while self.drop_timer >= interval:
            self.drop_timer -= interval
            if not self.try_move(0, 1):
                self.lock_piece()
                break

    def tick_das(self, pulses: int):
        if self.are_timer > 0 or self.line_clear_timer > 0 or not self.alive:
            return
        if self.das_dir == 0:
            self.das_timer = 0
            return
        for _ in range(pulses):
            self.das_timer += 1
            if self.das_timer == ROM_DAS_DELAY:
                self.try_move(self.das_dir, 0)
            elif self.das_timer > ROM_DAS_DELAY and (self.das_timer - ROM_DAS_DELAY) % ROM_DAS_REPEAT == 0:
                self.try_move(self.das_dir, 0)

    def tick(self):
        if not self.alive or self.paused:
            return
        self.rom.tick_frame()
        pulses = self.clock.pulse()
        if pulses <= 0:
            return
        if self.line_clear_timer > 0:
            self.line_clear_timer -= pulses
            if self.line_clear_timer <= 0:
                self.line_clear_timer = 0
                self._spawn_piece()
            return
        if self.are_timer > 0:
            self.are_timer = max(0, self.are_timer - pulses)
        self.tick_das(pulses)
        self.tick_gravity(pulses)

    def set_das(self, direction: int):
        if self.das_dir != direction:
            self.das_dir = direction
            self.das_timer = 0
            self.try_move(direction, 0)

    def stop_das(self):
        self.das_dir = 0
        self.das_timer = 0


class MusicPlayer:
    def __init__(self):
        self.enabled = False
        self.active = False
        self.idx = 0
        self.wait_until = 0
        self.rate = 22050
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=self.rate, size=-16, channels=2, buffer=512)
            self.channel = pygame.mixer.Channel(0)
            self.enabled = True
        except pygame.error:
            self.channel = None

    def start(self):
        if self.active:
            return
        self.active = True
        self.idx = 0
        self.wait_until = pygame.time.get_ticks()

    def stop(self):
        self.active = False
        self.idx = 0
        self.wait_until = 0
        if self.channel:
            self.channel.stop()

    @staticmethod
    def _square(freq: float, ms: int, rate: int = 22050, vol: float = 0.18, duty: float = GB_DUTY) -> pygame.mixer.Sound | None:
        if freq <= 0 or ms <= 0:
            return None
        n = max(1, int(rate * ms / 1000))
        buf = array.array("h")
        phase = 0.0
        step = freq / rate
        amp = int(32767 * vol)
        for _ in range(n):
            phase = (phase + step) % 1.0
            sample = amp if phase < duty else -amp
            buf.append(sample)
            buf.append(sample)
        try:
            return pygame.mixer.Sound(buffer=buf)
        except pygame.error:
            return None

    def update(self):
        if not self.active or not self.enabled or not self.channel:
            return
        now = pygame.time.get_ticks()
        if now < self.wait_until:
            return
        note, frames = KOROBEINIKI[self.idx]
        self.idx = (self.idx + 1) % len(KOROBEINIKI)
        ms = tempo_ms(frames)
        freq = NOTE.get(note, 0.0) * GB_PITCH
        if freq > 0:
            snd = self._square(freq, ms, self.rate)
            if snd:
                self.channel.play(snd)
        gap = GB_NOTE_GAP_MS if freq > 0 else 0
        self.wait_until = now + ms + gap


@dataclass
class MenuState:
    items: tuple[str, ...] = ("PLAY GAME", "HELP", "EXIT")
    selected: int = 0
    screen: str = "menu"


def draw_gb_pixel(surface: pygame.Surface, x: int, y: int, color: tuple[int, int, int], size: int | None = None):
    sz = size or CELL
    pygame.draw.rect(surface, color, (x, y, sz - 1, sz - 1))


def draw_gb_playfield(surface: pygame.Surface, ox: int, oy: int):
    w, h = BOARD_W * CELL, BOARD_VISIBLE_H * CELL
    pygame.draw.rect(surface, COL_FIELD, (ox - 6, oy - 6, w + 12, h + 12))
    pygame.draw.rect(surface, COL_BORDER, (ox - 6, oy - 6, w + 12, h + 12), 2)
    pygame.draw.rect(surface, COL_GB_3, (ox, oy, w, h))


def draw_board(surface: pygame.Surface, fonts: dict[str, pygame.font.Font], game: GameState):
    surface.fill(COL_BG)
    ox = MARGIN
    oy = MARGIN
    draw_gb_playfield(surface, ox, oy)

    row_offset = BOARD_HIDDEN_H
    for vy in range(BOARD_VISIBLE_H):
        by = vy + row_offset
        for x in range(BOARD_W):
            px = ox + x * CELL
            py = oy + vy * CELL
            if game.board[by][x]:
                draw_gb_pixel(surface, px, py, COL_BLOCK)

    if game.current and game.alive and game.are_timer <= 1:
        for x, y in game.current.cells():
            vy = y - row_offset
            if 0 <= vy < BOARD_VISIBLE_H:
                draw_gb_pixel(surface, ox + x * CELL, oy + vy * CELL, COL_ACTIVE)

    px = ox + BOARD_W * CELL + 16
    surface.blit(fonts["lbl"].render("SCORE", True, COL_TEXT), (px, oy + 4))
    surface.blit(fonts["digit"].render(f"{min(game.score, GB_MAX_SCORE):06d}", True, COL_TEXT), (px, oy + 20))
    surface.blit(fonts["lbl"].render("LEVEL", True, COL_TEXT), (px, oy + 56))
    surface.blit(fonts["digit"].render(f"{game.level:02d}", True, COL_TEXT), (px, oy + 72))
    surface.blit(fonts["lbl"].render("LINES", True, COL_TEXT), (px, oy + 108))
    surface.blit(fonts["digit"].render(f"{game.lines:03d}", True, COL_TEXT), (px, oy + 124))
    surface.blit(fonts["lbl"].render("NEXT", True, COL_TEXT), (px, oy + 160))

    nk = game.next_kind
    preview = SHAPES[nk][0]
    ps = 10
    py0 = oy + 182
    for ox2, oy2 in preview:
        draw_gb_pixel(surface, px + ox2 * ps, py0 + oy2 * ps, COL_BLOCK, ps)

    surface.blit(fonts["lbl"].render("NINTENDO", True, COL_DIM), (px, oy + BOARD_VISIBLE_H * CELL - 14))
    surface.blit(fonts["lbl"].render("GAME BOY", True, COL_DIM), (px, oy + BOARD_VISIBLE_H * CELL - 2))

    if game.paused:
        ov = pygame.Surface((GAME_W, GAME_H), pygame.SRCALPHA)
        ov.fill((*COL_GB_0, 120))
        surface.blit(ov, (0, 0))
        t = fonts["title"].render("PAUSE", True, COL_GB_3)
        surface.blit(t, t.get_rect(center=(GAME_W // 2, GAME_H // 2)))


def draw_logo(surface: pygame.Surface, fonts: dict[str, pygame.font.Font], y: int):
    t = fonts["logo"].render("ultra!tetrris", True, COL_TEXT)
    surface.blit(t, t.get_rect(center=(MENU_W // 2, y)))
    s = fonts["lbl"].render("by the tetris company & acholding + nintendo", True, COL_DIM)
    surface.blit(s, s.get_rect(center=(MENU_W // 2, y + 36)))


def draw_main_menu(surface: pygame.Surface, fonts: dict[str, pygame.font.Font], menu: MenuState):
    surface.fill(COL_BG)
    draw_logo(surface, fonts, 72)
    for i, item in enumerate(menu.items):
        sel = i == menu.selected
        c = COL_TEXT if sel else COL_DIM
        p = ">" if sel else " "
        lbl = fonts["menu"].render(f"{p} {item}", True, c)
        surface.blit(lbl, lbl.get_rect(center=(MENU_W // 2, 220 + i * 40)))
    h = fonts["lbl"].render("A-TYPE  UP/DOWN  START", True, COL_DIM)
    surface.blit(h, h.get_rect(center=(MENU_W // 2, MENU_H - 24)))


def draw_help(surface: pygame.Surface, fonts: dict[str, pygame.font.Font]):
    surface.fill(COL_BG)
    draw_logo(surface, fonts, 40)
    lines = [
        "GAME BOY A-TYPE (files = off)",
        "  Left / Right     Move (DAS 23/9f @59.73Hz)",
        "  Up / Z / X       Rotate (no wall kick)",
        "  Down             Soft drop (G/3, +1 pt)",
        "  P                Pause",
        "  Esc              Menu",
        "",
        "ENGINE (Rev A ROM opcodes, files = off)",
        "  10x18 field • DIV randomizer $2041",
        "  Gravity: ROM $1B06 bytes (n−1 table)",
        "  ARE 2f • line clear 93f • BCD score $0166",
        "  Level: +10 lines, L9→10 +100 (Rev A)",
        "  Score max 999999 • Music in-game only",
        f"  OST loop {music_loop_label()} (DMG A-Type, files = off)",
        "",
        "Esc = back",
    ]
    y = 110
    for line in lines:
        f = fonts["menu"] if line.startswith("GAME") or line.startswith("ENGINE") else fonts["lbl"]
        surface.blit(f.render(line, True, COL_TEXT if not line.startswith(" ") else COL_DIM), (36, y))
        y += 22 if line.startswith(" ") else 26


def draw_game_over(surface: pygame.Surface, fonts: dict[str, pygame.font.Font], game: GameState):
    ov = pygame.Surface((GAME_W, GAME_H), pygame.SRCALPHA)
    ov.fill((*COL_GB_0, 160))
    surface.blit(ov, (0, 0))
    t1 = fonts["title"].render("GAME OVER", True, COL_GB_3)
    t2 = fonts["digit"].render(f"{game.score:06d}", True, COL_GB_3)
    t3 = fonts["lbl"].render("START=MENU  A=RETRY", True, COL_GB_2)
    surface.blit(t1, t1.get_rect(center=(GAME_W // 2, GAME_H // 2 - 28)))
    surface.blit(t2, t2.get_rect(center=(GAME_W // 2, GAME_H // 2 + 8)))
    surface.blit(t3, t3.get_rect(center=(GAME_W // 2, GAME_H // 2 + 40)))


def handle_menu_input(event: pygame.event.Event, menu: MenuState) -> str | None:
    if event.type != pygame.KEYDOWN:
        return None
    if event.key in (pygame.K_UP, pygame.K_w):
        menu.selected = (menu.selected - 1) % len(menu.items)
    elif event.key in (pygame.K_DOWN, pygame.K_s):
        menu.selected = (menu.selected + 1) % len(menu.items)
    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
        return menu.items[menu.selected]
    elif event.key == pygame.K_ESCAPE:
        return "EXIT"
    return None


def handle_game_keydown(event: pygame.event.Event, game: GameState) -> str | None:
    if event.key == pygame.K_ESCAPE:
        return "menu"
    if not game.alive:
        if event.key == pygame.K_r:
            return "retry"
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            return "menu"
        return None
    if event.key == pygame.K_p:
        game.paused = not game.paused
        return None
    if game.paused:
        return None
    if event.key in (pygame.K_LEFT, pygame.K_a):
        game.set_das(-1)
    elif event.key in (pygame.K_RIGHT, pygame.K_d):
        game.set_das(1)
    elif event.key in (pygame.K_DOWN, pygame.K_s):
        game.soft_drop = True
    elif event.key in (pygame.K_UP, pygame.K_z, pygame.K_x):
        game.try_rotate()
    return None


def handle_game_keyup(event: pygame.event.Event, game: GameState):
    if event.key in (pygame.K_LEFT, pygame.K_a) and game.das_dir == -1:
        game.stop_das()
    elif event.key in (pygame.K_RIGHT, pygame.K_d) and game.das_dir == 1:
        game.stop_das()
    elif event.key in (pygame.K_DOWN, pygame.K_s):
        game.soft_drop = False


def make_fonts() -> dict[str, pygame.font.Font]:
    return {
        "logo": pygame.font.SysFont("Courier New", 34, bold=True),
        "title": pygame.font.SysFont("Courier New", 26, bold=True),
        "menu": pygame.font.SysFont("Courier New", 20, bold=True),
        "digit": pygame.font.SysFont("Courier New", 22, bold=True),
        "lbl": pygame.font.SysFont("Courier New", 13, bold=True),
    }


def main():
    pygame.init()
    pygame.display.set_caption(APP_TITLE)
    menu = MenuState()
    game = GameState()
    music = MusicPlayer()
    screen = pygame.display.set_mode((MENU_W, MENU_H))
    clock = pygame.time.Clock()
    fonts = make_fonts()
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif menu.screen == "menu":
                action = handle_menu_input(event, menu)
                if action == "PLAY GAME":
                    game.reset(0)
                    menu.screen = "playing"
                    screen = pygame.display.set_mode((GAME_W, GAME_H))
                elif action == "HELP":
                    menu.screen = "help"
                elif action == "EXIT":
                    music.stop()
                    running = False
            elif menu.screen == "help":
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    menu.screen = "menu"
            elif menu.screen == "playing":
                if event.type == pygame.KEYDOWN:
                    result = handle_game_keydown(event, game)
                    if result == "menu":
                        menu.screen = "menu"
                        screen = pygame.display.set_mode((MENU_W, MENU_H))
                    elif result == "retry":
                        game.reset(0)
                        menu.screen = "playing"
                elif event.type == pygame.KEYUP:
                    handle_game_keyup(event, game)

        if menu.screen == "playing" and game.alive and not game.paused:
            prev_y = game.current.y if game.current else 0
            game.tick()
            if game.current and game.soft_drop and game.current.y > prev_y:
                game.rom.op_l0166_add_bcd(game.score_bcd, 1)

        if not game.alive and menu.screen == "playing":
            menu.screen = "gameover"

        if menu.screen == "playing" and game.alive and not game.paused:
            if not music.active:
                music.start()
            music.update()
        else:
            music.stop()

        if menu.screen == "menu":
            draw_main_menu(screen, fonts, menu)
        elif menu.screen == "help":
            draw_help(screen, fonts)
        elif menu.screen in ("playing", "gameover"):
            draw_board(screen, fonts, game)
            if menu.screen == "gameover":
                draw_game_over(screen, fonts, game)

        pygame.display.flip()
        clock.tick(TARGET_FPS)

    pygame.quit()


if __name__ == "__main__":
    main()
