"""Gateway's JSON bridge: lets the web UI call the business logic of the two gates.

The UI is a local web page (ui/index.html) served by gate_api.server; all business logic
stays in inbound_gate / outbound_gate. This package only translates, it holds no rules.
"""

__all__ = ["cli", "server"]
__version__ = "1.0.0"
