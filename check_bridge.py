#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["paho-mqtt>=2,<3"]
# ///
"""End-to-end check for sidepulse_mqtt.py against a real MQTT broker.

Needs a broker without auth at MQTT_HOST:MQTT_PORT (default 127.0.0.1:18830).
Temporary files stand in for the app: a status history, a socket, a device
volume, and animations. Exits 1 on failure.
"""
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
root = Path(tempfile.mkdtemp(dir="/tmp"))
state_dir, volumes, animations = root / "state", root / "Volumes", root / "animations"
history = state_dir / "status-history.jsonl"
device = volumes / "SidePulse" / "LEDS.LED"
for directory in (state_dir, device.parent, animations):
    directory.mkdir(parents=True)
CYAN_ROLL = "off 320ms cosine\n0:#00E5FF 760ms pulse 0ms\nrepeat\n"
AMBER = "off\n#FF3A00 1.6s pulse\nrepeat\n"
fake_animations = {"cyan-roll-8": CYAN_ROLL, "cyan-roll-2": "off\n", "amber-pulse": AMBER,
                   "off": "off\n"}
for name, program in fake_animations.items():
    (animations / f"{name}.LED").write_text(program)
device.write_text("brightness 30\n" + CYAN_ROLL)


def record(status: str, agent: str, age: int = 0) -> None:
    at = datetime.now(timezone.utc) - timedelta(seconds=age)
    line = {"recorded_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "agent_status": agent,
            "display_status": status}
    with history.open("a") as file:
        file.write(json.dumps(line) + "\n")


# The fake app socket records each show request and replies "ok".
shows: list[dict] = []
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(state_dir / "events.sock"))
server.listen(4)


def serve() -> None:
    while True:
        connection, _ = server.accept()
        with connection:
            shows.append(json.loads(b"".join(iter(lambda: connection.recv(4096), b""))))
            connection.sendall(b"ok")


threading.Thread(target=serve, daemon=True).start()

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


def last(topic: str) -> str | None:
    return log[topic][-1] if log[topic] else None


def light() -> dict:
    return json.loads(last(bridge.LIGHT) or "{}")


def wait(what: str, predicate, timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            print(f"ok   {what}")
            return
        time.sleep(0.2)
    print(f"FAIL {what}\n     last={ {t: v[-1] for t, v in log.items() if v} }\n     shows={shows}")
    sys.exit(1)


# Retained commands from before the bridge starts must not play.
publisher.publish(bridge.SHOW, "old-alert", qos=1, retain=True).wait_for_publish()
publisher.publish(bridge.LIGHT_SET, '{"state": "OFF"}', qos=1, retain=True).wait_for_publish()
record("Working", "working")
env = {**os.environ, "MQTT_HOST": HOST, "MQTT_PORT": str(PORT),
       "SIDEPULSE_STATE_DIR": str(state_dir), "SIDEPULSE_VOLUMES": str(volumes),
       "SIDEPULSE_ANIMATIONS_DIR": str(animations)}
process = subprocess.Popen(["uv", "run", "--quiet", "--script", bridge.__file__], env=env)
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

    show = {"program": "#00FF00 1s pulse", "seconds": 3}
    publisher.publish(bridge.SHOW, json.dumps(show), qos=1).wait_for_publish()
    publisher.publish(bridge.SHOW, "off", qos=1).wait_for_publish()
    wait("JSON and plain-text alerts reach the app socket", lambda: len(shows) >= 2)
    assert shows[:2] == [
        {"command": "show", "program": "#00FF00 1s pulse", "seconds": 3.0},
        {"command": "show", "program": "off", "seconds": 10.0},
    ], f"the retained old-alert must not play: {shows}"

    hold = {"command": "show", "hold": True}
    commands = [
        ({"state": "ON", "color": {"r": 255, "g": 0, "b": 0}},
         {**hold, "program": "#FF0000 0.3s cosine\n"}),
        ({"state": "ON", "effect": "Amber Pulse"}, {**hold, "program": AMBER}),
        ({"state": "OFF"}, {**hold, "program": "off\n"}),
        ({"state": "ON"}, {"command": "clear"}),
    ]
    for command, _ in commands:
        publisher.publish(bridge.LIGHT_SET, json.dumps(command), qos=1).wait_for_publish()
    wait("light commands reach the app socket", lambda: len(shows) >= 6)
    expected = [request for _, request in commands]
    assert shows[2:] == expected, f"the retained light command must not play: {shows[2:]}"
    assert any(json.loads(p).get("effect") == "Solid" for p in log[bridge.LIGHT]), log[bridge.LIGHT]

    wait("the next poll reads the device again", lambda: light() == cyan)
    device.write_text("off\n")
    wait("an off program turns the light OFF", lambda: light() == {"state": "OFF"})
    shutil.rmtree(device.parent)
    wait("no device makes the light unavailable",
         lambda: last(bridge.LIGHT_AVAILABILITY) == "offline")

    record("Ask", "waiting_for_input", age=120)
    wait("stale history turns availability offline",
         lambda: last(bridge.AVAILABILITY) == "offline")
    print("ok: all checks passed")
finally:
    process.terminate()
    process.wait(timeout=5)
    for topic in (bridge.SHOW, bridge.LIGHT_SET):
        publisher.publish(topic, b"", qos=1, retain=True).wait_for_publish()
    shutil.rmtree(root, ignore_errors=True)
