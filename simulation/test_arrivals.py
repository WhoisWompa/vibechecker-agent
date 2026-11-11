"""Self-check for the compound-Poisson arrival process. Run: python -m simulation.test_arrivals"""

import random

from .env import _parties, _poisson
from .settings import GROUP_SIZES

rng = random.Random(0)
N = 20000

# Poisson mean is lam.
lam = 2.0
assert abs(sum(_poisson(rng, lam) for _ in range(N)) / N - lam) < 0.05

# Parties per timestep averages lam; people per timestep averages lam * E[size].
draws = [_parties(rng, lam, GROUP_SIZES) for _ in range(N)]
weight = sum(GROUP_SIZES.values())
e_size = sum(s * w for s, w in GROUP_SIZES.items()) / weight

assert abs(sum(len(d) for d in draws) / N - lam) < 0.05
assert abs(sum(sum(d) for d in draws) / N - lam * e_size) < 0.1
assert all(s in GROUP_SIZES for d in draws for s in d)

# Groups must be lumpier than the same number of people arriving alone.
people = [sum(d) for d in draws]
mean = sum(people) / N
var = sum((x - mean) ** 2 for x in people) / N
assert var > mean * 1.2, "compound Poisson should be overdispersed"

print("ok")
