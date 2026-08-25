### Key Security & Reliability Design Principles:
1. **At-Least-Once Delivery**: Files remain in an inbound folder or a processed/error state directory locally until safely saved to PostgreSQL.
2. **Idempotency**: A cryptographic hash (`MD5`) of each file content is calculated before database insertion. Duplicate hashes are caught via database uniqueness constraints to prevent multi-processing or re-delivery issues.
3. **Decoupled Architecture**: The API ingestion webhook returns an immediate `202 Accepted` status to the file watcher script, shifting heavy file parsing, hashing, and database writes to asynchronous worker pools.

---

## 2. Component Specifications

### Component 1: Linux File Watcher (systemd + Bash + inotify)
- **Mechanism**: Use `inotify-tools` to monitor the folder for filesystem events natively. Specifically monitor the `close_write` event to ensure the file is completely written by Fluidefy before triggering actions.
- **Error Handling**: Moving files to a `.processed/` or `.error/` directory prevents the watcher from re-processing files if the webhook fails.

### Component 2: API Gateway & Celery Broker (FastAPI + Redis)
- **Web Framework**: FastAPI handles high-concurrency requests with structural type safety via Pydantic.
- **Asynchronous Processing**: Celery handles worker distribution. Redis acts as an in-memory transport broker.

### Component 3: Data Staging Layer (PostgreSQL)
- **Purpose**: Acts as a persistent audit log and message buffer. Odoo can safely query this table via XML-RPC/JSON-RPC or native Odoo modules without stressing live API pipelines.

---

## 3. Step-by-Step Implementation Guide

### Step 1: Linux Environment Setup & Bash File Watcher

Create a systemd unit file and an execution script that monitors `/var/lib/fluidefy/orders/inbound/`.

#### Bash Watcher Script (`/usr/local/bin/fluidefy_watcher.sh`)
```bash
#!/bin/bash
set -euo pipefail

WATCH_DIR="/var/lib/fluidefy/orders/inbound"
PROCESSED_DIR="/var/lib/fluidefy/orders/processed"
ERROR_DIR="/var/lib/fluidefy/orders/error"
WEBHOOK_URL="http://localhost:8000/api/v1/orders/notify"

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

    # Construct clean payload via JSON safely
    PAYLOAD=$(jq -n --arg path "$FULL_PATH" --arg name "$FILENAME" '{file_path: $path, file_name: $name}')

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

## 6. Instructions for the Coding Agent
When implementing this system, follow these deployment steps:

    Verify that inotify-tools and jq are installed on the target Linux system (sudo apt-get install inotify-tools jq).

    Create the systemd configuration file under /etc/systemd/system/fluidefy-watcher.service, enable it via systemctl enable, and start it via systemctl start.

    Set up a Docker Compose file or local environment services for Redis and PostgreSQL.

    Generate the complete FastAPI and Celery files using the modular structure provided in Section 4.

    Use background orchestration tools like uvicorn main:app --reload to test the API endpoint and celery -A tasks.celery_app worker --loglevel=info to run the task consumer.