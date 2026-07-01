"""Quick hardware probe — how many switches are connected and their state.

Run (needs USB permission):
    sudo python3 tools/probe.py
"""

import os
import sys

# Repo root (holds the library) on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_circuits_switch import SolidStateSwitch, SwitchBank, SwitchError


def main() -> int:
    serials = SolidStateSwitch.list_serials()
    print(f"Connected switches: {len(serials)}")
    for s in serials:
        print(f"  - {s}")
    if not serials:
        print("\nNothing found. If a switch IS plugged in, this is almost always a")
        print("permission issue — run with sudo, or install packaging/99-mini-circuits-switch.rules.")
        return 1

    with SwitchBank.discover() as bank:
        models = bank.models()
        fws = bank.firmwares()
        states = bank.get_states()          # GET on COM A and COM B of every unit
        print("\nUnit details + GET on all channels:")
        for serial in bank.serials:
            st = states.get(serial, {})
            print(f"  {serial}  [{models.get(serial, '?')} fw {fws.get(serial, '?')}]"
                  f"   A={st.get('A')}  B={st.get('B')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SwitchError as exc:
        print(f"\nSwitchError: {exc}", file=sys.stderr)
        raise SystemExit(2)
