#!/usr/bin/env bash
#
# mini-switch one-time setup — makes the project run on a fresh machine.
#
#   1. checks for python3
#   2. ensures the libusb system library (pyusb needs it)
#   3. creates an isolated .venv and installs pyusb + Flask
#   4. installs the udev rule so the switch works without sudo (Linux)
#
# Everything is relative to this script's own folder, so you can copy the
# whole directory anywhere and just run:  ./setup.sh   then   ./run.sh
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> mini-switch setup  ($SCRIPT_DIR)"

# 0. don't run as root -----------------------------------------------------  #
# Running the whole thing under sudo makes .venv root-owned (then ./run.sh
# fails for your normal user). We only need root for the few apt/udev steps,
# and those call sudo themselves.
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  echo "ERROR: don't run setup.sh with sudo." >&2
  echo "       Run it as your normal user:  ./setup.sh" >&2
  echo "       (individual steps will ask for your password when they need root.)" >&2
  exit 1
fi

# 1. python3 ---------------------------------------------------------------- #
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.8+ first." >&2
  exit 1
fi
echo "    python: $(python3 --version)"

# 2. libusb (pyusb's backend) ---------------------------------------------- #
if ! ldconfig -p 2>/dev/null | grep -q 'libusb-1.0'; then
  echo "==> libusb-1.0 not found; trying to install it…"
  if   command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y libusb-1.0-0
  elif command -v dnf     >/dev/null 2>&1; then sudo dnf install -y libusbx
  elif command -v yum     >/dev/null 2>&1; then sudo yum install -y libusbx
  elif command -v pacman  >/dev/null 2>&1; then sudo pacman -S --noconfirm libusb
  elif command -v zypper  >/dev/null 2>&1; then sudo zypper install -y libusb-1_0-0
  elif command -v brew    >/dev/null 2>&1; then brew install libusb
  else echo "    WARN: please install the libusb-1.0 library manually for your OS."
  fi
fi

# 3. virtualenv + python deps ---------------------------------------------- #
if [ ! -d .venv ]; then
  echo "==> creating virtualenv (.venv)"
  python3 -m venv .venv
fi
VENV_PY="./.venv/bin/python"

# Some distros (esp. brand-new Python from a PPA/source build) create the venv
# WITHOUT pip because the matching python3-venv/ensurepip bundle is missing —
# that's the "No module named pip" failure. Bootstrap pip before installing.
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  echo "==> venv has no pip; bootstrapping it…"
  if "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
    echo "    pip bootstrapped via ensurepip"
  elif command -v curl >/dev/null 2>&1; then
    echo "    ensurepip unavailable; fetching get-pip.py"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$VENV_PY"
  elif command -v wget >/dev/null 2>&1; then
    echo "    ensurepip unavailable; fetching get-pip.py"
    wget -qO- https://bootstrap.pypa.io/get-pip.py | "$VENV_PY"
  else
    echo "ERROR: could not get pip into the venv." >&2
    echo "       Install your Python's venv package and re-run, e.g.:" >&2
    echo "         sudo apt-get install -y python3-venv   # or python3.X-venv" >&2
    echo "       then:  rm -rf .venv && ./setup.sh" >&2
    exit 1
  fi
fi

echo "==> installing Python dependencies (pyusb, Flask)"
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r requirements.txt

# 4. udev rule for non-root USB access (Linux only) ------------------------ #
install_udev() {
  [ -d /etc/udev/rules.d ] || { echo "    (no /etc/udev — skipping udev step, non-Linux)"; return; }
  local dst=/etc/udev/rules.d/99-mini-circuits-switch.rules
  if [ -f "$dst" ]; then echo "    udev rule already installed"; return; fi
  echo "==> installing udev rule so the switch works without sudo (needs your password)"
  if sudo cp packaging/99-mini-circuits-switch.rules "$dst" 2>/dev/null; then
    sudo udevadm control --reload-rules || true
    sudo udevadm trigger --action=add --subsystem-match=usb || true
    echo "    installed — if a switch is already plugged in, unplug/replug it once."
  else
    echo "    couldn't install automatically; run these once by hand:"
    echo "      sudo cp packaging/99-mini-circuits-switch.rules $dst"
    echo "      sudo udevadm control --reload-rules && sudo udevadm trigger --action=add --subsystem-match=usb"
  fi
}
install_udev

# 5. start with a clean, machine-local presets file ------------------------ #
# Presets are per-machine; a copied folder shouldn't carry over old ones.
# (setup is one-time, so this won't wipe presets during normal use.)
echo '{}' > webapp/presets.json
echo "==> presets reset (empty, local to this machine)"

echo
echo "Setup complete. Start the web app with:"
echo "    ./run.sh"
echo "then open  http://127.0.0.1:5000"
