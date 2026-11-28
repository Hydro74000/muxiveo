"""Compatibilité : implémentation dans ``core.jobs.output_template`` (partagée avec la couche de service)."""

from __future__ import annotations

from core.jobs.output_template import *  # noqa: F401,F403
from core.jobs.output_template import _audio_fr_tag as _audio_fr_tag  # noqa: F401 - utilisé par les tests
