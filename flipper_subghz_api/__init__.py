"""Token-protected LAN API that asks a USB-attached Flipper Zero to send Sub-GHz files that
are already saved on it, chosen from a fixed allowlist. It reports whether a
*transmission request* was sent/failed/unknown -- never the state of the device."""

__version__ = "0.2.0"
