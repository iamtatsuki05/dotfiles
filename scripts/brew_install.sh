#!/usr/bin/zsh

/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> /Users/$USER/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
brew install cask

brew bundle --global

echo 'eval "$(/opt/homebrew/bin/mise activate zsh)"' >> ~/.zshrc
