#!/bin/bash
set -euo pipefail

WEBHOOK_URL="http://localhost:8000/api/v1/orders/notify"
FLUIDEFY_HOME="/home/fluidefy/.local"
FLUIDEFY_VERSION="Fluid-efy-linux-1.2.0.2"

WATCH_DIR="$FLUIDEFY_HOME/$FLUIDEFY_VERSION/input/sisre2fluide"
ERROR_DIR="$FLUIDEFY_HOME/$FLUIDEFY_VERSION/errors"
PROCESSED_DIR="$FLUIDEFY_HOME/$FLUIDEFY_VERSION/processed"


# Ensure runtime directories exist
mkdir -p "$WATCH_DIR" "$PROCESSED_DIR" "$ERROR_DIR"

echo "Starting Fluidefy XML watcher on $WATCH_DIR..."

# Monitor folder for completed file writes
inotifywait -m -e close_write --format '%f' "$WATCH_DIR" | while read -r FILENAME; do
    # Skip hidden or empty files
    [[ "$FILENAME" =~ ^\..* ]] && continue
    [[ ! -s "$WATCH_DIR/$FILENAME" ]] && continue

    FULL_PATH="$WATCH_DIR/$FILENAME"
    echo "New file detected: $FILENAME. Notifying ingestion gateway..."

    # Construct clean payload via JSON safely, embedding the file contents
    # (base64-encoded) so the API/worker do not need access to the file on disk.
    # --rawfile reads the whole file inside jq, avoiding shell ARG_MAX limits.
    PAYLOAD=$(jq -n --rawfile data "$FULL_PATH" --arg name "$FILENAME" \
        '{file_name: $name, file_content_base64: ($data | @base64)}')

    # POST to API gateway with a timeout
    RESPONSE_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" \
        --max-time 10 \
        "$WEBHOOK_URL")

    if [ "$RESPONSE_CODE" -eq 202 ]; then
        echo "Successfully queued $FILENAME. Moving to processed."
        mv "$FULL_PATH" "$PROCESSED_DIR/"
    else
        echo "ERROR: Failed to notify API for $FILENAME. HTTP Status: $RESPONSE_CODE. Moving to error pool."
        mv "$FULL_PATH" "$ERROR_DIR/"
    fi
done