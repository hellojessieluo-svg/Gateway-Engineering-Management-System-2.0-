"""A shared toolbox for both gates: errors, naming, validation, addresses, config, constants.

Moved out of outbound_gate\\ on 2026-09-03. No gate's business logic lives here --
the intake gate and the issue gate both import it, and it imports neither gate.
"""

__all__ = ["constants", "config", "errors", "naming", "places", "validators"]
