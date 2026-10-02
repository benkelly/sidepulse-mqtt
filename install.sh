#!/bin/sh
# Install sidepulse-mqtt as a LaunchAgent for the current user, and start it.
# Usage: MQTT_HOST=homeassistant.local MQTT_USERNAME=mqtt ./install.sh
# Run it again after an update. The Keychain keeps the password between runs.
set -eu

: "${MQTT_HOST:?Set MQTT_HOST to the address of your MQTT broker.}"
MQTT_PORT=${MQTT_PORT:-1883}
MQTT_USERNAME=${MQTT_USERNAME:-}
LABEL=local.sidepulse-mqtt
DIR="$HOME/.local/share/sidepulse-mqtt"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/sidepulse-mqtt.log"
UV=$(command -v uv) || { echo "Install uv first: https://docs.astral.sh/uv/" >&2; exit 1; }

# macOS can block background jobs from ~/Documents, so the job runs a copy.
mkdir -p "$DIR" "$(dirname "$PLIST")"
cp "$(dirname "$0")/sidepulse_mqtt.py" "$DIR/sidepulse_mqtt.py"

RUN="exec '$UV' run --quiet --script '$DIR/sidepulse_mqtt.py'"
if [ -n "$MQTT_USERNAME" ]; then
    if ! security find-generic-password -s sidepulse-mqtt -a "$USER" >/dev/null 2>&1; then
        echo "Type the MQTT password for $MQTT_USERNAME. The Keychain keeps it."
        security add-generic-password -s sidepulse-mqtt -a "$USER" -l "SidePulse MQTT bridge" -w
    fi
    # The job reads the password from the Keychain when it starts.
    # The plist therefore contains no secret.
    RUN="MQTT_PASSWORD=\"\$(/usr/bin/security find-generic-password -s sidepulse-mqtt -a '$USER' -w)\" || exit 1; export MQTT_PASSWORD; $RUN"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/sh</string>
		<string>-c</string>
		<string>$RUN</string>
	</array>
	<key>EnvironmentVariables</key>
	<dict>
		<key>MQTT_HOST</key>
		<string>$MQTT_HOST</string>
		<key>MQTT_PORT</key>
		<string>$MQTT_PORT</string>
		<key>MQTT_USERNAME</key>
		<string>$MQTT_USERNAME</string>
		<key>PATH</key>
		<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
	</dict>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>StandardOutPath</key>
	<string>$LOG</string>
	<key>StandardErrorPath</key>
	<string>$LOG</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

# The old job can need a moment to stop, so retry the start a few times.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5; do
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null && break
    sleep 1
done
launchctl print "gui/$(id -u)/$LABEL" >/dev/null
echo "sidepulse-mqtt runs. Log: $LOG"
