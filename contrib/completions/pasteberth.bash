# Bash completion for Pasteberth.
#
# Install for the current user:
#   source contrib/completions/pasteberth.bash
#
# Install permanently with bash-completion:
#   install -Dm644 contrib/completions/pasteberth.bash \
#     "${BASH_COMPLETION_USER_DIR:-$HOME/.local/share/bash-completion}/completions/pasteberth"

_pasteberth_complete_path() {
    if declare -F _filedir >/dev/null 2>&1; then
        _filedir
    else
        mapfile -t COMPREPLY < <(compgen -f -- "$cur")
    fi
}

_pasteberth_complete() {
    local cur prev command command_index i
    local -a commands global_options options values

    cur=${COMP_WORDS[COMP_CWORD]}
    prev=""
    if (( COMP_CWORD > 0 )); then
        prev=${COMP_WORDS[COMP_CWORD - 1]}
    fi

    commands=(
        serve
        filesystem-drop
        filesystem-rename
        filesystem-delete
        passwd
        audit
    )
    global_options=(-h --help --version --config --generate-config --force)

    if [[ $prev == --config ]]; then
        _pasteberth_complete_path
        return
    fi

    command=""
    command_index=-1
    for ((i = 1; i < COMP_CWORD; i++)); do
        case ${COMP_WORDS[i]} in
            --config)
                ((i++))
                ;;
            --generate-config|--force|--version)
                ;;
            serve|filesystem-drop|filesystem-rename|filesystem-delete|passwd|audit)
                command=${COMP_WORDS[i]}
                command_index=$i
                break
                ;;
        esac
    done

    if [[ -z $command ]]; then
        COMPREPLY=( $(compgen -W "${global_options[*]} ${commands[*]}" -- "$cur") )
        return
    fi

    case $command in
        serve)
            options=(-h --help --config --log-level)
            values=(DEBUG INFO WARNING ERROR)
            ;;
        filesystem-drop)
            options=(-h --help --config --replace)
            values=()
            ;;
        filesystem-rename)
            options=(-h --help --config)
            values=()
            ;;
        filesystem-delete)
            options=(-h --help --config --force)
            values=()
            ;;
        passwd|audit)
            options=(-h --help --config)
            values=()
            ;;
        *)
            COMPREPLY=()
            return
            ;;
    esac

    if [[ $prev == --log-level ]]; then
        COMPREPLY=( $(compgen -W "${values[*]}" -- "$cur") )
        return
    fi

    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "${options[*]}" -- "$cur") )
        return
    fi

    # Directory, source-file, and managed-name arguments all benefit from the
    # shell's ordinary filesystem completion. The command validates the final
    # path/name against the configured zone and its safety rules.
    if (( command_index >= 0 )); then
        _pasteberth_complete_path
    fi
}

complete -o bashdefault -o default -F _pasteberth_complete pasteberth
