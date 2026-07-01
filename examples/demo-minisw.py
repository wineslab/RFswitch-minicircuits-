import usb.core
import usb.util

dev = usb.core.find(idVendor=0x20ce, idProduct=0x0022)
if dev is None:
    raise ValueError('Device not found')

for configuration in dev:
    for interface in configuration:
        ifnum = interface.bInterfaceNumber
        if not dev.is_kernel_driver_active(ifnum):
            continue
        try:
            dev.detach_kernel_driver(ifnum)
        except usb.core.USBError as e:   # <-- 'as e', not ', e'
            pass

dev.set_configuration()

def query(cmd):
    dev.write(1, cmd)
    resp = dev.read(0x81, 64)
    out = ""
    i = 1
    while resp[i] > 0 and resp[i] < 255:
        out += chr(resp[i])
        i += 1
    return out

print("Model: ", query("*:MN?"))
print("Serial:", query("*:SN?"))
print("FW:    ", query("*:FIRMWARE?"))