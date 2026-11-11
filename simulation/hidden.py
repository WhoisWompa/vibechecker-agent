"""The hidden answer key the agent never sees: f(t), closeness, hazard."""

from dataclasses import dataclass
import math
import random
from typing import ClassVar


def S(t: float) -> float:
    """Sun arc, t in minutes since 8am (sunrise 7am, sunset 7:15pm)."""
    return math.sin(math.pi * (t + 60) / 735) if -60 <= t <= 675 else 0.0


def bump(t: float, c: float, w: float) -> float:
    """Smooth hill centred at c, width w minutes."""
    return math.exp(-((t - c) ** 2) / (2 * w * w))


@dataclass(frozen=True)
class HiddenParams:
    """One roll per run; held fixed for every day of that run."""

    base_temp: float  # baseline ideal temp, degC
    midday_temp_dip: float
    lunch_temp_dip: float
    evening_temp_dip: float
    base_light: float  # baseline ideal light, lux
    midday_light_dip: float
    base_volume: float  # baseline ideal volume, dB
    lunch_volume_lift: float
    evening_volume_lift: float
    lunch_centre: float  # bump centres, minutes since 8am
    evening_centre: float
    co2_comfort: float  # ppm; stuffiness penalty starts here
    humidity_comfort: float  # %RH

    # Default (lo, hi) per parameter, in draw order. roll() draws one uniform
    # per row in this order, so the same seed gives the same non-overridden
    # values whether or not `ranges` narrows some of the others.
    DEFAULT_RANGES: ClassVar[tuple[tuple[str, tuple[float, float]], ...]] = (
        ("base_temp", (22, 26)),
        ("midday_temp_dip", (1, 4)),
        ("lunch_temp_dip", (0.5, 1.5)),
        ("evening_temp_dip", (0.5, 1.5)),
        ("base_light", (450, 800)),
        ("midday_light_dip", (250, 600)),
        ("base_volume", (58, 68)),
        ("lunch_volume_lift", (5, 15)),
        ("evening_volume_lift", (8, 20)),
        ("lunch_centre", (210, 300)),
        ("evening_centre", (660, 780)),
        ("co2_comfort", (600, 800)),
        ("humidity_comfort", (75, 80)),
    )

    @staticmethod
    def roll(
        rng: random.Random,
        ranges: dict[str, tuple[float, float]] | None = None,
    ) -> "HiddenParams":
        """One uniform draw per parameter. `ranges` replaces the (lo, hi) of
        any named parameter; the others keep their defaults."""
        ranges = ranges or {}
        u = rng.uniform
        return HiddenParams(**{
            name: u(*ranges.get(name, default))
            for name, default in HiddenParams.DEFAULT_RANGES
        })


def f(t: float, p: HiddenParams) -> dict[str, float]:
    """Ideal room conditions at minute t. Genre is outside f."""
    return {
        "temperature": p.base_temp - p.midday_temp_dip * S(t - 60)
        - p.lunch_temp_dip * bump(t, p.lunch_centre, 45)
        - p.evening_temp_dip * bump(t, p.evening_centre, 45),
        "light": p.base_light - p.midday_light_dip * S(t),
        "volume": p.base_volume + p.lunch_volume_lift * bump(t, p.lunch_centre, 45)
        + p.evening_volume_lift * bump(t, p.evening_centre, 75),
        "co2": p.co2_comfort,
        "humidity": p.humidity_comfort,
    }


# Only excess above the ideal counts — fresh air is neutral, never a bonus.
ONE_SIDED = frozenset({"co2", "humidity"})


def closeness(c: dict[str, float], ideal: dict[str, float],
              weights: dict[str, float]) -> float:
    """exp(-half the weighted squared distance); 1 at the ideal, falls to 0.

    Only dimensions named in `weights` are compared.
    """
    d2 = sum(
        w * (max(0.0, c[k] - ideal[k]) if k in ONE_SIDED else c[k] - ideal[k]) ** 2
        for k, w in weights.items()
    )
    return math.exp(-0.5 * d2)


def hazard(cl: float, p_min: float, p_max: float) -> float:
    """Per-timestep leave probability for a seated party, from closeness cl."""
    return p_min + (p_max - p_min) * (1 - cl)


def crowd_comfort(x: float, k: float, knee: float) -> float:
    """Multiplies closeness in the hazard. 1 below knee*k seated; 0 at full."""
    return 1 - max(0.0, (x / k - knee) / (1 - knee)) ** 2
