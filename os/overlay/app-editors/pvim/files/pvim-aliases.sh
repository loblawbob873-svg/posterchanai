# The names people type out of habit open pvim -- but only where that program is NOT installed:
# somebody who installs vim, neovim or nano gets the real one.
for _pc_ed in vi vim nvim nano; do
	command -v "$_pc_ed" >/dev/null 2>&1 || alias "$_pc_ed"=pvim
done
unset _pc_ed
