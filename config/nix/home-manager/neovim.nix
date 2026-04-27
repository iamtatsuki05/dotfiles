{ pkgs, ... }:

{
  programs.neovim.enable = true;
  programs.neovim.defaultEditor = true;
  programs.neovim.viAlias = true;
  programs.neovim.vimAlias = true;
  programs.neovim.withPython3 = true;
  programs.neovim.withRuby = true;
  programs.neovim.extraPackages = with pkgs; [
    fzf
    ripgrep
  ];
  programs.neovim.plugins = with pkgs.vimPlugins; [
    vim-airline
    vim-airline-themes
    vim-code-dark
    vim-fern
    vim-gitgutter
    vim-fugitive
    vim-rhubarb
    fzf-vim
  ];
  programs.neovim.extraConfig = builtins.readFile ../../nvim/init.vim;
}
