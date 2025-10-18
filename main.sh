#!/usr/bin/zsh

# copy dotfiles
cp -r dotfiles/* ~/

# Install Homebrew
sh brew_install.sh

# Setup default settings
sh default_setup.sh
mkdir -p ~/.config/alacritty
cp configs/alacritty.yml ~/.config/alacritty/alacritty.yml

# Install neovimconfig
curl -fLo ~/.vim/autoload/plug.vim --create-dirs \
    https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim

git clone https://github.com/tomasiser/vim-code-dark.git ~/.config/nvim/bundle/vim-code-dark.git
ln -s ~/.config/nvim/bundle/vim-code-dark.git/colors/codedark.vim ~/.config/nvim/colors/codedark.vim

git clone https://github.com/iamtatsuki05/neovim_config.git
cp neovim_config/init.vim ~/.config/nvim/

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
