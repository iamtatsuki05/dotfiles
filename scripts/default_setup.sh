#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Configure macOS default settings for better developer experience
# =============================================================================

# Keyboard repeat settings for faster cursor movement
readonly INITIAL_KEY_REPEAT=12  # Delay before key repeat starts (lower = faster)
readonly KEY_REPEAT=1           # Key repeat rate (lower = faster)

main() {
  echo "Configuring keyboard repeat settings..."
  defaults write -g InitialKeyRepeat -int "$INITIAL_KEY_REPEAT"
  defaults write -g KeyRepeat -int "$KEY_REPEAT"
  echo "Keyboard settings configured (InitialKeyRepeat=$INITIAL_KEY_REPEAT, KeyRepeat=$KEY_REPEAT)"
}

main "$@"
