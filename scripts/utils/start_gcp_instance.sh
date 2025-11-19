#!/bin/bash

set -euo pipefail

# =============================================================================
# Start GCP instance with automatic retry logic
# =============================================================================

# Configuration - Update these values for your instance
readonly INSTANCE_NAME="[インスタンス名]"
readonly ZONE_NAME="[Zone名]"

# Retry configuration
readonly MIN_SLEEP_SEC=90
readonly MAX_SLEEP_SEC=120
readonly MAX_RETRIES=100  # Safety limit to prevent infinite loops

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
log_info() {
  echo "[INFO] $*"
}

log_error() {
  echo "[ERROR] $*" >&2
}

random_sleep_seconds() {
  echo $((RANDOM % 31 + MIN_SLEEP_SEC))
}

start_instance() {
  gcloud compute instances start "$INSTANCE_NAME" --zone="$ZONE_NAME"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  local start_time
  local num_try=0

  start_time=$(date +%s)
  log_info "Starting GCP instance: $INSTANCE_NAME (zone: $ZONE_NAME)"

  while true; do
    num_try=$((num_try + 1))

    if [[ $num_try -gt $MAX_RETRIES ]]; then
      log_error "Maximum retry attempts ($MAX_RETRIES) reached. Aborting."
      exit 1
    fi

    log_info "Attempt $num_try: Starting instance..."

    if start_instance; then
      local end_time
      local elapsed_sec

      end_time=$(date +%s)
      elapsed_sec=$((end_time - start_time))

      log_info "Instance started successfully!"
      log_info "Total attempts: $num_try"
      log_info "Elapsed time: ${elapsed_sec}s"
      exit 0
    fi

    local sleep_time
    sleep_time=$(random_sleep_seconds)

    log_error "Failed to start instance (attempt $num_try)"
    log_info "Retrying in ${sleep_time}s..."
    sleep "$sleep_time"
  done
}

main "$@"
