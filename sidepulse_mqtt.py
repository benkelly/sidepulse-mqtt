#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["paho-mqtt>=2,<3"]
# ///
"""Bridge the SidePulse status-bar app on this Mac to Home Assistant over MQTT.

Publishes the agent status and the device LEDs with MQTT discovery. Passes show
requests and light commands from MQTT to the app socket. Settings come from
environment variables: MQTT_HOST (required), MQTT_PORT, MQTT_USERNAME,
MQTT_PASSWORD, SIDEPULSE_STATE_DIR, SIDEPULSE_VOLUMES, SIDEPULSE_ANIMATIONS_DIR.
"""
from __future__ import annotations

import functools
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

STATE_DIR = Path(
    os.environ.get("SIDEPULSE_STATE_DIR", "~/.local/state/sidepulse/agent-monitor")
).expanduser()
VOLUMES = Path(os.environ.get("SIDEPULSE_VOLUMES", "/Volumes"))
APP_ANIMATIONS = (
    ".local/share/sidepulse/venv/lib/python3*/site-packages/sidepulse/resources/animations"
)
HOST_NAME = socket.gethostname().split(".")[0]
NODE = "sidepulse_" + (re.sub(r"[^a-z0-9_]+", "_", HOST_NAME.lower()).strip("_") or "mac")
BASE = f"sidepulse/{NODE}"
AVAILABILITY, STATE, SHOW = f"{BASE}/availability", f"{BASE}/state", f"{BASE}/show"
LIGHT, LIGHT_SET = f"{BASE}/light", f"{BASE}/light/set"
LIGHT_AVAILABILITY, LIGHT_ATTRIBUTES = f"{BASE}/light/availability", f"{BASE}/light/attributes"
STATUSES = ["Idle", "Working", "Ask", "Done"]
POLL_SECONDS = 5
STALE_SECONDS = 60
DEFAULT_SHOW_SECONDS = 10
SOLID, CUSTOM = "Solid", "Custom"


def discovery(animations: dict[str, str]) -> dict[str, dict]:
    device = {
        "identifiers": [NODE],
        "name": f"SidePulse {HOST_NAME}",
        "manufacturer": "InteliWEAR",
        "model": "SidePulse",
    }
    common = {"state_topic": STATE, "availability_topic": AVAILABILITY, "device": device}
    return {
        f"homeassistant/sensor/{NODE}/agent_status/config": {
            **common,
            "name": "Agent status",
            "unique_id": f"{NODE}_agent_status",
            "device_class": "enum",
            "options": STATUSES,
            "value_template": "{{ value_json.display_status }}",
            "icon": "mdi:robot",
        },
        f"homeassistant/binary_sensor/{NODE}/needs_input/config": {
            **common,
            "name": "Needs input",
            "unique_id": f"{NODE}_needs_input",
            "value_template": "{{ value_json.needs_input }}",
            "icon": "mdi:account-alert",
        },
        f"homeassistant/light/{NODE}/leds/config": {
            "availability": [{"topic": AVAILABILITY}, {"topic": LIGHT_AVAILABILITY}],
            "availability_mode": "all",
            "device": device,
            "name": "LEDs",
            "unique_id": f"{NODE}_leds",
            "schema": "json",
            "state_topic": LIGHT,
            "command_topic": LIGHT_SET,
            "json_attributes_topic": LIGHT_ATTRIBUTES,
            "supported_color_modes": ["rgb"],
            "effect": True,
            "effect_list": [*animations, SOLID, CUSTOM],
            "icon": "mdi:led-strip-variant",
        },
    }


@functools.cache
def warn_once(message: str) -> None:
    print(f"sidepulse-mqtt: {message}", file=sys.stderr, flush=True)


def last_record(path: Path) -> dict | None:
    """Return the newest complete record of the status history, or None."""
    # NOTE: reads the file that the Python app writes for its History tab. After the
    # Rust cutover, read the service state instead (inteliwear/sidepulse#55).
    try:
        with path.open("rb") as file:
            file.seek(max(0, file.seek(0, os.SEEK_END) - 65536))
            lines = file.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue  # The app can be in the middle of an append.
    return None


def is_fresh(record: dict) -> bool:
    try:
        recorded = datetime.fromisoformat(str(record["recorded_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    return (datetime.now(timezone.utc) - recorded).total_seconds() < STALE_SECONDS


def state_payload(record: dict) -> str:
    status = record.get("display_status")
    return json.dumps(
        {
            "agent_status": record.get("agent_status"),
            "display_status": status,
            "needs_input": "ON" if status == "Ask" else "OFF",
        },
        sort_keys=True,
    )


def device_program() -> tuple[str, int] | None:
    """Return the program on the mounted SidePulse device and its LED count, or None."""
    try:
        for volume in sorted(VOLUMES.iterdir()):
            target = volume / "LEDS.LED"
            if volume.name.lower().startswith("sidepulse") and target.is_file():
                led_count = 2 if "dot" in volume.name.lower() else 8
                return target.read_text(errors="replace"), led_count
    except OSError as exc:
        warn_once(f"cannot read the device: {exc}")
    return None


def builtin_animations(led_count: int) -> dict[str, str]:
    """Map each built-in app animation name to its program for this device."""
    configured = os.environ.get("SIDEPULSE_ANIMATIONS_DIR")
    directory = Path(configured) if configured else next(Path.home().glob(APP_ANIMATIONS), None)
    animations: dict[str, str] = {}
    for path in sorted(directory.glob("*.LED")) if directory else ():
        name, _, variant = path.stem.rpartition("-")
        if variant not in ("2", "8"):
            name = path.stem
        elif variant != str(led_count):
            continue
        if name not in ("off", "immediate-off"):
            animations[name.replace("-", " ").title()] = path.read_text()
    return animations


def body(program: str) -> str:
    """Return the program without brightness lines, which the app adds."""
    lines = (line.strip() for line in program.splitlines())
    return "\n".join(
        line for line in lines if line and not re.fullmatch(r"brightness\s+\d+", line, re.I)
    )


def solid_program(rgb: str) -> str:
    return f"#{rgb.upper()} 0.3s cosine\n"


def light_state(program: str, animations: dict[str, str]) -> dict:
    """Describe a device program as Home Assistant light state."""
    colors = [color for color in re.findall(r"#([0-9A-Fa-f]{6})", program) if int(color, 16)]
    if not colors:
        return {"state": "OFF"}
    names = {body(text): name for name, text in animations.items()}
    names[body(solid_program(colors[0]))] = SOLID
    r, g, b = bytes.fromhex(colors[0])
    return {
        "state": "ON",
        "color_mode": "rgb",
        "color": {"r": r, "g": g, "b": b},
        "effect": names.get(body(program), CUSTOM),
    }


def light_request(command: dict, animations: dict[str, str]) -> dict:
    """Turn a Home Assistant light command into a request for the app socket."""
    if command.get("state") == "OFF":
        program = "off\n"
    elif "color" in command:
        rgb = "".join(f"{int(command['color'][key]):02X}" for key in "rgb")
        program = solid_program(rgb)
    elif command.get("effect") in animations:
        program = animations[command["effect"]]
    else:
        return {"command": "clear"}  # A plain "on" returns the device to live status.
    return {"command": "show", "program": program, "hold": True}


def app_request(message: dict) -> str:
    """Send one request to the app socket and return its reply."""
    # NOTE: uses the socket commands from inteliwear/sidepulse#56. After the Rust
    # cutover, use the interface that inteliwear/sidepulse#55 settles on.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(STATE_DIR / "events.sock"))
        client.sendall(json.dumps(message).encode())
        client.shutdown(socket.SHUT_WR)
        reply = b"".join(iter(lambda: client.recv(1024), b"")).decode(errors="replace")
    return reply or "error: no reply, so the app has no show command yet"


def main() -> int:
    host = os.environ.get("MQTT_HOST")
    if not host:
        print("sidepulse-mqtt: set MQTT_HOST", file=sys.stderr)
        return 2
    device = device_program()
    animations = builtin_animations(device[1] if device else 8)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NODE)
    if os.environ.get("MQTT_USERNAME"):
        client.username_pw_set(os.environ["MQTT_USERNAME"], os.environ.get("MQTT_PASSWORD"))
    client.will_set(AVAILABILITY, "offline", qos=1, retain=True)
    sent: dict[str, str] = {}

    def publish(topic: str, payload: str) -> None:
        if sent.get(topic) != payload:
            client.publish(topic, payload, qos=1, retain=True)
            sent[topic] = payload

    def on_connect(client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code.is_failure:
            print(f"sidepulse-mqtt: connect failed: {reason_code}", file=sys.stderr, flush=True)
            return
        sent.clear()
        for topic, config in discovery(animations).items():
            publish(topic, json.dumps(config))
        client.subscribe([(SHOW, 1), (LIGHT_SET, 1)])

    def on_message(_client, _userdata, message) -> None:
        if message.retain:
            return  # A retained command would play again after every reconnect.
        text = message.payload.decode(errors="replace")
        kind = "light" if message.topic == LIGHT_SET else "show"
        try:
            if kind == "light":
                request = light_request(json.loads(text), animations)
            else:
                plain = not text.lstrip().startswith("{")
                alert = {"program": text} if plain else json.loads(text)
                seconds = float(alert.get("seconds", DEFAULT_SHOW_SECONDS))
                request = {"command": "show", "program": alert["program"], "seconds": seconds}
            reply = app_request(request)
        except Exception as exc:
            reply = f"error: {exc}"
        else:
            if kind == "light" and reply == "ok" and "program" in request:
                # Show the push at once. The next poll reads the device again.
                state = light_state(request["program"], animations)
                publish(LIGHT, json.dumps(state, sort_keys=True))
        print(f"sidepulse-mqtt: {kind}: {reply}", file=sys.stderr, flush=True)

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_connect_fail = lambda *_: print(
        "sidepulse-mqtt: cannot reach the broker, retrying", file=sys.stderr, flush=True
    )
    client.connect_async(host, int(os.environ.get("MQTT_PORT", "1883")))
    client.loop_start()
    while True:
        if client.is_connected():
            record = last_record(STATE_DIR / "status-history.jsonl")
            fresh = record is not None and is_fresh(record)
            publish(AVAILABILITY, "online" if fresh else "offline")
            if fresh:
                publish(STATE, state_payload(record))
            device = device_program()
            publish(LIGHT_AVAILABILITY, "online" if device else "offline")
            if device:
                publish(LIGHT, json.dumps(light_state(device[0], animations), sort_keys=True))
                publish(LIGHT_ATTRIBUTES, json.dumps({"program": device[0]}))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
