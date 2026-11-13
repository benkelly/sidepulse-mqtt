#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["paho-mqtt>=2,<3"]
# ///
"""End-to-end check for sidepulse_mqtt.py against a real MQTT broker.

Needs a broker without auth at MQTT_HOST:MQTT_PORT (default 127.0.0.1:18830).
Temporary files stand in for SidePulse: a status history, the service socket, a
device volume, and animations. Exits 1 on failure.

check_service.py runs the bridge against a real SidePulse service.
"""
import atexit
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).parent))
import sidepulse_mqtt as bridge  # noqa: E402

HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
PORT = int(os.environ.get("MQTT_PORT", "18830"))
HOLD = 6.0  # The bridge sends a held program again after 5/6 of this time.
root = Path(tempfile.mkdtemp(dir="/tmp"))
atexit.register(shutil.rmtree, root, ignore_errors=True)
state_dir, volumes, animations = root / "state", root / "Volumes", root / "animations"
history = state_dir / "status-history.jsonl"
device = volumes / "SidePulse" / "LEDS.LED"
for directory in (state_dir, device.parent, animations):
    directory.mkdir(parents=True)
CYAN_ROLL = "off 320ms cosine\n0:#00E5FF 760ms pulse 0ms\nrepeat\n"
AMBER = "off\n#FF3A00 1.6s pulse\nrepeat\n"
RED = "#FF0000 0.3s cosine\n"
LIVE = "brightness 30\n" + CYAN_ROLL  # What the service shows while an agent works.
fake_animations = {"cyan-roll-8": CYAN_ROLL, "cyan-roll-2": "off\n", "amber-pulse": AMBER,
                   "off": "off\n"}
for name, program in fake_animations.items():
    (animations / f"{name}.LED").write_text(program)
device.write_text(LIVE)


def check(what: str, got, want) -> None:
    if got != want:
        print(f"FAIL {what}\n     want {want!r}\n     got  {got!r}")
        sys.exit(1)
    print(f"ok   {what}")


# ------------------------------------------------------------- functions
# These need no broker.

names = {}
for name in ("SidePulse", "SidePulsePro", "sidepulse lower", "PulseDot", "Macintosh HD",
             "My SidePulse"):
    mount = Path(tempfile.mkdtemp(dir=root))
    (mount / name).mkdir()
    (mount / name / "LEDS.LED").write_text("off\n")
    bridge.VOLUMES = mount
    found = bridge.mounted_device()
    names[name] = found and (found.target == mount / name / "LEDS.LED", found.led_count)
check("the volume names of a device, and its LED count", names, {
    "SidePulse": (True, 8), "SidePulsePro": (True, 8), "sidepulse lower": (True, 8),
    "PulseDot": (True, 2), "Macintosh HD": None, "My SidePulse": None})
empty = Path(tempfile.mkdtemp(dir=root))
(empty / "SidePulse").mkdir()
bridge.VOLUMES = empty
check("a volume without LEDS.LED is no device", bridge.mounted_device(), None)
bridge.VOLUMES = root / "missing"
check("a missing volumes folder is no device", bridge.mounted_device(), None)

check("a pushed program takes the brightness of the device", [
    bridge.with_brightness(RED, "brightness 30\noff\n"),
    bridge.with_brightness(RED, "off\n  Brightness 7 \n#000000\n"),
    bridge.with_brightness(RED, "off\n"),
    bridge.with_brightness("brightness 200\n" + RED, "brightness 30\noff\n"),
], ["brightness 30\n" + RED, "Brightness 7\n" + RED, RED, "brightness 200\n" + RED])
check("body drops brightness lines, blank lines and indents",
      bridge.body("brightness 30\n\n  off  \nBRIGHTNESS 5\n#FF0000 1s pulse\n"),
      "off\n#FF0000 1s pulse")

check("an alert is JSON or plain text, and plays for 1 s to 60 s", [
    bridge.alert('{"program": "off", "seconds": 8}'),
    bridge.alert('  {"program": "off"}'),
    bridge.alert("off\n#0080FF 0.5s pulse\n"),
    bridge.alert('{"program": "off", "seconds": 500}'),
    bridge.alert('{"program": "off", "seconds": 0}'),
], [("off", 8.0), ("off", 10.0), ("off\n#0080FF 0.5s pulse\n", 10.0), ("off", 60.0), ("off", 1.0)])

effects = {"Amber Pulse": AMBER}
check("a light command becomes the program to hold", [
    bridge.light_program({"state": "OFF"}, effects),
    bridge.light_program({"state": "ON", "color": {"r": 255, "g": 0, "b": 10}}, effects),
    bridge.light_program({"state": "ON", "effect": "Amber Pulse"}, effects),
    bridge.light_program({"state": "ON", "effect": "Unknown"}, effects),
    bridge.light_program({"state": "ON"}, effects),
], ["off\n", "#FF000A 0.3s cosine\n", AMBER, None, None])
check("a program becomes the light state", [
    bridge.light_state("off\n", effects),
    bridge.light_state("brightness 9\n#000000 1s\n", effects),
    bridge.light_state("brightness 9\n" + AMBER, effects),
    bridge.light_state("brightness 30\n#00FF80 0.3s cosine\n", effects),
    bridge.light_state("off\n#102030 1s pulse\n#405060 1s pulse\n", effects),
], [
    {"state": "OFF"},
    {"state": "OFF"},
    {"state": "ON", "color_mode": "rgb", "color": {"r": 255, "g": 58, "b": 0},
     "effect": "Amber Pulse"},
    {"state": "ON", "color_mode": "rgb", "color": {"r": 0, "g": 255, "b": 128}, "effect": "Solid"},
    {"state": "ON", "color_mode": "rgb", "color": {"r": 16, "g": 32, "b": 48},
     "effect": "Custom"},
])

os.environ["SIDEPULSE_ANIMATIONS_DIR"] = str(animations)
check("effect names come from the animation files for this LED count", [
    bridge.builtin_animations(8), bridge.builtin_animations(2)],
    [{"Amber Pulse": AMBER, "Cyan Roll": CYAN_ROLL}, {"Amber Pulse": AMBER, "Cyan Roll": "off\n"}])

now = datetime.now(timezone.utc)
stamp = lambda age: (now - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
check("a record is fresh for one minute", [
    bridge.is_fresh({"recorded_at": stamp(0)}), bridge.is_fresh({"recorded_at": stamp(50)}),
    bridge.is_fresh({"recorded_at": stamp(70)}), bridge.is_fresh({"recorded_at": "soon"}),
    bridge.is_fresh({})], [True, True, False, False, False])
check("the state payload turns Ask into needs_input", [
    json.loads(bridge.state_payload({"agent_status": "waiting_for_input", "display_status": "Ask"})),
    json.loads(bridge.state_payload({"agent_status": "working", "display_status": "Working"})),
], [
    {"agent_status": "waiting_for_input", "display_status": "Ask", "needs_input": "ON"},
    {"agent_status": "working", "display_status": "Working", "needs_input": "OFF"},
])
torn = root / "torn.jsonl"
torn.write_text('{"display_status": "Working"}\n{"display_status": "Ask"}\n{"display_sta')
check("the newest complete record wins over a half-written line",
      [bridge.last_record(torn), bridge.last_record(root / "none.jsonl")],
      [{"display_status": "Ask"}, None])

bridge.STATE_DIR = Path("/tmp/s")
short = bridge.service_socket()
bridge.STATE_DIR = Path("/tmp") / ("long" * 30)
digest = hashlib.sha256(str(bridge.STATE_DIR / "program-status.sock").encode()).hexdigest()[:16]
check("the service socket moves to /tmp when its path is too long", [short, bridge.service_socket()], [
    Path("/tmp/s/program-status.sock"),
    Path(f"/tmp/sidepulse-status-{os.getuid()}/{digest}.sock")])
bridge.STATE_DIR = state_dir
check("write_led without a service says so", bridge.write_led("off\n", device, 1),
      "error: the SidePulse service does not run")


# ------------------------------------------------------------------ bridge

def record(status: str, agent: str, age: int = 0) -> None:
    at = datetime.now(timezone.utc) - timedelta(seconds=age)
    line = {"recorded_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "agent_status": agent,
            "display_status": status}
    with history.open("a") as file:
        file.write(json.dumps(line) + "\n")


# The fake service records each write_led request and writes the program, as the
# real one does. It never restores live status. The checks do that where needed.
writes: list[dict] = []
refuse: list[str] = []  # While this holds a message, the service answers with that error.
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(state_dir / "program-status.sock"))
server.listen(4)


def serve() -> None:
    while True:
        connection, _ = server.accept()
        with connection:
            request = json.loads(b"".join(iter(lambda: connection.recv(4096), b"")))
            writes.append(request)
            if refuse:
                reply = {"ok": False, "error": refuse[0]}
            else:
                Path(request["target"]).write_text(request["program"])
                reply = {"ok": True, "target": request["target"]}
            connection.sendall(json.dumps(reply).encode())


threading.Thread(target=serve, daemon=True).start()
check("write_led reaches the service and reports its answer",
      [bridge.write_led("off\n", device, 2), writes.pop(), device.read_text()],
      ["ok", {"v": 1, "type": "write_led", "program": "off\n", "target": str(device),
              "hold_seconds": 2}, "off\n"])
refuse.append("LED program is empty.")
check("write_led reports an error from the service", bridge.write_led("", device, 2),
      "error: LED program is empty.")
refuse.clear()
writes.clear()
device.write_text(LIVE)

# The observer keeps every payload of each topic, in order.
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


def last(topic: str) -> str | None:
    return log[topic][-1] if log[topic] else None


def light() -> dict:
    return json.loads(last(bridge.LIGHT) or "{}")


def fail(what: str) -> None:
    print(f"FAIL {what}\n     last={ {t: v[-1] for t, v in log.items() if v} }\n     writes={writes}")
    sys.exit(1)


def wait(what: str, predicate, timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            print(f"ok   {what}")
            return
        time.sleep(0.2)
    fail(what)


def write(program: str, seconds: float) -> dict:
    return {"v": 1, "type": "write_led", "program": program, "target": str(device),
            "hold_seconds": seconds}


def send(topic: str, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    publisher.publish(topic, text, qos=1).wait_for_publish()


# Retained commands from before the bridge starts must not play.
publisher.publish(bridge.SHOW, "old-alert", qos=1, retain=True).wait_for_publish()
publisher.publish(bridge.LIGHT_SET, '{"state": "OFF"}', qos=1, retain=True).wait_for_publish()
record("Working", "working")
env = {**os.environ, "MQTT_HOST": HOST, "MQTT_PORT": str(PORT),
       "SIDEPULSE_STATE_DIR": str(state_dir), "SIDEPULSE_VOLUMES": str(volumes),
       "SIDEPULSE_ANIMATIONS_DIR": str(animations), "SIDEPULSE_HOLD_SECONDS": str(HOLD)}
bridge_log = (root / "bridge.log").open("w+")
process = subprocess.Popen(["uv", "run", "--quiet", "--script", bridge.__file__], env=env,
                           stderr=bridge_log)
try:
    sensor = f"homeassistant/sensor/{bridge.NODE}/agent_status/config"
    binary = f"homeassistant/binary_sensor/{bridge.NODE}/needs_input/config"
    leds = f"homeassistant/light/{bridge.NODE}/leds/config"
    wait("discovery for all three entities", lambda: all(map(last, (sensor, binary, leds))))
    config = json.loads(last(sensor))
    assert config["state_topic"] == bridge.STATE, config
    assert config["options"] == ["Idle", "Working", "Ask", "Done"], config
    config = json.loads(last(leds))
    assert config["command_topic"] == bridge.LIGHT_SET, config
    assert config["effect_list"] == ["Amber Pulse", "Cyan Roll", "Solid", "Custom"], config

    wait("online, Working, needs_input OFF", lambda: last(bridge.AVAILABILITY) == "online"
         and '"display_status": "Working"' in (last(bridge.STATE) or "")
         and '"needs_input": "OFF"' in (last(bridge.STATE) or ""))
    record("Ask", "waiting_for_input")
    wait("Ask turns needs_input ON", lambda: '"needs_input": "ON"' in (last(bridge.STATE) or ""))

    cyan = {"state": "ON", "color_mode": "rgb", "color": {"r": 0, "g": 229, "b": 255},
            "effect": "Cyan Roll"}
    wait("light mirrors the device: cyan, Cyan Roll",
         lambda: last(bridge.LIGHT_AVAILABILITY) == "online" and light() == cyan)
    attributes = json.loads(last(bridge.LIGHT_ATTRIBUTES))
    assert attributes == {"program": device.read_text(), "show_topic": bridge.SHOW}, attributes

    # Alerts. The service ends each one by itself, so the bridge keeps no hold.
    send(bridge.SHOW, {"program": "#00FF00 1s pulse", "seconds": 3})
    send(bridge.SHOW, "off")
    send(bridge.SHOW, {"program": "#0000FF 1s pulse", "seconds": 900})
    wait("JSON and plain-text alerts reach the service", lambda: len(writes) >= 3)
    check("an alert carries its seconds and the brightness of the device", writes[:3], [
        write("brightness 30\n#00FF00 1s pulse", 3.0),
        write("brightness 30\noff", 10.0),
        write("brightness 30\n#0000FF 1s pulse", 60.0),
    ])
    time.sleep(HOLD + bridge.POLL_SECONDS)
    check("an alert is not sent again, and the retained old-alert never played", len(writes), 3)

    # A colour holds until something else writes the device.
    device.write_text(LIVE)
    del writes[:]
    send(bridge.LIGHT_SET, {"state": "ON", "color": {"r": 255, "g": 0, "b": 0}})
    wait("a colour reaches the service as a hold", lambda: len(writes) >= 1)
    red = write("brightness 30\n" + RED, HOLD)
    check("the hold asks for the longest time the service allows", writes[0], red)
    wait("the light shows the pushed colour at once", lambda: light().get("effect") == "Solid")
    wait("the bridge sends a held program again before the service drops it",
         lambda: len(writes) >= 2, timeout=HOLD + 2 * bridge.POLL_SECONDS)
    check("the second request repeats the first", writes[1], red)
    device.write_text(LIVE)  # The service, or the SidePulse menu, takes the device back.
    count = len(writes)
    wait("the light follows the device back to live status", lambda: light() == cyan)
    time.sleep(HOLD + bridge.POLL_SECONDS)
    check("a hold ends when something else writes the device", len(writes), count)

    # Effect, off, and back to live status.
    del writes[:]
    send(bridge.LIGHT_SET, {"state": "ON", "effect": "Amber Pulse"})
    wait("an effect reaches the service", lambda: len(writes) >= 1)
    send(bridge.LIGHT_SET, {"state": "OFF"})
    wait("off reaches the service", lambda: len(writes) >= 2)
    send(bridge.LIGHT_SET, {"state": "ON"})
    wait("a plain on reaches the service", lambda: len(writes) >= 3)
    check("effect and off hold, and a plain on gives the device back", writes[:3], [
        write("brightness 30\n" + AMBER, HOLD),
        write("brightness 30\noff\n", HOLD),
        # A hold of 0 s with the program that is on the device already.
        write("brightness 30\noff\n", 0),
    ])
    time.sleep(HOLD + bridge.POLL_SECONDS)
    check("a plain on ends the hold, so nothing is sent again", len(writes), 3)

    # Failures go to the log of the bridge.
    del writes[:]
    refuse.append("Unsupported daemon command")
    send(bridge.LIGHT_SET, {"state": "OFF"})
    wait("a refused command reaches the service", lambda: len(writes) >= 1)
    refuse.clear()
    time.sleep(HOLD + bridge.POLL_SECONDS)
    check("a refused hold is not sent again", len(writes), 1)

    device.write_text(LIVE)
    wait("the next poll reads the device again", lambda: light() == cyan)
    device.write_text("off\n")
    wait("an off program turns the light OFF", lambda: light() == {"state": "OFF"})
    shutil.rmtree(device.parent)
    wait("no device makes the light unavailable",
         lambda: last(bridge.LIGHT_AVAILABILITY) == "offline")
    send(bridge.SHOW, "off")
    send(bridge.LIGHT_SET, "not json")

    record("Ask", "waiting_for_input", age=120)
    wait("stale history turns availability offline",
         lambda: last(bridge.AVAILABILITY) == "offline")

    def logged() -> str:
        bridge_log.seek(0)
        return bridge_log.read()

    for line in ("light: error: Unsupported daemon command",
                 "show: error: no SidePulse device is mounted",
                 "light: error: Expecting value"):
        wait(f"the log says: {line}", lambda: line in logged(), timeout=5)
    check("the log has no traceback", "Traceback" in logged(), False)
    print("ok: all checks passed")
finally:
    process.terminate()
    process.wait(timeout=5)
    for topic in (bridge.SHOW, bridge.LIGHT_SET):
        publisher.publish(topic, b"", qos=1, retain=True).wait_for_publish()
    shutil.rmtree(root, ignore_errors=True)
