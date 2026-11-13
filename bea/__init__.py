"""BEA (Backport Engineering Anything).

A clean-room, game-agnostic pipeline: ingest an extracted game folder, analyze it,
transform assets / configs / binaries as declared by a per-game profile, and package
the result as a LayeredFS mod.

Nothing in this package may name a specific game: no title IDs, no build IDs, no game
names. Everything game-specific is declarative data that lives outside of this package
(see ``docs/ARCHITECTURE.md`` and ``tests/test_arch_rules.py``).
"""

__version__ = "0.1.0"
