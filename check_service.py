#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["paho-mqtt>=2,<3"]
# ///
"""Check sidepulse_mqtt.py against a real SidePulse service and a real MQTT broker.

Needs SIDEPULSE_BIN, the path of a real `sidepulse` command, and a broker without
auth at MQTT_HOST:MQTT_PORT (default 127.0.0.1:18830). The script starts
`sidepulse service run` in a temporary home folder, with a folder that stands in
for the device. It does not touch your SidePulse or your device. Exits 1 on
failure.

Use it to check a SidePulse version. CI runs it against pinned SidePulse commits
and against its main branch.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).parent))
import sidepulse_mqtt as bridge  # noqa: E402

HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
PORT = int(os.environ.get("MQTT_PORT", "18830"))
# The real service drops a hold after this time unless the bridge renews it. The
# bridge renews after 5/6 of it, and it looks every 5 s, so 36 s leaves it one look.
HOLD = 36.0
cli = os.environ.get("SIDEPULSE_BIN", "")
if not Path(cli).is_file():
    sys.exit("Set SIDEPULSE_BIN to a real sidepulse command.")
# The animations ship inside the SidePulse package. Look next to the command first,
# and then where the SidePulse installer puts them.
packaged = [*Path(cli).resolve().parent.parent.glob(
    "lib/python3*/site-packages/sidepulse/resources/animations"),
    *Path.home().glob(bridge.APP_ANIMATIONS)]
if not packaged:
    sys.exit("Cannot find the animations of this SidePulse install.")

root = Path(tempfile.mkdtemp(dir="/tmp"))
# The long name makes the path of the service socket too long, so the service
# moves it to /tmp. The bridge must find it there.
home = root / ("a-long-home-folder-name-" * 4)
state_dir = home / ".local" / "state" / "sidepulse" / "agent-monitor"
volumes = root / "Volumes"
device = volumes / "SidePulse" / "LEDS.LED"
device.parent.mkdir(parents=True)
home.mkdir()
device.write_text("off\n")
RED = "#FF0000 0.3s cosine\n"
ALERT = "off\n#0080FF 0.5s pulse\nrepeat\n"

service_env = {**os.environ, "HOME": str(home), "SIDEPULSE_MOUNT_ROOTS": str(volumes)}
for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
    service_env.pop(name, None)

log: dict[str, list[str]] = defaultdict(list)
observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
observer.on_message = lambda _c, _u, message: log[message.topic].append(message.payload.decode())
observer.connect(HOST, PORT)
observer.subscribe([("homeassistant/#", 1), ("sidepulse/#", 1)])
observer.loop_start()
publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
publisher.connect(HOST, PORT)
publisher.loop_start()

# An earlier run can leave retained messages on the broker. Remove them, so that
# every payload seen below comes from this run.
for topic in (bridge.STATE, bridge.AVAILABILITY, bridge.LIGHT, bridge.LIGHT_AVAILABILITY,
              bridge.LIGHT_ATTRIBUTES, *bridge.discovery({})):
    publisher.publish(topic, b"", qos=1, retain=True).wait_for_publish()
time.sleep(1)
log.clear()

service_log = (root / "service.log").open("w+")
bridge_log = (root / "bridge.log").open("w+")


def last(topic: str) -> str | None:
    return log[topic][-1] if log[topic] else None


def state() -> dict:
    return json.loads(last(bridge.STATE) or "{}")


def light() -> dict:
    return json.loads(last(bridge.LIGHT) or "{}")


def on_device() -> str:
    return bridge.body(device.read_text())


def fail(what: str) -> None:
    print(f"FAIL {what}\n     device={device.read_text()!r}")
    print(f"     last={ {t: v[-1] for t, v in log.items() if 'config' not in t and v} }")
    for name, file in (("bridge", bridge_log), ("service", service_log)):
        file.seek(0)
        print(f"     {name} log: {file.read()[-1500:]!r}")
    sys.exit(1)


def wait(what: str, predicate, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            print(f"ok   {what}")
            return
        time.sleep(0.2)
    fail(what)


def stays(what: str, predicate, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not predicate():
            fail(what)
        time.sleep(0.2)
    print(f"ok   {what}")


def settled(what: str, differs_from: str) -> str:
    """Return the program on the device once the service has stopped changing it."""
    wait(what, lambda: on_device() != differs_from)
    program, since = on_device(), time.monotonic()
    while time.monotonic() - since < 2:
        time.sleep(0.2)
        if on_device() != program:
            program, since = on_device(), time.monotonic()
    return program


def report(status: str, **fields) -> None:
    """Tell the service what an agent session does, as a hook does."""
    body = {"v": 1, "type": "program_status", "scope": "check-service",
            "status": {"state": status, "app": "check", **fields}}
    done = subprocess.run([cli, "report", "--json"], input=json.dumps(body), env=service_env,
                          capture_output=True, text=True)
    if done.returncode:
        fail(f"sidepulse report {status}: {done.stderr}")


def send(topic: str, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    publisher.publish(topic, text, qos=1).wait_for_publish()


service = subprocess.Popen([cli, "service", "run"], env=service_env, stdout=service_log,
                           stderr=service_log)
process = None
try:
    wait("the real service starts", lambda: subprocess.run(
        [cli, "status", "--json"], env=service_env, capture_output=True).returncode == 0, 30)
    report("working")
    working = settled("the service writes live status to the device", "off")

    env = {**os.environ, "MQTT_HOST": HOST, "MQTT_PORT": str(PORT),
           "SIDEPULSE_STATE_DIR": str(state_dir), "SIDEPULSE_VOLUMES": str(volumes),
           "SIDEPULSE_ANIMATIONS_DIR": str(packaged[0]), "SIDEPULSE_HOLD_SECONDS": str(HOLD)}
    process = subprocess.Popen(["uv", "run", "--quiet", "--script", bridge.__file__], env=env,
                               stderr=bridge_log)
    leds = f"homeassistant/light/{bridge.NODE}/leds/config"
    wait("discovery reaches the broker", lambda: last(leds))
    effects = json.loads(last(leds))["effect_list"]
    assert len(effects) > 4 and effects[-2:] == ["Solid", "Custom"], effects
    print(f"ok   the effect list has the {len(effects) - 2} animations of this SidePulse")

    # The service writes the status history every 15 s.
    wait("the status history gives Working", lambda: last(bridge.AVAILABILITY) == "online"
         and state().get("display_status") == "Working" and state()["needs_input"] == "OFF", 40)
    wait("the light mirrors the program of the service",
         lambda: last(bridge.LIGHT_AVAILABILITY) == "online" and light().get("state") == "ON")
    report("blocked", kind="permission")
    wait("a waiting agent turns needs_input ON", lambda: state().get("needs_input") == "ON", 40)
    waiting = settled("the service shows the waiting agent on the device", working)

    # An alert plays, and then the service takes the device back by itself.
    send(bridge.SHOW, {"program": ALERT, "seconds": 3})
    wait("an alert plays on the device", lambda: on_device() == bridge.body(ALERT))
    wait("the service shows live status again after the alert", lambda: on_device() == waiting)

    # A colour holds through a change of the agent status, and past the time the
    # service keeps one push, because the bridge sends it again.
    send(bridge.LIGHT_SET, {"state": "ON", "color": {"r": 255, "g": 0, "b": 0}})
    wait("a colour from Home Assistant reaches the device", lambda: on_device() == bridge.body(RED))
    report("working")
    stays(f"the colour stays for {HOLD * 2:.0f} s, twice the hold of the service",
          lambda: on_device() == bridge.body(RED), HOLD * 2)
    wait("the light shows the held colour", lambda: light().get("effect") == "Solid"
         and light()["color"] == {"r": 255, "g": 0, "b": 0})

    send(bridge.LIGHT_SET, {"state": "ON"})
    wait("a plain on gives the device back to the service", lambda: on_device() == working)
    stays("the device stays on live status", lambda: on_device() == working, 8)

    send(bridge.LIGHT_SET, {"state": "ON", "effect": effects[0]})
    wait(f"the effect {effects[0]} reaches the device", lambda: on_device() != working)
    wait("the light names the effect", lambda: light().get("effect") == effects[0])
    send(bridge.LIGHT_SET, {"state": "OFF"})
    wait("off turns the device dark", lambda: on_device() == "off")
    wait("the light is OFF", lambda: light() == {"state": "OFF"})
    send(bridge.LIGHT_SET, {"state": "ON"})
    wait("live status returns after off", lambda: on_device() == working)

    bridge_log.seek(0)
    errors = [line for line in bridge_log.read().splitlines() if "error" in line.lower()]
    assert not errors, errors
    print("ok   the bridge logged no error")
    print("ok: all checks passed")
finally:
    for started in (process, service):
        if started:
            started.terminate()
            started.wait(timeout=10)
    for topic in (bridge.SHOW, bridge.LIGHT_SET):
        publisher.publish(topic, b"", qos=1, retain=True).wait_for_publish()
    shutil.rmtree(root, ignore_errors=True)
