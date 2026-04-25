"" options
set number
set confirm
set hlsearch
set showcmd
set autoindent
set smartindent
set tabstop=4
set shiftwidth=4
set splitright
set clipboard=unnamed
set encoding=UTF-8
set fileencodings=utf-8,euc-jp,sjis
set fileencoding=utf-8
set title
set hidden
set history=10000
set backspace=indent,eol,start
set updatetime=250


colorscheme codedark


"" keymap
nnoremap tc :tabnew<CR>
nnoremap tp :tabp<CR>
nnoremap tn :tabn<CR>
nnoremap tx :tabclose<CR>


"" plugin
call plug#begin()

"" vim-airline
Plug 'vim-airline/vim-airline'
Plug 'vim-airline/vim-airline-themes'
Plug 'tomasiser/vim-code-dark'

"" fern.vim
Plug 'lambdalisue/fern.vim'
" diff
Plug 'lambdalisue/fern-git-status.vim'
" icon
Plug 'lambdalisue/nerdfont.vim'
Plug 'lambdalisue/fern-renderer-nerdfont.vim'
Plug 'lambdalisue/glyph-palette.vim'

"" git
" diff
Plug 'airblade/vim-gitgutter'
" git command
Plug 'tpope/vim-fugitive'
" GitHub
Plug 'tpope/vim-rhubarb'

"" fzf
Plug 'junegunn/fzf', { 'do': { -> fzf#install() } }
Plug 'junegunn/fzf.vim'

call plug#end()


"" vim-airline
let g:airline_theme = 'codedark'
let g:airline_powerline_fonts = 1
let g:airline#extensions#tabline#enabled = 1
let g:airline#extensions#default#layout = [
  \ [ 'a', 'b', 'c' ],
  \ ['z']
  \ ]
let g:airline_section_c = '%t %M'
let g:airline_section_z = get(g:, 'airline_linecolumn_prefix', '').'%3l:%-2v'
let g:airline#extensions#hunks#non_zero_only = 1 "
let g:airline#extensions#tabline#fnamemod = ':t'
let g:airline#extensions#tabline#show_buffers = 1
let g:airline#extensions#tabline#show_splits = 0
let g:airline#extensions#tabline#show_tabs = 1
let g:airline#extensions#tabline#show_tab_nr = 0
let g:airline#extensions#tabline#show_tab_type = 1
let g:airline#extensions#tabline#show_close_button = 0


"" ferm.vim
nnoremap <C-n> :Fern . -reveal=% -drawer -toggle -width=40<CR>
let g:fern#renderer = 'nerdfont'
augroup my-glyph-palette
  autocmd! *
  autocmd FileType fern call glyph_palette#apply()
  autocmd FileType nerdtree,startify call glyph_palette#apply()
augroup END


"" git
nnoremap g[ :GitGutterPrevHunk<CR>
nnoremap g] :GitGutterNextHunk<CR>
nnoremap gh :GitGutterLineHighlightsToggle<CR>
nnoremap gp :GitGutterPreviewHunk<CR>
highlight GitGutterAdd ctermfg=green
highlight GitGutterChange ctermfg=blue
highlight GitGutterDelete ctermfg=red
nnoremap gb :Gbrowse<CR>
vnoremap gb :Gbrowse<CR>


"" fzf
fun! FzfOmniFiles()
  let is_git = system('git status')
  if v:shell_error
    :Files
  else
    :GitFiles
  endif
endfun
nnoremap <C-p> :call FzfOmniFiles()<CR>

" find text
command! -bang -nargs=* Rg
\ call fzf#vim#grep(
\ 'rg --column --line-number --hidden --ignore-case --no-heading --color=always '.shellescape(<q-args>), 1,
\ <bang>0 ? fzf#vim#with_preview({'options': '--delimiter : --nth 3..'}, 'up:60%')
\ : fzf#vim#with_preview({'options': '--exact --delimiter : --nth 3..'}, 'right:50%:hidden', '?'),
\ <bang>0)
nnoremap <C-g> :Rg<CR>

nnoremap fr vawy:Rg <C-R>"<CR>
xnoremap fr y:Rg <C-R>"<CR>

nnoremap fb :Buffers<CR>
nnoremap fp :Buffers<CR><CR>
nnoremap fl :BLines<CR>
nnoremap fm :Marks<CR>
nnoremap fh :History<CR>
nnoremap fc :Commits<CR>
