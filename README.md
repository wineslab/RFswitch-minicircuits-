# mini_circuits_switch

Pure-USB Python library for Mini-Circuits Solid-State Switches
(USB HID, `VID 0x20CE` / `PID 0x0022`), built for **two dual-SP4T units, each
with COM channels A and B**, driven in parallel.

Derived from the *Solid-State Switch Programming Manual* and the
`AN-49-009` application note; the USB transport matches the working
`set-get-state.py` / `demo-minisw.py` scripts.

## Layout

```
mini-switch/
├── mini_circuits_switch.py   the library
├── requirements.txt          pyusb + Flask
├── docs/                     Programming Manual + AN-49-009 (PDFs)
├── examples/                 example_parallel.py, demo-minisw.py, set-get-state.py
├── tests/                    test_scpi_wire.py (hardware-free, 87 checks)
├── logs/                     captured session output
├── packaging/                99-mini-circuits-switch.rules (udev)
└── webapp/                   Flask API + website
    ├── app.py                routes / JSON API
    ├── backend.py            real-hardware or simulation backend
    ├── templates/index.html
    └── static/{app.js,style.css}
```

## Run it on any machine (web app)

Copy the whole folder anywhere, then:

```bash
./setup.sh     # one time: makes a .venv, installs deps, installs the udev rule
./run.sh       # start the web app -> http://127.0.0.1:5000
```

`setup.sh` and `run.sh` are path-relative — they work from wherever you copy the
folder. **Don't copy the `.venv/` directory between machines** (it has absolute
paths baked in); just run `./setup.sh` again on the new machine to rebuild it.

### What it depends on

| Dependency | What / why | Installed by |
|---|---|---|
| **Python 3.8+** | runs everything | you (system) |
| **`pyusb`**, **`Flask`** | USB driver + web app (see `requirements.txt`) | `setup.sh` → `.venv` |
| **`libusb-1.0`** (system lib) | pyusb's USB backend | `setup.sh` (apt/dnf/pacman/brew) |
| **udev rule** `20ce:0022` | non-root USB access (Linux) | `setup.sh` (needs sudo once) |

That's the entire footprint — two pip packages, one system library, one udev rule.

### Manual install (instead of the scripts)

```bash
pip install -r requirements.txt              # pyusb (+ libusb backend) and Flask
# Linux only — permission to claim the HID interface (or run as root):
sudo cp packaging/99-mini-circuits-switch.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # then replug
python3 webapp/app.py                         # -> http://127.0.0.1:5000
```

## Two layers

| Class | Scope | Use it for |
|-------|-------|------------|
| `SolidStateSwitch` | one unit | every SCPI command on a single switch |
| `SwitchBank` | many units | driving both units / all A&B channels **in parallel** |

Each unit is addressed by **serial number** over its own USB handle, so the two
identical units never cross-talk.

## Quick start

```python
from mini_circuits_switch import SolidStateSwitch, SwitchBank

# Discover what's plugged in
print(SolidStateSwitch.list_serials())     # ['12208010025', '12208010026']

# --- Two units, A & B, in parallel ---
with SwitchBank.discover() as bank:
    bank.set_all(a=1, b=3)                  # COM A=1, B=3 on BOTH units at once
    print(bank.get_states())                # {'..25': {'A':1,'B':3}, '..26': {...}}

    bank.set_states({                       # per-unit, still parallel
        '12208010025': {'A': 2, 'B': 4},
        '12208010026': {'A': 4, 'B': 1},
    })

# --- One unit directly ---
with SolidStateSwitch(serial='12208010025') as sw:
    sw.set_a(2)                             # COM A -> port 2
    sw.set_b(4)                             # COM B -> port 4
    print(sw.get_ab())                      # {'A': 2, 'B': 4}
    print(sw.model_name(), sw.firmware())
```

`SwitchBank` runs one thread per unit, each on its own USB handle, so the units
switch as close to simultaneously as USB allows. Within a single unit COM A then
COM B are sent sequentially (one control link per unit).

See `example_parallel.py` for a fuller walkthrough.

## SCPI command coverage

All commands documented in the manual are exposed (with a `send_scpi()` raw
escape hatch for anything else):

**Identity / addressing**
`:MN?` → `model_name()` · `:SN?` → `serial_number()` ·
`:FIRMWARE?` → `firmware()` · `:AssignAddresses` → `assign_addresses()` ·
`:NumberOfSlaves?` → `number_of_slaves()`

**Switching** — `:[Sw_Type]:[Channel]:STATE[:state|?]`
`set_state(state, channel, sw_type)` / `get_state(channel, sw_type)` and the
shortcuts `set_a` `set_b` `get_a` `get_b` `set_ab` `get_ab`.
`sw_type` defaults to `"SP4T"`; pass `"SPDT"`, `"SP2T"`, `"SP6T"`, `"SP8T"`,
`"SP16T"`, etc. for other models.

**Sequence engine** — `:SEQ:...`
`seq_set_steps`/`seq_get_steps`, `seq_set_step`/`seq_get_step`,
`seq_set_state`/`seq_get_state`, `seq_set_dwell_time`/`seq_get_dwell_time`,
`seq_set_dwell_units`/`seq_get_dwell_units` (`U`=µs, `M`=ms, `S`=s),
`seq_set_cycles`/`seq_get_cycles`,
`seq_set_direction`/`seq_get_direction` (0=fwd, 1=rev, 2=bidir),
`seq_set_mode(on/off)`.

**Ethernet config** — `:ETHERNET:CONFIG:...` (informational on USB-only units)
`eth_listen`, `eth_get/set_ip`, `eth_get/set_gateway` (`NG`),
`eth_get/set_subnet` (`SM`), `eth_get_mac`, `eth_get/set_dhcp`,
`eth_get/set_http_port` (`HTPORT`), `eth_get/set_telnet_port`,
`eth_get/set_ssh_port`, `eth_get/set_ssh_login`,
`eth_get/set_pwd_enabled`, `eth_get/set_password`, `eth_init`.

## DLL-API compatibility

For code written against the manual's **DLL function reference**, every function
has a same-named wrapper (delegating to the SCPI methods above):

`Connect`/`ConnectByAddress`/`Disconnect` → `connect()` / `connect_by_address()` /
`disconnect()` · `GetUSBConnectionStatus` → `get_usb_connection_status()` ·
`Read_ModelName`/`Read_SN`/`GetFirmware` → `read_model_name()` / `read_sn()` /
`get_firmware()` · `Set_Address`/`Get_Address` → `set_address()` / `get_address()`
(manage the SCPI address prefix for chained units) ·
`Get_Available_SN_List`/`Get_Available_Address_List` →
`get_available_sn_list()` / `get_available_address_list()` ·
`Set_SP4T_COM_To`/`Get_SP4T_State` → `set_sp4t_com_to()` / `get_sp4t_state()` ·
the `SetSequence_*`/`GetSequence_*` family → `set_sequence_*()` / `get_sequence_*()`
(incl. the one-call `set_sequence_step(step, switch_to, dwell, dwell_units)`,
`set_sequence_on()`, `set_sequence_off()`) · the `*Ethernet_*` family →
`get_ethernet_*()` / `save_ethernet_*()`.

**Binary byte-0 only** (no ASCII SCPI form): `ResetDevice` and `GetExtFirmware`
are present but raise `NotImplementedError` — power-cycle to reset; use
`firmware()` for the revision string.

**Ethernet HTTP/Telnet/SSH text commands** (`SETA=`, `SETB=`, `GETA`, `GETB`,
`MCLRFSWITCH?`) run over the network interface, not USB. Their USB equivalents
are `set_a`/`set_b`/`get_a`/`get_b` and `device_info()` (which aggregates the
same model/serial/IP/subnet/gateway/MAC fields as `MCLRFSWITCH?`).

## Web app (Flask API + website)

```bash
python3 webapp/app.py        # http://127.0.0.1:5000
```

**Running with the real switch (needs USB permission).** Use a Python that has
both `pyusb` and `Flask`, with access to the device. Two options:

```bash
# A) quickest — root + the base conda env (has pyusb + Flask):
sudo /home/aswanth/miniconda3/bin/python3 webapp/app.py

# B) no sudo — install the udev rule once, then run as your user:
sudo cp packaging/99-mini-circuits-switch.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # replug the switch
python3 webapp/app.py
```

Gotcha: plain `sudo python3` uses the *system* Python (no `pyusb`) and fails with
`ModuleNotFoundError: usb`; point sudo at the conda interpreter that has pyusb.
Quick hardware check: `sudo /home/aswanth/miniconda3/bin/python3 tools/probe.py`.

**Real hardware only — no simulation.** The server starts immediately and
connects to the switch lazily (proving the USB link with an identity probe on
the first request). The page shows a **CONNECTED / DISCONNECTED** badge, lets you
click ports for COM A/B per unit, set all units at once, and run raw SCPI, and
has a **help panel** (top-right "? Help") listing every feature and endpoint.
When the switch is unplugged or the USB link is denied, the API returns
**HTTP 503** with the real reason — nothing is faked. After (re)plugging, hit
**Retry connection** (or `POST /api/reconnect`) — no restart needed.

Needs USB permission: install the udev rule (above) or run with `sudo`.

### JSON API

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/health` | — | `{ok, connected, units, error}` |
| `POST /api/reconnect` | — | `{units:[...]}` (or 503) |
| `GET /api/units` | — | `{units:[{serial,model,firmware}], channels, ports}` |
| `GET /api/states` | — | `{serial:{A,B}, ...}` |
| `GET /api/units/<serial>/state` | — | `{A,B}` |
| `POST /api/units/<serial>/state` | `{A?,B?}` | `{A,B}` |
| `POST /api/set_all` | `{a?,b?}` | `{serial:{A,B}, ...}` |
| `POST /api/scpi` | `{serial,command}` | `{reply}` |

```bash
curl -X POST localhost:5000/api/units/<serial>/state -H 'Content-Type: application/json' -d '{"A":3}'
curl -X POST localhost:5000/api/scpi -H 'Content-Type: application/json' -d '{"serial":"<serial>","command":"SP4T:A:STATE?"}'
```

## Tests

```bash
python3 tests/test_scpi_wire.py   # 87 checks, no hardware/root needed
```
```
