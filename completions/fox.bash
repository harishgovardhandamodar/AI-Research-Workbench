# bash completion for the `fox` CLI.
# Source from your .bashrc:  source /path/to/repo/completions/fox.bash
# Optionally wire --url / FOX_URL so completions hit a live server.

_fox_subcommands="splash version status doctor serve graph papers projects runs run experiments experiment compare research manage jobs scheduler pool manual"

_fox_actions() {
  case "$1" in
    projects)   echo "list new show rm fork" ;;
    run)        echo "show report" ;;
    experiments) echo "list start run-obfuscation" ;;
    experiment) echo "show ranking" ;;
    research)   echo "list status report build synthesize experiments loop" ;;
    manage)     echo "repos status link commit push commit-and-push" ;;
    papers)     echo "list search add" ;;
    pool)       echo "list topics topics-add topics-rm import" ;;
    *)          echo "" ;;
  esac
}

_fox() {
  local cur prev words cword
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  local cmd=""
  local i
  for ((i = 1; i < COMP_CWORD; i++)); do
    case "${COMP_WORDS[i]}" in
      -*|--*) continue ;;
      *) cmd="${COMP_WORDS[i]}"; break ;;
    esac
  done

  local flags="--json --quiet --debug --url --help --version"

  # after a flag that takes a value, complete next token freely
  case "$prev" in
    --url|--name|-n|--hypothesis|--goal-metric|--goal-target|--plan|--metric|--message|-m|--n-rows|--seed|-d|--description)
      COMPREPLY=()
      return 0 ;;
  esac

  if [[ "$cur" == -* ]]; then
    COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
    return 0
  fi

  local actions
  if [[ -n "$cmd" ]]; then
    actions="$(_fox_actions "$cmd") $flags"
  else
    actions="$_fox_subcommands help exit"
  fi
  COMPREPLY=( $(compgen -W "$actions" -- "$cur") )
  return 0
}

complete -F _fox fox
