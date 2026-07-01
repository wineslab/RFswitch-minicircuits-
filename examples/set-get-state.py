#:[Sw_Type]:[Sw_Channel]:STATE:[Sw_State]
# Get State 
#:[SP4T]:[A]/[B]:STATE?
# set State 
#:[SP4T]:[A]/[B]:STATE:1

import usb.core
import usb.util

dev = usb.core.find(idVendor=0x20ce, idProduct=0x0022)
# Source - https://stackoverflow.com/a/67459378
# Posted by Eno Gerguri
# Retrieved 2026-06-28, License - CC BY-SA 4.0
i = 0
if dev.is_kernel_driver_active(i):
    try:
        dev.detach_kernel_driver(i)
    except usb.core.USBError as e:
        sys.exit("Could not detatch kernel driver from interface({0}): {1}".format(i, str(e)))

# Source - https://stackoverflow.com/a/67459378
# Posted by Eno Gerguri
# Retrieved 2026-06-28, License - CC BY-SA 4.0

def query(cmd):
    dev.write(1, cmd)
    resp = dev.read(0x81, 64)
    out = ""
    i = 1
    while resp[i] > 0 and resp[i] < 255:
        out += chr(resp[i])
        i += 1
    return out

set_switch = 2
print("------Switch-------")
print(f"Read A Before:", query("*:SP4T:A:STATE?"))
print(f"Set A->{set_switch}:     ", query(f"*:SP4T:A:STATE:{set_switch}"))
print(f"Read A After: ", query("*:SP4T:A:STATE?"))