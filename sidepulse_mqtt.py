#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["paho-mqtt>=2,<3"]
# ///
"""Bridge the SidePulse service on this Mac to Home Assistant over MQTT.

Publishes the agent status and the device LEDs with MQTT discovery. Passes
alerts and light commands from MQTT to the SidePulse service. Settings come from
environment variables: MQTT_HOST (required), MQTT_PORT, MQTT_USERNAME,
MQTT_PASSWORD, SIDEPULSE_STATE_DIR, SIDEPULSE_VOLUMES, SIDEPULSE_ANIMATIONS_DIR,
SIDEPULSE_HOLD_SECONDS.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

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
MAX_SHOW_SECONDS = 60
# The service keeps a pushed program for one hour at most. A colour or an effect
# from Home Assistant stays until you change it, so the bridge sends it again
# before the hour ends.
HOLD_SECONDS = float(os.environ.get("SIDEPULSE_HOLD_SECONDS", "3600"))
RENEW_SECONDS = HOLD_SECONDS * 5 / 6
DEVICE_NAMES = ("sidepulse", "pulsedot")  # The Dot mounts as PulseDot.
BRIGHTNESS = re.compile(r"^\s*brightness\s+\d+\s*$", re.I | re.M)
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


class Device(NamedTuple):
    target: Path  # The LEDS.LED file of the mounted device.
    program: str
    led_count: int


def last_record(path: Path) -> dict | None:
    """Return the newest complete record of the status history, or None."""
    # NOTE: reads the file that the SidePulse service writes for the History tab,
    # every 15 s. After the Rust cutover, read the service state instead
    # (inteliwear/sidepulse#55).
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


def mounted_device() -> Device | None:
    """Return the mounted SidePulse device with the program that it plays, or None."""
    try:
        for volume in sorted(VOLUMES.iterdir()):
            target = volume / "LEDS.LED"
            if volume.name.lower().startswith(DEVICE_NAMES) and target.is_file():
                led_count = 2 if "dot" in volume.name.lower() else 8
                return Device(target, target.read_text(errors="replace"), led_count)
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
    """Return the program without brightness lines, which the service adds."""
    lines = (line.strip() for line in program.splitlines())
    return "\n".join(line for line in lines if line and not BRIGHTNESS.fullmatch(line))


def with_brightness(program: str, current: str) -> str:
    """Give a program the brightness line of the program that the device plays."""
    # The service writes its own programs with the brightness from the SidePulse
    # settings, but it writes a pushed program as it is. Without this line a pushed
    # colour would ignore the brightness setting.
    line = BRIGHTNESS.search(current)
    if line is None or BRIGHTNESS.search(program):
        return program
    return f"{line.group().strip()}\n{program}"


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


def light_program(command: dict, animations: dict[str, str]) -> str | None:
    """Turn a Home Assistant light command into the program to hold, or None."""
    if command.get("state") == "OFF":
        return "off\n"
    if "color" in command:
        rgb = "".join(f"{int(command['color'][key]):02X}" for key in "rgb")
        return solid_program(rgb)
    if command.get("effect") in animations:
        return animations[command["effect"]]
    return None  # A plain "on" returns the device to live status.


def alert(text: str) -> tuple[str, float]:
    """Read an alert from the show topic: the program and the seconds that it plays."""
    plain = not text.lstrip().startswith("{")
    request = {"program": text} if plain else json.loads(text)
    seconds = float(request.get("seconds", DEFAULT_SHOW_SECONDS))
    return request["program"], min(max(seconds, 1), MAX_SHOW_SECONDS)


def service_socket() -> Path:
    """Return the path of the socket of the SidePulse service."""
    # A Unix socket path has a length limit. The service moves the socket to /tmp
    # when the state directory is too long, and this is its rule.
    path = STATE_DIR / "program-status.sock"
    if len(str(path).encode()) > 96:
        digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        return Path("/tmp") / f"sidepulse-status-{os.getuid()}" / f"{digest}.sock"
    return path


def write_led(program: str, target: Path, seconds: float) -> str:
    """Ask the service to write a program to the device and to keep it for some seconds.

    After that time the service writes the live agent status again. A time of 0
    therefore returns the device to live status at once.
    """
    # NOTE: write_led is the command that the SidePulse menu uses for a preview. It
    # is not a documented interface for other programs. After the Rust cutover, use
    # the interface that inteliwear/sidepulse#55 settles on.
    message = {"v": 1, "type": "write_led", "program": program, "target": str(target),
               "hold_seconds": seconds}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(service_socket()))
            client.sendall(json.dumps(message).encode())
            client.shutdown(socket.SHUT_WR)
            reply = json.loads(b"".join(iter(lambda: client.recv(4096), b"")) or b"{}")
    except (FileNotFoundError, ConnectionRefusedError):
        return "error: the SidePulse service does not run"
    except (OSError, ValueError) as exc:
        return f"error: {exc}"
    return "ok" if reply.get("ok") else f"error: {reply.get('error', 'no reply')}"


def main() -> int:
    host = os.environ.get("MQTT_HOST")
    if not host:
        print("sidepulse-mqtt: set MQTT_HOST", file=sys.stderr)
        return 2
    device = mounted_device()
    animations = builtin_animations(device.led_count if device else 8)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NODE)
    if os.environ.get("MQTT_USERNAME"):
        client.username_pw_set(os.environ["MQTT_USERNAME"], os.environ.get("MQTT_PASSWORD"))
    client.will_set(AVAILABILITY, "offline", qos=1, retain=True)
    sent: dict[str, str] = {}
    # The program that Home Assistant holds on the device, and the time to send it
    # again. Empty while the device shows live status. MQTT messages arrive on
    # another thread, so the lock guards it.
    hold: dict = {}
    lock = threading.Lock()

    def publish(topic: str, payload: str) -> None:
        if sent.get(topic) != payload:
            client.publish(topic, payload, qos=1, retain=True)
            sent[topic] = payload

    def push(program: str | None, seconds: float, keep: bool = False) -> tuple[str, str]:
        """Write a program through the service. Return the reply and the program.

        With keep, the bridge sends the program again before the service drops it.
        """
        device = mounted_device()
        if device is None:
            return "error: no SidePulse device is mounted", ""
        if program is None:
            # Back to live status. The service needs a program even for a hold of
            # 0 s, so write the current one again. It then replaces it.
            program, seconds = device.program, 0
        else:
            program = with_brightness(program, device.program)
        with lock:
            reply = write_led(program, device.target, seconds)
            hold.clear()  # Every push ends the hold before it.
            if reply == "ok" and keep and seconds:
                hold.update(program=program, renew=time.monotonic() + RENEW_SECONDS)
        return reply, program

    def keep_hold(device: Device) -> None:
        """Send the held program again before the service drops it. Needs the lock."""
        if not hold:
            return
        if body(device.program) != body(hold["program"]):
            # The service or the SidePulse menu wrote the device. The hold is over.
            hold.clear()
        elif time.monotonic() >= hold["renew"]:
            reply = write_led(hold["program"], device.target, HOLD_SECONDS)
            if reply == "ok":
                hold["renew"] = time.monotonic() + RENEW_SECONDS
            else:
                hold.clear()
                print(f"sidepulse-mqtt: hold: {reply}", file=sys.stderr, flush=True)

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
                held = light_program(json.loads(text), animations)
                reply, program = push(held, HOLD_SECONDS, keep=True)
            else:
                reply, program = push(*alert(text))
        except Exception as exc:
            reply = f"error: {exc}"
        else:
            if kind == "light" and reply == "ok" and held is not None:
                # Show the push at once. The next poll reads the device again.
                state = light_state(program, animations)
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
        # A hold must outlive a broker that is down for a while, so this does not
        # wait for the connection. The lock stops a push from landing between the
        # read of the device and the check of the hold.
        with lock:
            device = mounted_device()
            if device:
                keep_hold(device)
        if client.is_connected():
            record = last_record(STATE_DIR / "status-history.jsonl")
            fresh = record is not None and is_fresh(record)
            publish(AVAILABILITY, "online" if fresh else "offline")
            if fresh:
                publish(STATE, state_payload(record))
            publish(LIGHT_AVAILABILITY, "online" if device else "offline")
            if device:
                state = light_state(device.program, animations)
                publish(LIGHT, json.dumps(state, sort_keys=True))
                # Blueprints read show_topic, so users pick the light, not a topic.
                attributes = {"program": device.program, "show_topic": SHOW}
                publish(LIGHT_ATTRIBUTES, json.dumps(attributes))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
