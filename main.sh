#!/usr/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
SCRIPTS_DIR="$REPO_ROOT/scripts"
DOTFILES_DIR="$REPO_ROOT/dotfiles"

# copy dotfiles (use dotfiles/. to include hidden files and avoid zsh glob "no matches" errors)
cp -r "$DOTFILES_DIR"/. ~/

# Install Homebrew
sh "$SCRIPTS_DIR/brew_install.sh"

# Setup default settings
sh "$SCRIPTS_DIR/default_setup.sh"

# Setup config settings
sh "$SCRIPTS_DIR/setup_config.sh"

# Install neovimconfig
curl -fLo ~/.vim/autoload/plug.vim --create-dirs \
    https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim

git clone https://github.com/tomasiser/vim-code-dark.git ~/.config/nvim/bundle/vim-code-dark.git
ln -s ~/.config/nvim/bundle/vim-code-dark.git/colors/codedark.vim ~/.config/nvim/colors/codedark.vim

cp config/init.vim ~/.config/nvim/

# Install Vim plugins automatically
nvim -c 'PlugInstall' -c 'qa'

echo """
Optional: Install GitHub Copilot
Run the following commands:
# terminal
git clone https://github.com/github/copilot.vim \\
   ~/.config/nvim/pack/github/start/copilot.vim
# neovim
nvim -c 'Copilot setup'
"""

echo "Please restart your terminal."
