"""Hardware backend for the web app — real Mini-Circuits switches only.

Wraps :class:`SwitchBank` and connects **lazily** so the server can start even
if the switch is unplugged; the connection is made (and an identity probe run)
on the first request. If the hardware is absent or the USB link is denied, the
API returns a real error — there is no simulation/fallback.

Each dual-SP4T unit has COM channels **A** and **B**, each selectable to ports
**1-4**.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Dict, List, Union

# Repo root (holds mini_circuits_switch.py) on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHANNELS = ("A", "B")
PORTS = (1, 2, 3, 4)          # dual-SP4T: each COM -> one of 4 ports
SW_TYPE = "SP4T"


class HardwareUnavailable(Exception):
    """No reachable switch (not plugged in, or USB permission denied)."""


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "access denied" in low or "permission" in low:
        return ("USB permission denied — install "
                "packaging/99-mini-circuits-switch.rules (or run with sudo).")
    if "no mini-circuits" in low or "no switches" in low:
        return "No Mini-Circuits switch detected on the USB bus."
    if "busy" in low or "errno 16" in low:
        return ("The switch is busy — another program or app instance already "
                "holds it. Close that process (e.g. a running probe / another "
                "app.py) and reconnect.")
    return msg


def _release_all():
    """Best-effort release of every matching device handle (clears Errno 16)."""
    try:
        import usb.util
        from mini_circuits_switch import SolidStateSwitch
        for dev in SolidStateSwitch._iter_raw_devices():
            usb.util.dispose_resources(dev)
    except Exception:
        pass


class SwitchService:
    """Lazily-connected, thread-safe gateway to the connected switches."""

    def __init__(self):
        self._bank = None
        self._lock = threading.RLock()
        self._identity = {}  # serial -> {"serial","model","firmware"} cache

    # ----- connection ----------------------------------------------------- #
    def _connect_locked(self):
        try:
            from mini_circuits_switch import SwitchBank
        except Exception as exc:  # pyusb / libusb missing
            raise HardwareUnavailable(f"pyusb/libusb not available: {exc}") from exc

        last = None
        for attempt in range(2):
            try:
                bank = SwitchBank.discover()
                models = bank.models()      # identity probe — proves the link answers
                fws = bank.firmwares()
                for s in bank.serials:      # cache identity for the live-status poll
                    self._identity[s] = {
                        "serial": s,
                        "model": str(models.get(s, "")),
                        "firmware": str(fws.get(s, "")),
                    }
                self._bank = bank
                return bank
            except Exception as exc:
                last = exc
                # A stale handle (Errno 16 busy) clears once we release it; retry once.
                _release_all()
        raise HardwareUnavailable(_friendly(last))

    def _ensure(self):
        with self._lock:
            if self._bank is None:
                return self._connect_locked()
            return self._bank

    def _invalidate(self):
        with self._lock:
            if self._bank is not None:
                try:
                    self._bank.close()
                except Exception:
                    pass
                self._bank = None

    def reconnect(self):
        """Drop any existing handles and connect fresh (after (re)plugging)."""
        with self._lock:
            self._invalidate()
            self._connect_locked()
            return self.info()

    def health(self) -> Dict[str, object]:
        """Live, per-unit connection state — actually probes the hardware.

        Each call (1) sends a cheap query to every held unit and drops the ones
        that no longer answer (unplugged), and (2) compares the number of units
        physically on the USB bus against the number we hold, so a freshly
        (re)plugged unit is picked up automatically. Never raises.
        """
        from mini_circuits_switch import SolidStateSwitch
        with self._lock:
            # Lazily open if we hold nothing yet (best effort, never raises).
            if self._bank is None:
                try:
                    self._connect_locked()
                except HardwareUnavailable:
                    pass

            present = []
            bank = self._bank
            if bank is not None:
                dead = []
                for serial, sw in list(bank.switches.items()):
                    try:
                        fw = sw.firmware()  # cheap liveness query
                        self._identity.setdefault(
                            serial, {"serial": serial, "model": "", "firmware": ""}
                        )["firmware"] = str(fw)
                        present.append(serial)
                    except Exception:
                        dead.append(serial)
                for serial in dead:          # release handles for unplugged units
                    sw = bank.switches.pop(serial, None)
                    if sw is not None:
                        try:
                            sw.close()
                        except Exception:
                            pass
                if not bank.switches:
                    self._invalidate()

            # Replug / new-unit detection: more devices on the bus than we hold?
            try:
                bus_count = len(SolidStateSwitch._iter_raw_devices())
            except Exception:
                bus_count = len(present)
            if bus_count > len(present):
                self._invalidate()
                _release_all()
                try:
                    self._connect_locked()
                    present = list(self._bank.serials)
                except Exception:
                    present = []

            units = [
                dict(self._identity.get(s, {"serial": s, "model": "", "firmware": ""}))
                for s in present
            ]
            return {
                "connected": bool(present),
                "count": len(present),
                "units": units,
                "error": "" if present else "No reachable Mini-Circuits switch.",
            }

    def status(self) -> Dict[str, object]:
        """Compact connection state (delegates to the live probe)."""
        h = self.health()
        return {"connected": h["connected"], "units": h["count"], "error": h["error"]}

    def _run(self, fn):
        """Run an operation; on a transport error, drop the handle and re-raise."""
        with self._lock:
            bank = self._ensure()
            try:
                return fn(bank)
            except HardwareUnavailable:
                raise
            except Exception as exc:
                self._invalidate()
                raise HardwareUnavailable(_friendly(exc)) from exc

    # ----- operations ----------------------------------------------------- #
    def serials(self) -> List[str]:
        return self._run(lambda b: list(b.serials))

    def info(self) -> Dict[str, Dict[str, str]]:
        def _info(b):
            models, fws = b.models(), b.firmwares()
            return {
                s: {"serial": s, "model": str(models.get(s, "")), "firmware": str(fws.get(s, ""))}
                for s in b.serials
            }
        return self._run(_info)

    def states(self) -> Dict[str, Dict[str, int]]:
        # Read each unit independently and skip any that no longer answer, so one
        # unplugged unit doesn't blank out the others (or invalidate the bank).
        def _states(b):
            out: Dict[str, Dict[str, int]] = {}
            for serial, sw in list(b.switches.items()):
                try:
                    out[serial] = sw.get_ab(SW_TYPE)
                except Exception:
                    continue
            return out
        return self._run(_states)

    def set_state(self, serial: str, a=None, b=None) -> Dict[str, int]:
        want: Dict[str, Union[int, str]] = {}
        if a is not None:
            want["A"] = a
        if b is not None:
            want["B"] = b

        def _set(bank):
            bank.set_states({serial: want}, SW_TYPE)
            return bank[serial].get_ab(SW_TYPE)
        return self._run(_set)

    def set_all(self, a=None, b=None) -> Dict[str, Dict[str, int]]:
        def _set(bank):
            bank.set_all(a=a, b=b, sw_type=SW_TYPE)
            return bank.get_states(SW_TYPE)
        return self._run(_set)

    def apply_mapping(self, mapping: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
        """Apply a saved preset: ``{serial: {"A": port, "B": port}}``.

        Only routes channels whose value is a real port (1-4) and only for units
        that are currently connected; a `0`/open or an absent unit is skipped.
        """
        def _set(bank):
            want: Dict[str, Dict[str, int]] = {}
            for serial, st in (mapping or {}).items():
                if serial not in bank.switches:
                    continue
                chans = {ch: st[ch] for ch in ("A", "B")
                         if isinstance(st.get(ch), int) and st[ch] in PORTS}
                if chans:
                    want[serial] = chans
            if want:
                bank.set_states(want, SW_TYPE)
            out: Dict[str, Dict[str, int]] = {}
            for serial, sw in list(bank.switches.items()):
                try:
                    out[serial] = sw.get_ab(SW_TYPE)
                except Exception:
                    continue
            return out
        return self._run(_set)

    def send_scpi(self, serial: str, command: str) -> str:
        return self._run(lambda b: b[serial].send_scpi(command))

    def close(self) -> None:
        self._invalidate()
