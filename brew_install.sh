#!/usr/bin/zsh

/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> /Users/$USER/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
brew install cask

# Install Applications
brew install --cask clipy
brew install --cask google-japanese-ime
brew install --cask filezilla
brew install --cask zoomus

brew install --cask google-chrome
brew install --cask firefox

brew install --cask docker
brew install --cask virtualbox

brew install --cask iterm2
brew install --cask sourcetree
brew install --cask visual-studio-code
brew install --cask coteditor
brew install --cask sequel-pro
brew install --cask brave
brew install --cask warp
brew install --cask google-drive
brew install --cask adobe-acrobat-reader
brew install --cask alfred
brew install --cask android-studio
brew install --cask raycast
brew install --cask unity-hub
brew install --cask slack

# Install CLI tools
brew install neovim
brew install tmux
brew install z
brew install anyenv
anyenv install --init
echo 'eval "$(anyenv init -)"' >> ~/.zshrc
brew install tree
brew install git-lfs
brew install ffmpeg
brew install wget
brew install git-secrets
brew install gh
brew install fzf
brew install bat
brew install ripgrep
