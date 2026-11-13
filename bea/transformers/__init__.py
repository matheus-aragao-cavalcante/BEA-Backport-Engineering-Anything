"""Transformers: each turns one input file of the game into one output file of the mod.

The contract is in :mod:`bea.transformers.base`. :func:`default_transformers` returns the
set the command line uses, in planning order. The order matters only when two
transformers want to write the same output: the first one wins and the other is reported.
"""

from __future__ import annotations

from bea.toolbox import Toolbox
from bea.transformers.base import Transformer

__all__ = ["default_transformers"]


def default_transformers(tools: Toolbox | None = None) -> list[Transformer]:
    """Return one instance of every built-in transformer.

    ``tools`` is the toolbox used by transformers that run external programs.
    """
    # Imported here so that importing one transformer never loads the others.
    from bea.transformers.binary import BinaryTransformer
    from bea.transformers.config import ConfigTransformer
    from bea.transformers.media import MediaTransformer
    from bea.transformers.texture import TextureTransformer

    return [
        BinaryTransformer(),
        ConfigTransformer(),
        TextureTransformer(),
        MediaTransformer(tools),
    ]
