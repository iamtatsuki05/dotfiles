function dotfiles_add_default_ssh_key {
  local key_path="$HOME/.ssh/id_rsa"

  if [[ -o interactive && -r "$key_path" && -S "${SSH_AUTH_SOCK:-}" ]]; then
    ssh-add -q "$key_path" 2>/dev/null || true
  fi
}

dotfiles_add_default_ssh_key
unfunction dotfiles_add_default_ssh_key
# If you come from bash you might have to change your $PATH.
# export PATH=$HOME/bin:/usr/local/bin:$PATH

# Path to your oh-my-zsh installation.
export ZSH="$HOME/.oh-my-zsh"

# Set name of the theme to load --- if set to "random", it will
# load a random theme each time oh-my-zsh is loaded, in which case,
# to know which specific one was loaded, run: echo $RANDOM_THEME
# See https://github.com/ohmyzsh/ohmyzsh/wiki/Themes
ZSH_THEME="candy"

# Set list of themes to pick from when loading at random
# Setting this variable when ZSH_THEME=random will cause zsh to load
# a theme from this variable instead of looking in $ZSH/themes/
# If set to an empty array, this variable will have no effect.
# ZSH_THEME_RANDOM_CANDIDATES=( "robbyrussell" "agnoster" )

# Uncomment the following line to use case-sensitive completion.
# CASE_SENSITIVE="true"

# Uncomment the following line to use hyphen-insensitive completion.
# Case-sensitive completion must be off. _ and - will be interchangeable.
# HYPHEN_INSENSITIVE="true"

# Uncomment one of the following lines to change the auto-update behavior
# zstyle ':omz:update' mode disabled  # disable automatic updates
# zstyle ':omz:update' mode auto      # update automatically without asking
# zstyle ':omz:update' mode reminder  # just remind me to update when it's time

# Uncomment the following line to change how often to auto-update (in days).
# zstyle ':omz:update' frequency 13

# Uncomment the following line if pasting URLs and other text is messed up.
# DISABLE_MAGIC_FUNCTIONS="true"

# Uncomment the following line to disable colors in ls.
# DISABLE_LS_COLORS="true"

# Uncomment the following line to disable auto-setting terminal title.
# DISABLE_AUTO_TITLE="true"

# Uncomment the following line to enable command auto-correction.
# ENABLE_CORRECTION="true"

# Uncomment the following line to display red dots whilst waiting for completion.
# You can also set it to another string to have that shown instead of the default red dots.
# e.g. COMPLETION_WAITING_DOTS="%F{yellow}waiting...%f"
# Caution: this setting can cause issues with multiline prompts in zsh < 5.7.1 (see #5765)
# COMPLETION_WAITING_DOTS="true"

# Uncomment the following line if you want to disable marking untracked files
# under VCS as dirty. This makes repository status check for large repositories
# much, much faster.
# DISABLE_UNTRACKED_FILES_DIRTY="true"

# Uncomment the following line if you want to change the command execution time
# stamp shown in the history command output.
# You can set one of the optional three formats:
# "mm/dd/yyyy"|"dd.mm.yyyy"|"yyyy-mm-dd"
# or set a custom format using the strftime function format specifications,
# see 'man strftime' for details.
# HIST_STAMPS="mm/dd/yyyy"

# Would you like to use another custom folder than $ZSH/custom?
# ZSH_CUSTOM=/path/to/new-custom-folder

# Which plugins would you like to load?
# Standard plugins can be found in $ZSH/plugins/
# Custom plugins may be added to $ZSH_CUSTOM/plugins/
# Example format: plugins=(rails git textmate ruby lighthouse)
# Add wisely, as too many plugins slow down shell startup.
plugins=(git)
plugins=(git zsh-syntax-highlighting)
plugins=(git zsh-syntax-highlighting zsh-completions)

# zsh-completionsの設定
function dotfiles_cleanup_stale_homebrew_completion {
  if [[ -L /opt/homebrew/share/zsh/site-functions/_brew && ! -e /opt/homebrew/share/zsh/site-functions/_brew ]]; then
    fpath=(${fpath:#/opt/homebrew/share/zsh/site-functions})
  fi
}

function dotfiles_compinit {
  dotfiles_cleanup_stale_homebrew_completion

  local dotfiles_completion_dir
  for dotfiles_completion_dir in \
    "$HOME/.linuxbrew/share/zsh/site-functions" \
    "$HOME/.linuxbrew/share/zsh-completions" \
    "/home/linuxbrew/.linuxbrew/share/zsh/site-functions" \
    "/home/linuxbrew/.linuxbrew/share/zsh-completions" \
    "/opt/homebrew/share/zsh/site-functions" \
    "/usr/local/share/zsh/site-functions"
  do
    if [[ -d "$dotfiles_completion_dir" ]]; then
      fpath=("$dotfiles_completion_dir" $fpath)
    fi
  done

  local zcompdump_dir="${XDG_CACHE_HOME:-$HOME/.cache}/zsh"
  mkdir -p "$zcompdump_dir"
  autoload -Uz compinit
  compinit -u -d "$zcompdump_dir/zcompdump-$ZSH_VERSION"
}

dotfiles_compinit
if [[ -r "$ZSH/oh-my-zsh.sh" ]]; then
  source "$ZSH/oh-my-zsh.sh"
fi

# User configuration

# export MANPATH="/usr/local/man:$MANPATH"

# You may need to manually set your language environment
# export LANG=en_US.UTF-8

# Preferred editor for local and remote sessions
# if [[ -n $SSH_CONNECTION ]]; then
#   export EDITOR='vim'
# else
#   export EDITOR='nvim'
# fi

# Compilation flags
# export ARCHFLAGS="-arch x86_64"

# Set personal aliases, overriding those provided by oh-my-zsh libs,
# plugins, and themes. Aliases can be placed here, though oh-my-zsh
# users are encouraged to define aliases within the ZSH_CUSTOM folder.
# For a full list of active aliases, run `alias`.
#
# Example aliases
# alias zshconfig="mate ~/.zshrc"
# alias ohmyzsh="mate ~/.oh-my-zsh"


# ファイル名の展開でディレクトリにマッチした場合 末尾に / を付加
setopt mark_dirs

# コマンドのスペルチェックをする
setopt correct


# コマンドライン全てのスペルチェックをする
setopt correct_all

# sudo の後ろでコマンド名を補完する
zstyle ':completion:*:sudo:*' command-path /usr/local/sbin /usr/local/bin \
                   /usr/sbin /usr/bin /sbin /bin /usr/X11R6/bin

# ps コマンドのプロセス名補完
zstyle ':completion:*:processes' command 'ps x -o pid,s,args'
# パスの最後のスラッシュを削除しない
setopt noautoremoveslash
# 自動補完は dotfiles_compinit で初期化済み
# コマンドミスを修正
setopt correct

# 補完の選択を楽にする
zstyle ':completion:*' menu select

# 補完候補をできるだけ詰めて表示する
setopt list_packed

# Optional plugins are installed by Nix, Homebrew, or the host package manager.
function dotfiles_source_if_readable {
  local source_path="$1"
  [[ -r "$source_path" ]] && source "$source_path"
}

dotfiles_source_if_readable "$HOME/.linuxbrew/share/zsh-autosuggestions/zsh-autosuggestions.zsh"
dotfiles_source_if_readable "/home/linuxbrew/.linuxbrew/share/zsh-autosuggestions/zsh-autosuggestions.zsh"
dotfiles_source_if_readable "/opt/homebrew/share/zsh-autosuggestions/zsh-autosuggestions.zsh"
dotfiles_source_if_readable "/usr/local/share/zsh-autosuggestions/zsh-autosuggestions.zsh"
dotfiles_source_if_readable "$HOME/.linuxbrew/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
dotfiles_source_if_readable "/home/linuxbrew/.linuxbrew/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
dotfiles_source_if_readable "/opt/homebrew/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
dotfiles_source_if_readable "/usr/local/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
unfunction dotfiles_source_if_readable

# commands

## Git
function gt() {
  is_in_git_repo || return
  git tag --sort -version:refname |
  fzf-down --multi --preview-window right:70% \
    --preview 'git show --color=always {} | head -200'
}

function gr() {
  is_in_git_repo || return
  git remote -v | awk '{print $1 "\t" $2}' | uniq |
  fzf-down --tac \
    --preview 'git log --oneline --graph --date=short --pretty="format:%C(auto)%cd %h%d %s" {1} | head -200' |
  cut -d$'\t' -f1
}

function gs() {
  is_in_git_repo || return
  git stash list | fzf-down --reverse -d: --preview 'git show --color=always {1}' |
  cut -d: -f1
}

function _ssh {
  compadd `fgrep 'Host ' ~/.ssh/config | awk '{print $2}' | sort`;
}

## gcp
alias fgcp="
  gcloud config configurations list \
    | awk '{ print \$1,\$3,\$4 }' \
    | column -t \
    | fzf --header-lines=1 \
    | awk '{ print \$1 }' \
    | xargs -r gcloud config configurations activate
"
alias fgcc='
  for h in $(
    gcloud \
      compute instances list \
      | fzf --header-lines=1 \
      | awk '"'"'{ print $1"@"$2 }'"'"'
  ); do
    gcloud \
      compute ssh \
      --zone ${h##*@} ${h%%@*} \
	  --tunnel-through-iap \
	  --ssh-flag="-A"
  done
'
alias fgcc_rinit='
  for h in $(
    gcloud \
      compute instances list \
      | fzf --header-lines=1 \
      | awk '"'"'{ print $1"@"$2 }'"'"'
  ); do
    gcloud \
      compute ssh \
      --zone ${h##*@} ${h%%@*} \
	  --tunnel-through-iap \
	  -dry-run
  done
'
alias fgcc_p='
  for h in $(
    gcloud \
      compute instances list \
      | fzf --header-lines=1 \
      | awk '"'"'{ print $2"@"$3 }'"'"'
  ); do
    gcloud \
      compute ssh \
      --zone ${h##*@} ${h%%@*} \
      --tunnel-through-iap \
      --ssh-flag="-A" \
      --ssh-flag="-L $4:localhost:$4
  done
'
alias fgrs='
  for h in $(
    gcloud compute instances list \
      | fzf --header-lines=1 \
      | awk "{ print \$1\"@\"\$2 }"
  ); do
    gstop_instance --zone ${h##*@} ${h%%@*};
    gstart_instance --zone ${h##*@} ${h%%@*};
  done
'
alias ginit='gcloud init'
alias gauth='gcloud auth login'
alias gls='gcloud compute instances list'
alias gstop_instance='gcloud compute instances stop'
alias gstart_instance='gcloud compute instances start'
alias gdelete_instance='gcloud compute instances delete'

# terminal
function git-current-branch {
  local branch_name
  branch_name=`git rev-parse --abbrev-ref HEAD 2> /dev/null`
  if [ -n "$branch_name" ]; then
    echo "%B%F{29}◀%f%K{29}%F{15} $branch_name %f%k%b"
  fi
}

setopt prompt_subst
function prompt-machine-emoji {
  local checksum
  local -a emojis=(🥺 🥹 🥺ྀི 😎 😇 😘 🤗 🤔 🤖 🧠 🚀 🎧 🎮 🎹 🎨)

  checksum="$(printf '%s' "${HOST:-$(hostname)}" | cksum)"
  printf '%s' "$emojis[$(( ${checksum%% *} % ${#emojis[@]} + 1 ))]"
}

PROMPT_MACHINE_EMOJI="${PROMPT_MACHINE_EMOJI:-$(prompt-machine-emoji)}"
PROMPT='%F{33}%~%f `git-current-branch`
 ${PROMPT_MACHINE_EMOJI}  ▶  '

# environment
if [[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/shell/dotfiles-shell-common.sh" ]]; then
  source "${XDG_CONFIG_HOME:-$HOME/.config}/shell/dotfiles-shell-common.sh"
fi

# start default apps
# tmux
