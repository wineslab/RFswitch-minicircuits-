"""Example: drive two dual-SP4T units (channels A & B) in parallel over USB.

Run with:  python3 examples/example_parallel.py   (from the repo root)
(needs the two switches plugged in; on Linux run with sudo or a udev rule).
"""

import os
import sys

# Allow running from anywhere: put the repo root (which holds the library) on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_circuits_switch import SolidStateSwitch, SwitchBank


def main() -> None:
    # 1. See what is connected.
    serials = SolidStateSwitch.list_serials()
    print("Connected units:", serials)

    # 2. Open every connected unit. (Or pin the order/identity explicitly:
    #    bank = SwitchBank.from_serials(["12208010025", "12208010026"]) )
    with SwitchBank.discover() as bank:
        print("Models:   ", bank.models())
        print("Firmware: ", bank.firmwares())

        # --- Set the SAME state on both units, A and B together, in parallel.
        print("\nSet A=1, B=3 on BOTH units...")
        bank.set_all(a=1, b=3)
        print("Read back:", bank.get_states())

        # --- Set each unit independently (still dispatched in parallel).
        print("\nSet per-unit states...")
        bank.set_states({
            serials[0]: {"A": 2, "B": 4},
            serials[1]: {"A": 4, "B": 1},
        })
        print("Read back:", bank.get_states())

        # --- Talk to one specific unit directly.
        one = bank[serials[0]]
        print(f"\nUnit {one.serial}: A={one.get_a()} B={one.get_b()}")

        # --- Raw SCPI escape hatch (any command from the manual).
        print("Raw query :SN? ->", one.send_scpi("SN?"))


if __name__ == "__main__":
    main()
