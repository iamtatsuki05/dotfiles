# environment
export PATH="$HOME/.local/bin:$PATH"
if [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env" ]]; then
  source "${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env"
fi

export HOMEBREW_ARCH=sandybridge
if [[ -x "$HOME/.linuxbrew/bin/brew" ]]; then
  eval "$("$HOME/.linuxbrew/bin/brew" shellenv)"
elif [[ -x "/home/linuxbrew/.linuxbrew/bin/brew" ]]; then
  eval "$("/home/linuxbrew/.linuxbrew/bin/brew" shellenv)"
elif [[ -n "${HOMEBREW_PREFIX:-}" && -x "${HOMEBREW_PREFIX}/bin/brew" ]]; then
  eval "$("${HOMEBREW_PREFIX}/bin/brew" shellenv)"
elif command -v brew >/dev/null 2>&1; then
  eval "$(brew shellenv)"
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  alias intel="env /usr/bin/arch -x86_64 /bin/zsh --login"
  alias arm="env /usr/bin/arch -arm64 /bin/zsh --login"
fi

if command -v mise >/dev/null 2>&1; then
  eval "$(mise activate zsh)"
fi

## start default apps
# tmux

# custom
export HF_HOME="/cl/home2/share/huggingface"
export HF_TOKEN_PATH="${HOME}/.cache/huggingface/token"
export HF_HUB_CACHE="${HF_HOME}/hub"
export HF_ASSETS_CACHE="${HF_HOME}/assets"

export WORK_DIR="/cl/work15"
export DATA_DIR="${WORK_DIR}/data"
mv_work() {
  cd "${WORK_DIR}/${USER}" || return
}
