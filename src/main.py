# PicoMail entry point for the Raspberry Pi Pico W (MicroPython).
# Connects to Wi-Fi, sets the clock, then runs the mail servers forever.
import asyncio
import gc
import sys
import time

import machine
import network

import config
from picomail import app

led = machine.Pin("LED", machine.Pin.OUT)


def connect_wifi():
    try:
        import rp2
        rp2.country(getattr(config, "WIFI_COUNTRY", "US"))
    except Exception:
        pass
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.config(pm=0xA11140)  # disable power saving: we are a server
    static = getattr(config, "STATIC_IP", None)
    if static:
        wlan.ifconfig(static)
    if not wlan.isconnected():
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        for _ in range(60):
            if wlan.isconnected():
                break
            led.toggle()
            time.sleep(0.5)
    if not wlan.isconnected():
        raise RuntimeError("Wi-Fi connection failed")
    led.on()
    print("wifi: connected, ip", wlan.ifconfig()[0])
    return wlan


def sync_time():
    import ntptime
    host = getattr(config, "NTP_HOST", None)
    if host:
        ntptime.host = host
    for _ in range(5):
        try:
            ntptime.settime()
            print("time: synced", time.gmtime())
            return True
        except Exception:
            time.sleep(2)
    print("time: NTP failed, dates in headers may be wrong")
    return False


async def housekeeping(wlan, servers):
    last_ntp = time.time()
    while True:
        await asyncio.sleep(30)
        if not app.servers_alive(servers):
            raise RuntimeError("a listener stopped; rebooting")
        if not wlan.isconnected():
            led.off()
            print("wifi: lost connection, reconnecting")
            wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
            for _ in range(40):
                await asyncio.sleep(0.5)
                if wlan.isconnected():
                    led.on()
                    break
        if time.time() - last_ntp > 86400:
            sync_time()
            last_ntp = time.time()
        gc.collect()


async def main():
    wlan = connect_wifi()
    sync_time()
    _, servers = await app.start(config)
    gc.collect()
    print("picomail: running, free RAM", gc.mem_free())
    await housekeeping(wlan, servers)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("stopped")
except Exception as e:
    # Log the crash to flash and reboot so the server comes back on its own.
    try:
        with open("/crash.log", "w") as f:
            sys.print_exception(e, f)
    except Exception:
        pass
    sys.print_exception(e)
    time.sleep(10)
    machine.reset()
