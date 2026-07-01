"""Hardware-free verification that mini_circuits_switch emits exactly the SCPI
wire strings documented in Prog_Manual-Solid_State_Switch.pdf.

A fake USB device records every byte string written to the OUT endpoint and
returns programmable HID replies, so we can assert the command builder and the
reply parser against the manual without a real switch or root permission.

Run:  python3 tests/test_scpi_wire.py   (from the repo root)
"""

import os
import sys

# Allow running from anywhere: put the repo root (which holds the library) on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_circuits_switch import SolidStateSwitch


class FakeDev:
    """Minimal stand-in for usb.core.Device that logs writes / canned reads."""

    def __init__(self):
        self.writes = []
        self.reply = "1"          # default ACK

    # _claim iterates the device; an empty config list is fine here.
    def __iter__(self):
        return iter([])

    def set_configuration(self):
        pass

    def write(self, ep, data):
        assert ep == 0x01, f"expected OUT endpoint 0x01, got {ep:#x}"
        self.writes.append(data)
        return len(data)

    def read(self, ep, length):
        assert ep == 0x81, f"expected IN endpoint 0x81, got {ep:#x}"
        assert length == 64, f"expected 64-byte HID report, got {length}"
        # Byte 0 = report id (skipped by the decoder); payload is null-terminated.
        return bytes([0]) + self.reply.encode("ascii") + bytes([0])


def new_switch():
    fake = FakeDev()
    fake.reply = "SN=12208010025"          # ctor caches serial_number()
    sw = SolidStateSwitch(dev=fake)
    fake.writes.clear()                    # drop the ctor's SN? probe
    fake.reply = "1"
    return sw, fake


PASS = 0
FAIL = 0


def expect_wire(sw, fake, call, manual_cmd, reply="1"):
    """Run `call`, assert it wrote exactly `manual_cmd`; return the call result."""
    global PASS, FAIL
    fake.writes.clear()
    fake.reply = reply
    result = call()
    sent = fake.writes[-1] if fake.writes else None
    ok = sent == manual_cmd
    print(f"  [{'OK ' if ok else 'XX '}] wire={sent!r:38} manual={manual_cmd!r}")
    PASS += ok
    FAIL += not ok
    return result


def expect_value(label, got, want):
    global PASS, FAIL
    ok = got == want
    print(f"  [{'OK ' if ok else 'XX '}] {label}: got {got!r} want {want!r}")
    PASS += ok
    FAIL += not ok


sw, fake = new_switch()

print("Identity / addressing")
expect_value("model_name parse", expect_wire(sw, fake, sw.model_name, "*:MN?", "MN=RC-2SPDT-A18"), "RC-2SPDT-A18")
expect_value("serial parse", expect_wire(sw, fake, sw.serial_number, "*:SN?", "SN=12208010025"), "12208010025")
expect_wire(sw, fake, sw.firmware, "*:FIRMWARE?", "B3")
expect_wire(sw, fake, sw.assign_addresses, "*:AssignAddresses")
expect_value("slaves parse", expect_wire(sw, fake, sw.number_of_slaves, "*:NumberOfSlaves?", "1"), 1)

print("Switching  (:[Sw_Type]:[Sw_Channel]:STATE...)")
expect_wire(sw, fake, lambda: sw.set_state(2, "A"), "*:SP4T:A:STATE:2")
expect_wire(sw, fake, lambda: sw.set_a(1), "*:SP4T:A:STATE:1")
expect_wire(sw, fake, lambda: sw.set_b(4), "*:SP4T:B:STATE:4")
expect_value("get_a parse", expect_wire(sw, fake, sw.get_a, "*:SP4T:A:STATE?", "1"), 1)
expect_wire(sw, fake, lambda: sw.set_state(1, "A", "SP2T"), "*:SP2T:A:STATE:1")   # manual p.example
expect_wire(sw, fake, lambda: sw.set_state(16, None, "SP16T"), "*:SP16T:STATE:16")  # no channel
expect_value("multi-unit STATE? parse", expect_wire(sw, fake, lambda: sw.get_state(None, "SP16T"), "*:SP16T:STATE?", "00:16"), 16)

print("Sequence engine  (:SEQ:...)")
expect_wire(sw, fake, lambda: sw.seq_set_steps(10), "*:SEQ:STEPS:10")
expect_value("steps parse", expect_wire(sw, fake, sw.seq_get_steps, "*:SEQ:STEPS?", "10"), 10)
expect_wire(sw, fake, lambda: sw.seq_set_step(2), "*:SEQ:STEP:2")
expect_value("step parse", expect_wire(sw, fake, sw.seq_get_step, "*:SEQ:STEP?", "2"), 2)
expect_wire(sw, fake, lambda: sw.seq_set_state(1, 2, 2, 1), "*:SEQ:STATE:1:2:2:1")
expect_wire(sw, fake, lambda: sw.seq_set_state(8), "*:SEQ:STATE:8")
expect_value("seq state parse", expect_wire(sw, fake, sw.seq_get_state, "*:SEQ:STATE?", "1:2:2:1"), "1:2:2:1")
expect_wire(sw, fake, lambda: sw.seq_set_dwell_time(250), "*:SEQ:DWELLTIME:250")
expect_value("dwell parse", expect_wire(sw, fake, sw.seq_get_dwell_time, "*:SEQ:DWELLTIME?", "250"), 250)
expect_wire(sw, fake, lambda: sw.seq_set_dwell_units("U"), "*:SEQ:DWELLUNITS:U")
expect_wire(sw, fake, lambda: sw.seq_set_dwell_units("m"), "*:SEQ:DWELLUNITS:M")  # case-normalised
expect_wire(sw, fake, lambda: sw.seq_set_cycles(5), "*:SEQ:CYCLES:5")
expect_value("cycles parse", expect_wire(sw, fake, sw.seq_get_cycles, "*:SEQ:CYCLES?", "5"), 5)
expect_wire(sw, fake, lambda: sw.seq_set_direction(2), "*:SEQ:DIRECTION:2")
expect_value("direction parse", expect_wire(sw, fake, sw.seq_get_direction, "*:SEQ:DIRECTION?", "1"), 1)
expect_wire(sw, fake, lambda: sw.seq_set_mode("OFF"), "*:SEQ:MODE:OFF")
expect_wire(sw, fake, lambda: sw.seq_set_mode(True), "*:SEQ:MODE:ON")

print("Ethernet config  (:ETHERNET:CONFIG:...)")
expect_wire(sw, fake, sw.eth_listen, "*:ETHERNET:CONFIG:LISTEN?", "192.100.1.1;255.255.255.0;192.100.1.0")
expect_wire(sw, fake, sw.eth_get_mac, "*:ETHERNET:CONFIG:MAC?", "D0-73-7F-82-D8-01")
expect_value("dhcp parse", expect_wire(sw, fake, sw.eth_get_dhcp, "*:ETHERNET:CONFIG:DHCPENABLED?", "1"), 1)
expect_wire(sw, fake, lambda: sw.eth_set_dhcp(True), "*:ETHERNET:CONFIG:DHCPENABLED:1")
expect_wire(sw, fake, sw.eth_get_ip, "*:ETHERNET:CONFIG:IP?", "192.100.1.1")
expect_wire(sw, fake, lambda: sw.eth_set_ip("192.100.1.1"), "*:ETHERNET:CONFIG:IP:192.100.1.1")
expect_wire(sw, fake, sw.eth_get_gateway, "*:ETHERNET:CONFIG:NG?", "192.168.1.0")
expect_wire(sw, fake, lambda: sw.eth_set_gateway("192.100.1.0"), "*:ETHERNET:CONFIG:NG:192.100.1.0")
expect_wire(sw, fake, sw.eth_get_subnet, "*:ETHERNET:CONFIG:SM?", "255.255.255.0")
expect_wire(sw, fake, lambda: sw.eth_set_subnet("255.255.255.0"), "*:ETHERNET:CONFIG:SM:255.255.255.0")
expect_wire(sw, fake, lambda: sw.eth_set_http_port(8080), "*:ETHERNET:CONFIG:HTPORT:8080")
expect_wire(sw, fake, lambda: sw.eth_set_telnet_port(21), "*:ETHERNET:CONFIG:TELNETPORT:21")
expect_wire(sw, fake, lambda: sw.eth_set_ssh_port(21), "*:ETHERNET:CONFIG:SSHPORT:21")
expect_wire(sw, fake, lambda: sw.eth_set_ssh_login("ssh_user"), "*:ETHERNET:CONFIG:SSHLOGINNAME:ssh_user")
expect_wire(sw, fake, lambda: sw.eth_set_pwd_enabled(1), "*:ETHERNET:CONFIG:PWDENABLED:1")
expect_wire(sw, fake, lambda: sw.eth_set_password("PASS-123"), "*:ETHERNET:CONFIG:PWD:PASS-123")
expect_wire(sw, fake, sw.eth_init, "*:ETHERNET:CONFIG:INIT")

print("Raw escape hatch  (no double-prefix; explicit address respected)")
expect_wire(sw, fake, lambda: sw.send_scpi("SP4T:A:STATE?"), "*:SP4T:A:STATE?")  # prefix added
expect_wire(sw, fake, lambda: sw.send_scpi(":00:MN?"), ":00:MN?")                 # leading colon kept
expect_wire(sw, fake, lambda: sw.send_scpi("00:SP16T:STATE:16"), "00:SP16T:STATE:16")  # digit addr kept

print("DLL-API-compatible aliases")
expect_wire(sw, fake, lambda: sw.set_sp4t_com_to(1, "A"), "*:SP4T:A:STATE:1")
expect_value("get_sp4t_state parse", expect_wire(sw, fake, lambda: sw.get_sp4t_state("A"), "*:SP4T:A:STATE?", "3"), 3)
expect_wire(sw, fake, sw.read_model_name, "*:MN?", "MN=RC-2SPDT-A18")
expect_wire(sw, fake, sw.read_sn, "*:SN?", "SN=12208010025")
expect_wire(sw, fake, sw.get_firmware, "*:FIRMWARE?", "B3")
expect_wire(sw, fake, lambda: sw.set_sequence_no_of_steps(5), "*:SEQ:STEPS:5")
expect_wire(sw, fake, lambda: sw.set_sequence_direction(1), "*:SEQ:DIRECTION:1")
expect_wire(sw, fake, lambda: sw.set_sequence_no_of_cycles(3), "*:SEQ:CYCLES:3")
expect_wire(sw, fake, sw.set_sequence_on, "*:SEQ:MODE:ON")
expect_wire(sw, fake, sw.set_sequence_off, "*:SEQ:MODE:OFF")
expect_wire(sw, fake, lambda: sw.save_ethernet_ip_address(1, 2, 3, 4), "*:ETHERNET:CONFIG:IP:1.2.3.4")
expect_wire(sw, fake, lambda: sw.save_ethernet_ip_address("192.100.1.1"), "*:ETHERNET:CONFIG:IP:192.100.1.1")
expect_wire(sw, fake, lambda: sw.save_ethernet_tcpip_port(80), "*:ETHERNET:CONFIG:HTPORT:80")
expect_wire(sw, fake, lambda: sw.save_ethernet_use_dhcp(True), "*:ETHERNET:CONFIG:DHCPENABLED:1")
expect_wire(sw, fake, lambda: sw.save_ethernet_subnet_mask(255, 255, 255, 0), "*:ETHERNET:CONFIG:SM:255.255.255.0")
expect_value("get_usb_connection_status", sw.get_usb_connection_status(), 1)

print("Composite sequence step  (DLL SetSequence_Step)")
fake.writes.clear(); fake.reply = "1"
sw.set_sequence_step(2, 3, 5, 0)          # step 2 -> state 3, dwell 5 us
seq_wires = list(fake.writes)
expect_value("set_sequence_step wires", seq_wires,
             ["*:SEQ:STEP:2", "*:SEQ:STATE:3", "*:SEQ:DWELLTIME:5", "*:SEQ:DWELLUNITS:U"])
expect_value("get_sequence_dwell_units parse",
             (lambda: (fake.__setattr__('reply', 'M'), sw.get_sequence_dwell_units(2))[1])(), 1)

print("Address-prefix management  (DLL Set_Address / Get_Address)")
expect_value("set_address(1) -> prefix", sw.set_address(1), "01")
expect_wire(sw, fake, sw.get_a, "01:SP4T:A:STATE?", "1")   # prefix now applied
expect_value("get_address()", sw.get_address(), "01")
sw.set_address("*")                                        # restore
expect_wire(sw, fake, sw.get_a, "*:SP4T:A:STATE?", "1")

print("Byte-0-only functions raise NotImplementedError")
for name in ("reset_device", "get_ext_firmware"):
    try:
        getattr(sw, name)()
        print(f"  [XX ] {name} did not raise"); FAIL += 1
    except NotImplementedError:
        print(f"  [OK ] {name} raises NotImplementedError"); PASS += 1

print("device_info() aggregates identity + ethernet")
fake.reply = "X"
info = sw.device_info()
expect_value("device_info keys", sorted(info), ["gateway", "ip", "mac", "model", "serial", "subnet"])

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
