#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Configure macOS default settings for better developer experience
# =============================================================================

# Keyboard repeat settings for faster cursor movement
readonly INITIAL_KEY_REPEAT=12  # Delay before key repeat starts (lower = faster)
readonly KEY_REPEAT=1           # Key repeat rate (lower = faster)
readonly SUDO_LOCAL_TEMPLATE="/etc/pam.d/sudo_local.template"
readonly SUDO_LOCAL_FILE="/etc/pam.d/sudo_local"

configure_keyboard_repeat() {
  echo "Configuring keyboard repeat settings..."
  defaults write -g InitialKeyRepeat -int "$INITIAL_KEY_REPEAT"
  defaults write -g KeyRepeat -int "$KEY_REPEAT"
  echo "Keyboard settings configured (InitialKeyRepeat=$INITIAL_KEY_REPEAT, KeyRepeat=$KEY_REPEAT)"
}

configure_sudo_touch_id() {
  echo "Configuring Touch ID authentication for sudo..."

  if [ ! -f "$SUDO_LOCAL_FILE" ]; then
    if [ ! -f "$SUDO_LOCAL_TEMPLATE" ]; then
      echo "Skipped: $SUDO_LOCAL_TEMPLATE not found"
      return
    fi

    sudo cp "$SUDO_LOCAL_TEMPLATE" "$SUDO_LOCAL_FILE"
  fi

  if grep -q '^auth[[:space:]][[:space:]]*sufficient[[:space:]][[:space:]]*pam_tid\.so' "$SUDO_LOCAL_FILE"; then
    echo "Touch ID authentication for sudo is already enabled"
    return
  fi

  sudo sed -i '' \
    's/^#auth[[:space:]][[:space:]]*sufficient[[:space:]][[:space:]]*pam_tid\.so/auth sufficient pam_tid.so/' \
    "$SUDO_LOCAL_FILE"

  if grep -q '^auth[[:space:]][[:space:]]*sufficient[[:space:]][[:space:]]*pam_tid\.so' "$SUDO_LOCAL_FILE"; then
    echo "Touch ID authentication for sudo enabled"
  else
    echo "WARNING: Failed to enable Touch ID authentication for sudo" >&2
    return 1
  fi
}

main() {
  configure_keyboard_repeat
  configure_sudo_touch_id
}

main "$@"
