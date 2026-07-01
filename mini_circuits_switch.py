"""mini_circuits_switch
=======================

Pure-USB Python library for Mini-Circuits Solid-State Switches
(USB HID, idVendor=0x20CE, idProduct=0x0022) such as the dual-SP4T units.

The library has two layers:

* :class:`SolidStateSwitch` -- controls **one** physical unit. It exposes
  every SCPI command documented in the Programming Manual
  (Prog_Manual-Solid_State_Switch.pdf): identity, switch state set/get for
  every channel (A, B, ...), the full sequence (``:SEQ:...``) engine, the
  Ethernet configuration block (``:ETHERNET:CONFIG:...``), unit addressing,
  and a raw :meth:`~SolidStateSwitch.send_scpi` escape hatch.

* :class:`SwitchBank` -- manages several units at once and drives them
  **in parallel** (one OS thread per unit, each unit on its own USB handle).
  This is the layer you want for "two dual-SP4T units, channels A & B".

Because both units share the same VID/PID, individual units are addressed by
their **serial number** over independent USB handles -- never by a shared SCPI
bus address -- so a command sent to one unit can never reach the other.

Transport
---------
Commands are ASCII SCPI strings written to USB interrupt OUT endpoint ``0x01``;
replies are read from interrupt IN endpoint ``0x81`` as a 64-byte HID report
whose first byte is the report id (skipped). This matches the behaviour proven
in ``set-get-state.py`` / ``demo-minisw.py``.

Requires: ``pyusb`` (``pip install pyusb``) and a libusb backend.
On Linux you typically need a udev rule or root to claim the HID interface.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple, Union

import usb.core
import usb.util

__all__ = [
    "VENDOR_ID",
    "PRODUCT_ID",
    "SwitchError",
    "SolidStateSwitch",
    "SwitchBank",
]

VENDOR_ID = 0x20CE
PRODUCT_ID = 0x0022

# USB endpoints (Mini-Circuits HID convention).
_EP_OUT = 0x01
_EP_IN = 0x81
_REPORT_LEN = 64

# Default SCPI address prefix. "*" is the address-agnostic prefix used in the
# manual's USB examples (e.g. "*:SP4T:A:STATE?"). With one USB handle per unit
# the prefix is cosmetic -- the handle already selects the unit -- but we keep
# the proven form.
_DEFAULT_ADDR = "*"


class SwitchError(Exception):
    """Raised for device-not-found, communication, or command errors."""


# --------------------------------------------------------------------------- #
#  Single unit
# --------------------------------------------------------------------------- #
class SolidStateSwitch:
    """Control a single Mini-Circuits solid-state switch over USB.

    Parameters
    ----------
    serial:
        Serial number of the unit to open. If ``None`` the first matching
        device on the bus is opened (fine when only one unit is present).
    dev:
        An already-resolved ``usb.core.Device`` to wrap (used internally by
        :class:`SwitchBank`); takes precedence over ``serial``.
    addr:
        SCPI address prefix for every command (default ``"*"``).

    Examples
    --------
    >>> sw = SolidStateSwitch(serial="12208010025")
    >>> sw.model_name()
    'RC-2SPDT-A18'
    >>> sw.set_state(2, channel="A")      # COM A -> port 2
    >>> sw.get_state(channel="A")
    2
    >>> sw.close()

    Can also be used as a context manager::

        with SolidStateSwitch() as sw:
            sw.set_state(1, "A")
    """

    def __init__(
        self,
        serial: Optional[str] = None,
        *,
        dev: Optional["usb.core.Device"] = None,
        addr: str = _DEFAULT_ADDR,
    ) -> None:
        self.addr = addr
        self._lock = threading.Lock()  # serialise access to this USB handle
        self._closed = False

        if dev is None:
            dev = self._find_device(serial)
        self.dev = dev
        self._claim(dev)

        # Cache identity (and validate the link works).
        self._serial = serial
        try:
            self._serial = self.serial_number()
        except Exception:  # pragma: no cover - keep ctor resilient
            pass

    # ----- discovery / connection ----------------------------------------- #
    @staticmethod
    def _find_device(serial: Optional[str]) -> "usb.core.Device":
        if serial is None:
            dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            if dev is None:
                raise SwitchError(
                    f"No Mini-Circuits switch found "
                    f"(VID={VENDOR_ID:#06x} PID={PRODUCT_ID:#06x})."
                )
            return dev

        for cand in SolidStateSwitch._iter_raw_devices():
            tmp = SolidStateSwitch.__new__(SolidStateSwitch)
            tmp.addr = _DEFAULT_ADDR
            tmp._lock = threading.Lock()
            tmp._closed = False
            matched = False
            try:
                SolidStateSwitch._claim(cand)
                tmp.dev = cand
                matched = tmp.serial_number() == str(serial)
            except Exception:
                matched = False
            if matched:
                return cand
            # Release the probe handle so the caller can re-open this unit.
            usb.util.dispose_resources(cand)
        raise SwitchError(f"No Mini-Circuits switch with serial {serial!r} found.")

    @staticmethod
    def _iter_raw_devices() -> List["usb.core.Device"]:
        found = usb.core.find(
            find_all=True, idVendor=VENDOR_ID, idProduct=PRODUCT_ID
        )
        return list(found) if found is not None else []

    @staticmethod
    def _claim(dev: "usb.core.Device") -> None:
        """Detach any kernel driver and set the active configuration."""
        for configuration in dev:
            for interface in configuration:
                ifnum = interface.bInterfaceNumber
                try:
                    if dev.is_kernel_driver_active(ifnum):
                        dev.detach_kernel_driver(ifnum)
                except (usb.core.USBError, NotImplementedError):
                    pass
        try:
            dev.set_configuration()
        except usb.core.USBError:
            # Already configured -- benign when another handle set it up.
            pass

    @classmethod
    def list_serials(cls) -> List[str]:
        """Return the serial numbers of every connected unit.

        >>> SolidStateSwitch.list_serials()
        ['12208010025', '12208010026']
        """
        serials: List[str] = []
        for dev in cls._iter_raw_devices():
            tmp = cls.__new__(cls)
            tmp.addr = _DEFAULT_ADDR
            tmp._lock = threading.Lock()
            tmp._closed = False
            try:
                cls._claim(dev)
                tmp.dev = dev
                serials.append(tmp.serial_number())
            except Exception:
                continue
            finally:
                # Release the probe handle, else the next open() -> Errno 16 busy.
                usb.util.dispose_resources(dev)
        return serials

    # ----- low level transport --------------------------------------------- #
    def send_scpi(self, command: str) -> str:
        """Send a raw SCPI command/query and return the textual reply.

        This is the universal escape hatch -- any command in the manual can be
        issued through it, e.g. ``send_scpi("SP4T:A:STATE?")``. The configured
        address prefix is added automatically if the command does not already
        start with an address (``*`` or a digit) or a leading colon.
        """
        if self._closed:
            raise SwitchError("Switch handle is closed.")
        wire = self._wire(command)
        with self._lock:
            try:
                self.dev.write(_EP_OUT, wire)
                resp = self.dev.read(_EP_IN, _REPORT_LEN)
            except usb.core.USBError as exc:
                raise SwitchError(f"USB error sending {wire!r}: {exc}") from exc
        return self._decode(resp)

    def _wire(self, command: str) -> str:
        cmd = command.strip()
        if cmd[:1] in ("*", ":") or cmd[:1].isdigit():
            return cmd
        return f"{self.addr}:{cmd}"

    @staticmethod
    def _decode(resp: Sequence[int]) -> str:
        # Byte 0 is the HID report id; payload is null/0xFF terminated ASCII.
        out = []
        for byte in resp[1:]:
            if byte <= 0 or byte >= 255:
                break
            out.append(chr(byte))
        return "".join(out)

    # ----- identity --------------------------------------------------------- #
    def model_name(self) -> str:
        """``:MN?`` -- model name, e.g. ``RC-2SPDT-A18``."""
        return self._strip(self.send_scpi("MN?"), "MN=")

    def serial_number(self) -> str:
        """``:SN?`` -- serial number."""
        return self._strip(self.send_scpi("SN?"), "SN=")

    def firmware(self) -> str:
        """``:FIRMWARE?`` -- firmware revision string."""
        return self.send_scpi("FIRMWARE?")

    @staticmethod
    def _strip(value: str, prefix: str) -> str:
        return value[len(prefix):] if value.startswith(prefix) else value

    # ----- unit addressing (daisy-chain / multi-unit) ---------------------- #
    def assign_addresses(self) -> str:
        """``:AssignAddresses`` -- auto-assign addresses to chained units."""
        return self.send_scpi("AssignAddresses")

    def number_of_slaves(self) -> int:
        """``:NumberOfSlaves?`` -- count of chained slave units."""
        return int(self.send_scpi("NumberOfSlaves?"))

    # ----- switching -------------------------------------------------------- #
    def set_state(
        self,
        state: Union[int, str],
        channel: Optional[str] = None,
        sw_type: str = "SP4T",
    ) -> str:
        """Set a switch channel state.

        SCPI: ``:[Sw_Type]:[Sw_Channel]:STATE:[state]``

        Parameters
        ----------
        state:
            Target port/state. For an SP4T this is ``1``-``4``; for an SPDT
            ``1``/``2``; an SP16T accepts ``1``-``16``, etc.
        channel:
            ``"A"`` or ``"B"`` on multi-channel units. Pass ``None`` on
            single-channel models that omit the channel field.
        sw_type:
            Switch type token (``"SP4T"``, ``"SPDT"``, ``"SP6T"``, ``"SP8T"``,
            ``"SP16T"``, ``"SP2T"`` ...). Defaults to ``"SP4T"``.

        Returns the device acknowledgement (``"1"`` on success).
        """
        body = self._state_body(sw_type, channel) + f":STATE:{state}"
        return self.send_scpi(body)

    def get_state(
        self,
        channel: Optional[str] = None,
        sw_type: str = "SP4T",
    ) -> int:
        """Query a switch channel state -> ``int``.

        SCPI: ``:[Sw_Type]:[Sw_Channel]:STATE?``
        """
        body = self._state_body(sw_type, channel) + ":STATE?"
        reply = self.send_scpi(body)
        # Multi-unit replies may be prefixed "AA:state"; take the last field.
        return int(reply.split(":")[-1])

    @staticmethod
    def _state_body(sw_type: str, channel: Optional[str]) -> str:
        return f"{sw_type}:{channel}" if channel else sw_type

    # Convenience wrappers for the common dual-SP4T A/B layout ------------- #
    def set_a(self, state: Union[int, str], sw_type: str = "SP4T") -> str:
        """Set COM **A** (shortcut for ``set_state(state, "A")``)."""
        return self.set_state(state, "A", sw_type)

    def set_b(self, state: Union[int, str], sw_type: str = "SP4T") -> str:
        """Set COM **B** (shortcut for ``set_state(state, "B")``)."""
        return self.set_state(state, "B", sw_type)

    def get_a(self, sw_type: str = "SP4T") -> int:
        """Get COM **A** state."""
        return self.get_state("A", sw_type)

    def get_b(self, sw_type: str = "SP4T") -> int:
        """Get COM **B** state."""
        return self.get_state("B", sw_type)

    def set_ab(
        self,
        state_a: Union[int, str],
        state_b: Union[int, str],
        sw_type: str = "SP4T",
    ) -> Tuple[str, str]:
        """Set COM A then COM B on this unit (single USB link -> sequential).

        For simultaneous switching *across units* use :class:`SwitchBank`,
        which runs one thread per unit.
        """
        return self.set_a(state_a, sw_type), self.set_b(state_b, sw_type)

    def get_ab(self, sw_type: str = "SP4T") -> Dict[str, int]:
        """Return ``{"A": stateA, "B": stateB}`` for this unit."""
        return {"A": self.get_a(sw_type), "B": self.get_b(sw_type)}

    # ----- sequence engine (:SEQ:...) -------------------------------------- #
    def seq_set_steps(self, steps: int) -> str:
        """``:SEQ:STEPS:[steps]`` -- number of steps in the sequence."""
        return self.send_scpi(f"SEQ:STEPS:{steps}")

    def seq_get_steps(self) -> int:
        """``:SEQ:STEPS?``"""
        return int(self.send_scpi("SEQ:STEPS?"))

    def seq_set_step(self, index: int) -> str:
        """``:SEQ:STEP:[index]`` -- select the active step to edit/read."""
        return self.send_scpi(f"SEQ:STEP:{index}")

    def seq_get_step(self) -> int:
        """``:SEQ:STEP?``"""
        return int(self.send_scpi("SEQ:STEP?"))

    def seq_set_state(self, *state: Union[int, str]) -> str:
        """``:SEQ:STATE:[state]`` -- state(s) for the current step.

        Accepts either a single combined index (``seq_set_state(8)``) or the
        per-channel form (``seq_set_state(1, 2, 2, 1)`` -> ``1:2:2:1``).
        """
        joined = ":".join(str(s) for s in state)
        return self.send_scpi(f"SEQ:STATE:{joined}")

    def seq_get_state(self) -> str:
        """``:SEQ:STATE?`` -- raw state string for the current step."""
        return self.send_scpi("SEQ:STATE?")

    def seq_set_dwell_time(self, time: int) -> str:
        """``:SEQ:DWELLTIME:[time]`` -- dwell time (in current units)."""
        return self.send_scpi(f"SEQ:DWELLTIME:{time}")

    def seq_get_dwell_time(self) -> int:
        """``:SEQ:DWELLTIME?``"""
        return int(self.send_scpi("SEQ:DWELLTIME?"))

    def seq_set_dwell_units(self, units: str) -> str:
        """``:SEQ:DWELLUNITS:[units]`` -- ``"U"`` µs, ``"M"`` ms, ``"S"`` s."""
        units = units.upper()
        if units not in ("U", "M", "S"):
            raise ValueError("dwell units must be 'U' (us), 'M' (ms) or 'S' (s)")
        return self.send_scpi(f"SEQ:DWELLUNITS:{units}")

    def seq_get_dwell_units(self) -> str:
        """``:SEQ:DWELLUNITS?`` -> ``"U"``/``"M"``/``"S"``."""
        return self.send_scpi("SEQ:DWELLUNITS?")

    def seq_set_cycles(self, count: int) -> str:
        """``:SEQ:CYCLES:[count]`` -- number of times to run the sequence."""
        return self.send_scpi(f"SEQ:CYCLES:{count}")

    def seq_get_cycles(self) -> int:
        """``:SEQ:CYCLES?``"""
        return int(self.send_scpi("SEQ:CYCLES?"))

    def seq_set_direction(self, mode: int) -> str:
        """``:SEQ:DIRECTION:[mode]`` -- 0=forward, 1=reverse, 2=bidirectional."""
        return self.send_scpi(f"SEQ:DIRECTION:{mode}")

    def seq_get_direction(self) -> int:
        """``:SEQ:DIRECTION?``"""
        return int(self.send_scpi("SEQ:DIRECTION?"))

    def seq_set_mode(self, on: Union[bool, str]) -> str:
        """``:SEQ:MODE:[ON|OFF]`` -- start/stop the stored sequence.

        Accepts ``True``/``False`` or the strings ``"ON"``/``"OFF"``.
        """
        if isinstance(on, str):
            token = on.upper()
        else:
            token = "ON" if on else "OFF"
        return self.send_scpi(f"SEQ:MODE:{token}")

    def seq_get_mode(self) -> str:
        """``:SEQ:MODE?`` -- ``"ON"``/``"OFF"`` (continuous-mode state).

        Mirrors the DLL ``GetSequence_ContinuousMode``. The manual lists this
        only in the binary byte-0 command table, so some firmware may not
        answer the ASCII query; treat a non-``ON``/``OFF`` reply as unsupported.
        """
        return self.send_scpi("SEQ:MODE?")

    # Composite step programming (DLL ``SetSequence_Step`` parity) ---------- #
    # Dwell-unit integers follow the DLL convention 0=µs, 1=ms, 2=s.
    _DWELL_UNIT_BY_INT = {0: "U", 1: "M", 2: "S"}
    _DWELL_INT_BY_UNIT = {"U": 0, "M": 1, "S": 2}

    def set_sequence_step(
        self,
        step_no: int,
        switch_to: Union[int, str],
        dwell: int,
        dwell_units: Union[int, str] = 0,
    ) -> str:
        """Program one whole sequence step in a single call.

        DLL: ``SetSequence_Step(StepNo, SwitchTo, Dwell, DwellUnits)``. Selects
        the step, then sets its state, dwell time and dwell units. ``dwell_units``
        accepts the DLL integer (``0``=µs, ``1``=ms, ``2``=s) or the SCPI letter
        (``"U"``/``"M"``/``"S"``). Returns the last acknowledgement.
        """
        if isinstance(dwell_units, int):
            try:
                unit = self._DWELL_UNIT_BY_INT[dwell_units]
            except KeyError:
                raise ValueError("dwell_units int must be 0 (us), 1 (ms) or 2 (s)")
        else:
            unit = dwell_units.upper()
        self.seq_set_step(step_no)
        self.seq_set_state(switch_to)
        self.seq_set_dwell_time(dwell)
        return self.seq_set_dwell_units(unit)

    def get_sequence_switch_to(self, step_no: int) -> str:
        """DLL ``GetSequence_SwitchTo(StepNo)`` -- state string of a step."""
        self.seq_set_step(step_no)
        return self.seq_get_state()

    def get_sequence_dwell(self, step_no: int) -> int:
        """DLL ``GetSequence_Dwell(StepNo)`` -- dwell time of a step."""
        self.seq_set_step(step_no)
        return self.seq_get_dwell_time()

    def get_sequence_dwell_units(self, step_no: int) -> int:
        """DLL ``GetSequence_DwellUnits(StepNo)`` -> ``0``/``1``/``2`` (µs/ms/s)."""
        self.seq_set_step(step_no)
        return self._DWELL_INT_BY_UNIT.get(self.seq_get_dwell_units().strip().upper(), -1)

    # ----- Ethernet configuration (:ETHERNET:CONFIG:...) ------------------- #
    # Present for completeness; informational on USB-only units.
    def eth_listen(self) -> str:
        """``:ETHERNET:CONFIG:LISTEN?`` -> ``ip;mask;gateway``."""
        return self.send_scpi("ETHERNET:CONFIG:LISTEN?")

    def eth_get_mac(self) -> str:
        """``:ETHERNET:CONFIG:MAC?``"""
        return self.send_scpi("ETHERNET:CONFIG:MAC?")

    def eth_get_dhcp(self) -> int:
        """``:ETHERNET:CONFIG:DHCPENABLED?``"""
        return int(self.send_scpi("ETHERNET:CONFIG:DHCPENABLED?"))

    def eth_set_dhcp(self, enabled: Union[bool, int]) -> str:
        """``:ETHERNET:CONFIG:DHCPENABLED:[0|1]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:DHCPENABLED:{int(bool(enabled))}")

    def eth_get_ip(self) -> str:
        """``:ETHERNET:CONFIG:IP?``"""
        return self.send_scpi("ETHERNET:CONFIG:IP?")

    def eth_set_ip(self, ip: str) -> str:
        """``:ETHERNET:CONFIG:IP:[ip]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:IP:{ip}")

    def eth_get_gateway(self) -> str:
        """``:ETHERNET:CONFIG:NG?`` -- network gateway."""
        return self.send_scpi("ETHERNET:CONFIG:NG?")

    def eth_set_gateway(self, gateway: str) -> str:
        """``:ETHERNET:CONFIG:NG:[gateway]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:NG:{gateway}")

    def eth_get_subnet(self) -> str:
        """``:ETHERNET:CONFIG:SM?`` -- subnet mask."""
        return self.send_scpi("ETHERNET:CONFIG:SM?")

    def eth_set_subnet(self, mask: str) -> str:
        """``:ETHERNET:CONFIG:SM:[mask]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:SM:{mask}")

    def eth_get_http_port(self) -> int:
        """``:ETHERNET:CONFIG:HTPORT?``"""
        return int(self.send_scpi("ETHERNET:CONFIG:HTPORT?"))

    def eth_set_http_port(self, port: int) -> str:
        """``:ETHERNET:CONFIG:HTPORT:[port]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:HTPORT:{port}")

    def eth_get_telnet_port(self) -> int:
        """``:ETHERNET:CONFIG:TELNETPORT?``"""
        return int(self.send_scpi("ETHERNET:CONFIG:TELNETPORT?"))

    def eth_set_telnet_port(self, port: int) -> str:
        """``:ETHERNET:CONFIG:TELNETPORT:[port]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:TELNETPORT:{port}")

    def eth_get_ssh_port(self) -> int:
        """``:ETHERNET:CONFIG:SSHPORT?``"""
        return int(self.send_scpi("ETHERNET:CONFIG:SSHPORT?"))

    def eth_set_ssh_port(self, port: int) -> str:
        """``:ETHERNET:CONFIG:SSHPORT:[port]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:SSHPORT:{port}")

    def eth_get_ssh_login(self) -> str:
        """``:ETHERNET:CONFIG:SSHLOGINNAME?``"""
        return self.send_scpi("ETHERNET:CONFIG:SSHLOGINNAME?")

    def eth_set_ssh_login(self, name: str) -> str:
        """``:ETHERNET:CONFIG:SSHLOGINNAME:[name]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:SSHLOGINNAME:{name}")

    def eth_get_pwd_enabled(self) -> int:
        """``:ETHERNET:CONFIG:PWDENABLED?``"""
        return int(self.send_scpi("ETHERNET:CONFIG:PWDENABLED?"))

    def eth_set_pwd_enabled(self, enabled: Union[bool, int]) -> str:
        """``:ETHERNET:CONFIG:PWDENABLED:[0|1]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:PWDENABLED:{int(bool(enabled))}")

    def eth_get_password(self) -> str:
        """``:ETHERNET:CONFIG:PWD?``"""
        return self.send_scpi("ETHERNET:CONFIG:PWD?")

    def eth_set_password(self, pwd: str) -> str:
        """``:ETHERNET:CONFIG:PWD:[pwd]``"""
        return self.send_scpi(f"ETHERNET:CONFIG:PWD:{pwd}")

    def eth_init(self) -> str:
        """``:ETHERNET:CONFIG:INIT`` -- apply/commit the Ethernet config."""
        return self.send_scpi("ETHERNET:CONFIG:INIT")

    # --------------------------------------------------------------------- #
    #  DLL-API-compatible layer
    #
    #  Thin wrappers that mirror the function names in the Programming Manual's
    #  DLL reference (Connect, Set_SP4T_COM_To, Read_ModelName, SetSequence_*,
    #  *Ethernet_* ...), so code written against the manual maps 1:1 onto this
    #  pure-USB library. Each delegates to the SCPI methods above; no new wire
    #  protocol is introduced.
    # --------------------------------------------------------------------- #

    # ----- connection ----------------------------------------------------- #
    @classmethod
    def connect(cls, serial: Optional[str] = None, addr: str = _DEFAULT_ADDR) -> "SolidStateSwitch":
        """DLL ``Connect([SN])`` -- open a unit (by serial, or the first one)."""
        return cls(serial=serial, addr=addr)

    @classmethod
    def connect_by_address(cls, address: Union[int, str]) -> "SolidStateSwitch":
        """DLL ``ConnectByAddress(Address)`` -- open a unit and target a chain
        address. The address becomes the SCPI prefix (e.g. ``1`` -> ``:01:...``)
        used for every subsequent command on this handle.
        """
        sw = cls()
        sw.set_address(address)
        return sw

    def disconnect(self) -> None:
        """DLL ``Disconnect()`` -- release the USB handle (alias for close)."""
        self.close()

    def get_usb_connection_status(self) -> int:
        """DLL ``GetUSBConnectionStatus()`` -> ``1`` if the handle is open."""
        return 0 if self._closed else 1

    # ----- address prefix management (chained units) ---------------------- #
    def set_address(self, address: Union[int, str]) -> str:
        """DLL ``Set_Address(Address)`` -- target chained-unit address.

        Sets the SCPI address prefix for this handle. An integer is formatted
        as the manual's two-digit form (``1`` -> ``"01"``); ``"*"`` restores the
        address-agnostic prefix. Note: unlike the DLL call this does not
        *reprogram* the device's stored address (that is a binary byte-0
        operation); it selects which already-assigned address this handle talks
        to. Returns the new prefix.
        """
        if isinstance(address, int):
            self.addr = f"{address:02d}"
        else:
            self.addr = str(address)
        return self.addr

    def get_address(self) -> str:
        """DLL ``Get_Address()`` -- the SCPI address prefix in use."""
        return self.addr

    @classmethod
    def get_available_sn_list(cls) -> List[str]:
        """DLL ``Get_Available_SN_List()`` -- serials of all connected units."""
        return cls.list_serials()

    def get_available_address_list(self) -> List[str]:
        """DLL ``Get_Available_Address_List()`` -- ``["00", "01", ...]`` for the
        local unit plus its chained slaves (from ``:NumberOfSlaves?``)."""
        try:
            slaves = self.number_of_slaves()
        except Exception:
            slaves = 0
        return [f"{i:02d}" for i in range(slaves + 1)]

    # ----- identity ------------------------------------------------------- #
    def read_model_name(self) -> str:
        """DLL ``Read_ModelName()`` (alias for :meth:`model_name`)."""
        return self.model_name()

    def read_sn(self) -> str:
        """DLL ``Read_SN()`` (alias for :meth:`serial_number`)."""
        return self.serial_number()

    def get_firmware(self) -> str:
        """DLL ``GetFirmware()`` (alias for :meth:`firmware`)."""
        return self.firmware()

    def get_ext_firmware(self):
        """DLL ``GetExtFirmware()`` -- not available over ASCII SCPI.

        The extended firmware tuple (A0..A3 + string) is only exposed through
        the binary byte-0 protocol; this USB-ASCII library cannot produce it.
        Use :meth:`firmware` for the firmware revision string.
        """
        raise NotImplementedError(
            "GetExtFirmware uses the binary byte-0 protocol; use firmware() for "
            "the revision string."
        )

    def reset_device(self):
        """DLL ``ResetDevice()`` -- not available over ASCII SCPI.

        The manual documents reset only via the binary byte-0 command set;
        power-cycle the unit instead.
        """
        raise NotImplementedError(
            "ResetDevice uses the binary byte-0 protocol; power-cycle the unit "
            "to reset it."
        )

    def device_info(self) -> Dict[str, str]:
        """USB equivalent of the Ethernet ``MCLRFSWITCH?`` info query.

        Aggregates the same fields that query returns (model, serial, IP,
        subnet, gateway, MAC) using the identity and Ethernet config commands.
        """
        info = {"model": self.model_name(), "serial": self.serial_number()}
        for key, getter in (
            ("ip", self.eth_get_ip), ("subnet", self.eth_get_subnet),
            ("gateway", self.eth_get_gateway), ("mac", self.eth_get_mac),
        ):
            try:
                info[key] = getter()
            except Exception:
                info[key] = ""
        return info

    # ----- switching ------------------------------------------------------ #
    def set_sp4t_com_to(self, port: Union[int, str], channel: Optional[str] = None) -> str:
        """DLL ``Set_SP4T_COM_To(Port)`` (alias for :meth:`set_state`)."""
        return self.set_state(port, channel, "SP4T")

    def get_sp4t_state(self, channel: Optional[str] = None) -> int:
        """DLL ``Get_SP4T_State()`` (alias for :meth:`get_state`)."""
        return self.get_state(channel, "SP4T")

    # ----- sequence (DLL names) ------------------------------------------- #
    def set_sequence_no_of_steps(self, n: int) -> str:
        """DLL ``SetSequence_NoOfSteps`` (alias for :meth:`seq_set_steps`)."""
        return self.seq_set_steps(n)

    def get_sequence_no_of_steps(self) -> int:
        """DLL ``GetSequence_NoOfSteps`` (alias for :meth:`seq_get_steps`)."""
        return self.seq_get_steps()

    def set_sequence_direction(self, direction: int) -> str:
        """DLL ``SetSequence_Direction`` (alias for :meth:`seq_set_direction`)."""
        return self.seq_set_direction(direction)

    def get_sequence_direction(self) -> int:
        """DLL ``GetSequence_Direction`` (alias for :meth:`seq_get_direction`)."""
        return self.seq_get_direction()

    def set_sequence_no_of_cycles(self, n: int) -> str:
        """DLL ``SetSequence_NoOfCycles`` (alias for :meth:`seq_set_cycles`)."""
        return self.seq_set_cycles(n)

    def get_sequence_no_of_cycles(self) -> int:
        """DLL ``GetSequence_NoOfCycles`` (alias for :meth:`seq_get_cycles`)."""
        return self.seq_get_cycles()

    def set_sequence_continuous_mode(self, mode: Union[bool, int, str]) -> str:
        """DLL ``SetSequence_ContinuousMode`` (alias for :meth:`seq_set_mode`)."""
        return self.seq_set_mode(mode)

    def get_sequence_continuous_mode(self) -> str:
        """DLL ``GetSequence_ContinuousMode`` (alias for :meth:`seq_get_mode`)."""
        return self.seq_get_mode()

    def set_sequence_on(self) -> str:
        """DLL ``SetSequence_ON()`` -- start the stored sequence."""
        return self.seq_set_mode(True)

    def set_sequence_off(self) -> str:
        """DLL ``SetSequence_OFF()`` -- stop the stored sequence."""
        return self.seq_set_mode(False)

    # ----- Ethernet (DLL names) ------------------------------------------- #
    @staticmethod
    def _dotted(*parts: Union[int, str]) -> str:
        """Accept ('1.2.3.4',) or (1, 2, 3, 4) -> '1.2.3.4'."""
        if len(parts) == 1:
            return str(parts[0])
        return ".".join(str(p) for p in parts)

    def get_ethernet_use_dhcp(self) -> int:
        """DLL ``GetEthernet_UseDHCP`` (alias for :meth:`eth_get_dhcp`)."""
        return self.eth_get_dhcp()

    def save_ethernet_use_dhcp(self, enabled: Union[bool, int]) -> str:
        """DLL ``SaveEthernet_UseDHCP`` (alias for :meth:`eth_set_dhcp`)."""
        return self.eth_set_dhcp(enabled)

    def get_ethernet_ip_address(self) -> str:
        """DLL ``GetEthernet_IPAddress`` (alias for :meth:`eth_get_ip`)."""
        return self.eth_get_ip()

    def save_ethernet_ip_address(self, *ip: Union[int, str]) -> str:
        """DLL ``SaveEthernet_IPAddress`` -- accepts '1.2.3.4' or 1,2,3,4."""
        return self.eth_set_ip(self._dotted(*ip))

    def get_ethernet_mac_address(self) -> str:
        """DLL ``GetEthernet_MACAddress`` (alias for :meth:`eth_get_mac`)."""
        return self.eth_get_mac()

    def get_ethernet_network_gateway(self) -> str:
        """DLL ``GetEthernet_NetworkGateway`` (alias for :meth:`eth_get_gateway`)."""
        return self.eth_get_gateway()

    def save_ethernet_network_gateway(self, *gw: Union[int, str]) -> str:
        """DLL ``SaveEthernet_NetworkGateway`` -- accepts '1.2.3.4' or 1,2,3,4."""
        return self.eth_set_gateway(self._dotted(*gw))

    def get_ethernet_subnet_mask(self) -> str:
        """DLL ``GetEthernet_SubNetMask`` (alias for :meth:`eth_get_subnet`)."""
        return self.eth_get_subnet()

    def save_ethernet_subnet_mask(self, *mask: Union[int, str]) -> str:
        """DLL ``SaveEthernet_SubnetMask`` -- accepts '255.255.255.0' or quads."""
        return self.eth_set_subnet(self._dotted(*mask))

    def get_ethernet_tcpip_port(self) -> int:
        """DLL ``GetEthernet_TCPIPPort`` (alias for :meth:`eth_get_http_port`)."""
        return self.eth_get_http_port()

    def save_ethernet_tcpip_port(self, port: int) -> str:
        """DLL ``SaveEthernet_TCPIPPort`` (alias for :meth:`eth_set_http_port`)."""
        return self.eth_set_http_port(port)

    def get_ethernet_telnet_port(self) -> int:
        """DLL ``GetEthernet_TelnetPort`` (alias for :meth:`eth_get_telnet_port`)."""
        return self.eth_get_telnet_port()

    def save_ethernet_telnet_port(self, port: int) -> str:
        """DLL ``SaveEthernet_TelnetPort`` (alias for :meth:`eth_set_telnet_port`)."""
        return self.eth_set_telnet_port(port)

    def get_ethernet_ssh_port(self) -> int:
        """DLL ``GetEthernet_SSHPort`` (alias for :meth:`eth_get_ssh_port`)."""
        return self.eth_get_ssh_port()

    def save_ethernet_ssh_port(self, port: int) -> str:
        """DLL ``SaveEthernet_SSHPort`` (alias for :meth:`eth_set_ssh_port`)."""
        return self.eth_set_ssh_port(port)

    def get_ethernet_ssh_login_name(self) -> str:
        """DLL ``GetEthernet_SSHLoginName`` (alias for :meth:`eth_get_ssh_login`)."""
        return self.eth_get_ssh_login()

    def save_ethernet_ssh_login_name(self, name: str) -> str:
        """DLL ``SaveEthernet_SSHLoginName`` (alias for :meth:`eth_set_ssh_login`)."""
        return self.eth_set_ssh_login(name)

    def get_ethernet_use_pwd(self) -> int:
        """DLL ``GetEthernet_UsePWD`` (alias for :meth:`eth_get_pwd_enabled`)."""
        return self.eth_get_pwd_enabled()

    def save_ethernet_use_pwd(self, enabled: Union[bool, int]) -> str:
        """DLL ``SaveEthernet_UsePWD`` (alias for :meth:`eth_set_pwd_enabled`)."""
        return self.eth_set_pwd_enabled(enabled)

    def get_ethernet_pwd(self) -> str:
        """DLL ``GetEthernet_PWD`` (alias for :meth:`eth_get_password`)."""
        return self.eth_get_password()

    def save_ethernet_pwd(self, pwd: str) -> str:
        """DLL ``SaveEthernet_PWD`` (alias for :meth:`eth_set_password`)."""
        return self.eth_set_password(pwd)

    # ----- lifecycle -------------------------------------------------------- #
    @property
    def serial(self) -> Optional[str]:
        """Cached serial number of the connected unit."""
        return self._serial

    def close(self) -> None:
        """Release the USB resources held by this handle."""
        if self._closed:
            return
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        self._closed = True

    def __enter__(self) -> "SolidStateSwitch":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<SolidStateSwitch serial={self._serial!r}>"


# --------------------------------------------------------------------------- #
#  Multiple units, in parallel
# --------------------------------------------------------------------------- #
class SwitchBank:
    """Drive several units concurrently -- one thread per unit.

    Built for the "two dual-SP4T units, channels A & B" setup: opens both
    units (by serial, or auto-discovers all connected units) and lets you set
    or read every channel with a single call. Per-unit operations run in
    parallel threads, each on its own USB handle, so the two units switch as
    close to simultaneously as USB allows.

    Examples
    --------
    >>> bank = SwitchBank.discover()              # open every connected unit
    >>> bank.serials
    ['12208010025', '12208010026']

    # Set COM A=1, COM B=3 on *both* units simultaneously:
    >>> bank.set_all(a=1, b=3)

    # Or set each unit independently, all dispatched in parallel:
    >>> bank.set_states({
    ...     '12208010025': {'A': 1, 'B': 2},
    ...     '12208010026': {'A': 4, 'B': 3},
    ... })

    # Read everything back:
    >>> bank.get_states()
    {'12208010025': {'A': 1, 'B': 2}, '12208010026': {'A': 4, 'B': 3}}

    >>> bank.close()
    """

    def __init__(self, switches: Sequence[SolidStateSwitch]):
        if not switches:
            raise SwitchError("SwitchBank requires at least one switch.")
        self.switches: Dict[str, SolidStateSwitch] = {}
        for sw in switches:
            key = sw.serial or f"dev{len(self.switches)}"
            self.switches[key] = sw
        self._pool = ThreadPoolExecutor(
            max_workers=len(self.switches),
            thread_name_prefix="switchbank",
        )

    # ----- construction ---------------------------------------------------- #
    @classmethod
    def discover(cls, addr: str = _DEFAULT_ADDR) -> "SwitchBank":
        """Open *every* connected Mini-Circuits switch."""
        devs = SolidStateSwitch._iter_raw_devices()
        if not devs:
            raise SwitchError("No Mini-Circuits switches found on the USB bus.")
        switches = [SolidStateSwitch(dev=d, addr=addr) for d in devs]
        return cls(switches)

    @classmethod
    def from_serials(
        cls, serials: Sequence[str], addr: str = _DEFAULT_ADDR
    ) -> "SwitchBank":
        """Open the units with the given serial numbers (order preserved)."""
        return cls([SolidStateSwitch(serial=s, addr=addr) for s in serials])

    # ----- introspection --------------------------------------------------- #
    @property
    def serials(self) -> List[str]:
        return list(self.switches.keys())

    def __getitem__(self, serial: str) -> SolidStateSwitch:
        return self.switches[serial]

    def __len__(self) -> int:
        return len(self.switches)

    def __iter__(self):
        return iter(self.switches.values())

    # ----- parallel primitive ---------------------------------------------- #
    def _map(self, fn) -> Dict[str, object]:
        """Run ``fn(switch)`` on every unit in parallel; return {serial: result}.

        Re-raises the first error encountered (with the serial annotated).
        """
        futures = {
            serial: self._pool.submit(fn, sw)
            for serial, sw in self.switches.items()
        }
        results: Dict[str, object] = {}
        errors: List[str] = []
        for serial, fut in futures.items():
            try:
                results[serial] = fut.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{serial}: {exc}")
        if errors:
            raise SwitchError("Parallel operation failed -> " + "; ".join(errors))
        return results

    def run(self, fn) -> Dict[str, object]:
        """Public parallel-map: ``fn(SolidStateSwitch) -> value`` per unit.

        Escape hatch for any operation the convenience methods don't cover,
        e.g. ``bank.run(lambda s: s.firmware())``.
        """
        return self._map(fn)

    # ----- bulk switching --------------------------------------------------- #
    def set_all(
        self,
        a: Optional[Union[int, str]] = None,
        b: Optional[Union[int, str]] = None,
        sw_type: str = "SP4T",
    ) -> Dict[str, object]:
        """Set the same COM A and/or COM B state on **all** units in parallel.

        Pass only ``a`` to leave B untouched, only ``b`` for the reverse, or
        both to set each unit's A then B (the two units run concurrently).
        """
        if a is None and b is None:
            raise ValueError("provide at least one of a= or b=")

        def _apply(sw: SolidStateSwitch):
            res = {}
            if a is not None:
                res["A"] = sw.set_a(a, sw_type)
            if b is not None:
                res["B"] = sw.set_b(b, sw_type)
            return res

        return self._map(_apply)

    def set_states(
        self,
        mapping: Dict[str, Dict[str, Union[int, str]]],
        sw_type: str = "SP4T",
    ) -> Dict[str, object]:
        """Set per-unit states in parallel.

        ``mapping`` maps each serial to ``{"A": stateA, "B": stateB}`` (either
        channel optional). Units not present in the mapping are left alone.
        """
        unknown = set(mapping) - set(self.switches)
        if unknown:
            raise SwitchError(f"Unknown serial(s): {sorted(unknown)}")

        def _apply(sw: SolidStateSwitch):
            want = mapping.get(sw.serial, {})
            res = {}
            if "A" in want:
                res["A"] = sw.set_a(want["A"], sw_type)
            if "B" in want:
                res["B"] = sw.set_b(want["B"], sw_type)
            return res

        return self._map(_apply)

    def get_states(self, sw_type: str = "SP4T") -> Dict[str, Dict[str, int]]:
        """Read ``{serial: {"A": .., "B": ..}}`` for all units in parallel."""
        return self._map(lambda sw: sw.get_ab(sw_type))  # type: ignore[return-value]

    # ----- identity (handy for verifying which unit is which) -------------- #
    def models(self) -> Dict[str, str]:
        """Model name of every unit."""
        return self._map(lambda sw: sw.model_name())  # type: ignore[return-value]

    def firmwares(self) -> Dict[str, str]:
        """Firmware revision of every unit."""
        return self._map(lambda sw: sw.firmware())  # type: ignore[return-value]

    # ----- lifecycle -------------------------------------------------------- #
    def close(self) -> None:
        """Close every unit and shut down the worker pool."""
        for sw in self.switches.values():
            sw.close()
        self._pool.shutdown(wait=True)

    def __enter__(self) -> "SwitchBank":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<SwitchBank units={self.serials}>"


if __name__ == "__main__":  # pragma: no cover - quick manual smoke test
    print("Connected switch serials:", SolidStateSwitch.list_serials())
    with SwitchBank.discover() as bank:
        print("Models:   ", bank.models())
        print("Firmware: ", bank.firmwares())
        print("States:   ", bank.get_states())
