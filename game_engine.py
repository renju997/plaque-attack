"""Plaque Attack -- shared, pure game-logic module (no HTTP, no storage).

Ports the exact spawn/scoring rules from plaque-attack.html's <script> (see
HOLES/difficultyStep/randomFreeHole/popUp/scheduleSpawn/whack there) so a
submitted tap log can be authoritatively re-scored server-side, mirroring how
Football Fever's game_engine.py is shared between serve.py and api/game.py.

Unlike a turn-based game, whacking a mole here frees its hole up *before* its
natural timeout -- which changes which holes are eligible for the next spawn
pick. So the spawn schedule is NOT a pure function of the seed alone; it has
to be replayed as a single discrete-event simulation interleaving spawn ticks
with the player's own tap log, exactly as real time would on the client. The
client needs no equivalent of this: it already does this naturally, one event
at a time, via real setTimeout/pointerdown callbacks.

mulberry32() must stay byte-for-byte identical to its JS twin in
plaque-attack.html -- same call order in both is what makes a replay match.
"""

BOARD_HOLES = 12
ROUND_MS = 60000
GRACE_MS = 220          # matches the client's post-miss "still tappable" window
MAX_TAPS = 2000         # generous cap (well above any real 60s tap rate); defends against abuse

# Per-sprite base points, in the same [donut, chocolate bar, soda cup, fries,
# healthy tooth] cycle order as the client's SPRITES array (hole i uses
# SPRITES[i % 5] there). Must stay in lockstep with the `points` values on
# SPRITES in plaque-attack.html. The healthy tooth is a negative-points
# "avoid me" sprite -- whacking it is a mistake, not a scoring hit.
_SPRITE_CYCLE_PTS = [20, 20, 15, 10, -30]
HOLE_BASE_PTS = [_SPRITE_CYCLE_PTS[i % len(_SPRITE_CYCLE_PTS)] for i in range(BOARD_HOLES)]


def mulberry32(seed):
    state = seed & 0xFFFFFFFF

    def rng():
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = ((t ^ (t >> 15)) * (1 | t)) & 0xFFFFFFFF
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & 0xFFFFFFFF)) ^ t) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return rng


def _difficulty(time_left):
    elapsed = 60 - time_left
    speed_factor = min(elapsed / 55.0, 1.0)
    spawn_delay = (850 - speed_factor * 420) * 1.15
    up_time = (1150 - speed_factor * 620) * 1.15
    return spawn_delay, up_time


class _Hole:
    # `resolved` covers BOTH terminal outcomes of a pop -- whacked (hit) or timed
    # out (miss) -- once true, the hole is free for a new pop and can no longer
    # match a tap. Using one flag (rather than separate up/consumed checks)
    # avoids a boundary bug: at the exact instant t == natural_end, a "was it
    # up" check already excludes t (since it timed out), but a "was it
    # resolved" check correctly still fires the miss exactly once right there.
    __slots__ = ("start", "natural_end", "resolved")

    def __init__(self):
        self.start = None
        self.natural_end = None
        self.resolved = True  # no active pop yet

    def is_free(self, t):
        return self.resolved or t >= self.natural_end

    def is_clickable(self, t):
        return not self.resolved and self.start <= t <= self.natural_end + GRACE_MS


def run_session(seed, taps):
    """Replay one round from its seed, consuming a tap log
    ([{"hole": int, "t": ms-since-round-start}, ...]) exactly as the client's
    real-time whack()/miss()/scheduleSpawn() loop would, and return the
    server's own authoritative {"score", "hits", "misses"}.
    """
    clean_taps = sorted(
        (t for t in (taps or [])
         if isinstance(t, dict)
         and isinstance(t.get("hole"), int) and 0 <= t["hole"] < BOARD_HOLES
         and isinstance(t.get("t"), (int, float)) and 0 <= t["t"] <= ROUND_MS),
        key=lambda t: t["t"],
    )[:MAX_TAPS]

    rng = mulberry32(seed)
    holes = [_Hole() for _ in range(BOARD_HOLES)]
    tap_i = 0
    n_taps = len(clean_taps)

    combo = 0
    score = 0
    hits = 0
    misses = 0

    spawn_delay, up_time = 977.5, 1322.5
    next_spawn_t = spawn_delay
    t = 0.0

    def free_indices(at):
        return [i for i, h in enumerate(holes) if h.is_free(at)]

    def pop(i, at, time_left):
        nonlocal spawn_delay, up_time
        spawn_delay, up_time = _difficulty(time_left)
        up_for = up_time * (0.8 + rng() * 0.4)
        holes[i].start = at
        holes[i].natural_end = at + up_for
        holes[i].resolved = False

    while True:
        next_miss_t = min(
            (h.natural_end for h in holes if not h.resolved and t < h.natural_end),
            default=None,
        )
        next_tap_t = clean_taps[tap_i]["t"] if tap_i < n_taps else None
        candidates = [x for x in (next_spawn_t, next_miss_t, next_tap_t) if x is not None]
        if not candidates:
            break
        event_t = min(candidates)
        if event_t >= ROUND_MS:
            break
        t = event_t

        # ties resolve tap-first, so a tap landing in the same instant a hole
        # would otherwise naturally miss counts as the hit it visually was
        while tap_i < n_taps and clean_taps[tap_i]["t"] <= t:
            tap = clean_taps[tap_i]
            tap_i += 1
            h = holes[tap["hole"]]
            if h.is_clickable(t):
                h.resolved = True
                base_pts = HOLE_BASE_PTS[tap["hole"]]
                if base_pts < 0:
                    # Healthy tooth: a mistake, not a combo hit -- breaks the
                    # streak instead of extending it, no multiplier applied.
                    combo = 0
                    score += base_pts
                else:
                    combo += 1
                    mult = 3 if combo >= 8 else 2 if combo >= 4 else 1
                    score += base_pts * mult
                hits += 1
            # an unmatched tap is simply ignored, exactly like the client's
            # board listener finding zero clickable candidates at that point

        for h in holes:
            if not h.resolved and t >= h.natural_end:
                h.resolved = True
                misses += 1
                combo = 0

        if event_t == next_spawn_t:
            time_left = max(0, 60 - int(t // 1000))
            free = free_indices(t)
            if free:
                pop(free[int(rng() * len(free))], t, time_left)
            double_chance = min((60 - time_left) / 90.0, 0.35)
            if rng() < double_chance:
                free2 = free_indices(t)
                if free2:
                    pop(free2[int(rng() * len(free2))], t, time_left)
            next_spawn_t = t + spawn_delay

    return {"score": score, "hits": hits, "misses": misses}
