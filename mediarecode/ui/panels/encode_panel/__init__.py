"""
ui/panels/encode_panel — Encode panel package.

Re-exports EncodePanel so that existing imports of the form
    from ui.panels.encode_panel import EncodePanel
continue to work unchanged.
"""

from ui.panels.encode_panel.panel import EncodePanel

__all__ = ["EncodePanel"]
