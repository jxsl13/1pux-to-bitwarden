#!/bin/zsh
set -eu
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "${0:A:h}"
trap 'unset BW_SESSION; printf "\nPress Enter to close. "; read -r answer' EXIT
if ! command -v bw >/dev/null || ! command -v python3 >/dev/null; then
  print 'Python 3 and the Bitwarden CLI must be installed. See README.md.'
  exit 1
fi
state="$(bw status | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
if [[ "$state" == 'unauthenticated' ]]; then
  print 'Sign in to the correct Bitwarden server in Terminal first.'
  print 'See README.md for server selection and sign-in instructions.'
  exit 1
fi
if [[ "$state" != 'unlocked' ]]; then
  export BW_SESSION="$(bw unlock --raw)"
  [[ -n "$BW_SESSION" ]] || exit 1
fi
source_file="$(osascript -e 'POSIX path of (choose file with prompt "Choose your existing 1Password export (.1pux)")')"
print 'v = Preview the full migration (default)'
print 'i = Import entries into My vault, then upload attachments'
print 'a = Only add attachments to existing entries'
read -r 'mode?Choose a mode: '
args=()
if [[ "$mode" == 'i' ]]; then
  args+=(--import-items --apply)
elif [[ "$mode" == 'a' ]]; then
  args+=(--apply)
elif [[ -z "$mode" || "$mode" == 'v' ]]; then
  args+=(--import-items)
else
  print 'Invalid selection.'
  exit 1
fi
mapping_file='mapping.json'
# Preserve manually reviewed mappings from the initial German-language version.
if [[ ! -f "$mapping_file" && -f zuordnung.json ]]; then
  mapping_file='zuordnung.json'
fi
if [[ "$mode" == 'a' && -f "$mapping_file" ]]; then
  print "Using manual mappings from $mapping_file."
  args+=(--mapping "$PWD/$mapping_file")
fi
result=0
python3 ./import_attachments.py "$source_file" "${args[@]}" || result=$?
review='work/preview.html'
[[ "$mode" != 'i' && "$mode" != 'a' ]] || review='work/import-report.html'
if [[ "$result" == 0 || "$result" == 2 ]] && [[ -f "$review" ]]; then
  open "$review"
fi
exit "$result"
