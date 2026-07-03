#!/usr/bin/env bash
# Sourced helper (not an entry point): load ./.env from the repo root into the environment — the SAME
# file docker compose reads, with the SAME precedence: variables already set in the shell WIN over
# .env values. Empty values are skipped (they'd just re-trigger the defaults anyway). Lines are plain
# KEY=VALUE; comments (#) and blanks ignored; one layer of surrounding quotes stripped.
_dotenv="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.env"
if [ -f "$_dotenv" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in ''|\#*) continue ;; esac
    _key="${_line%%=*}"; _val="${_line#*=}"
    case "$_key" in *[!A-Za-z0-9_]*|'') continue ;; esac
    case "$_val" in
      \"*\") _val="${_val#\"}"; _val="${_val%\"}" ;;
      \'*\') _val="${_val#\'}"; _val="${_val%\'}" ;;
    esac
    [ -n "$_val" ] || continue
    if eval "[ -z \"\${${_key}+x}\" ]"; then export "${_key}=${_val}"; fi
  done < "$_dotenv"
  echo "  · .env loaded (shell-set variables take precedence)"
fi
unset _dotenv _line _key _val 2>/dev/null || true
