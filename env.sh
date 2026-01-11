#!/bin/bash
# Source this script in the calling shell:
# $ . ./env.sh
# to set, clear or show the calling shell's environment variables for Circuit CLI.

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  echo "Usage: . ./env.sh [OPTIONS] COMMAND [ARG] [local]"
  echo ""
  echo " Manage environment variables for Circuit CLI"
  echo ""
  echo "Options:"
  echo " -h, --help Show this message and exit"
  echo ""
  echo "Commands:"
  echo " clear [keepkey]             Clear environment variables (optionally keep PRIVATE_KEY)"
  echo " set main|test|sim [local]   Set environment variables"
  echo "                             To point to main/test backend running on localhost, specify 'local'"
  echo " show                        Show currently set environment variables"
  echo ""
  echo "Examples:"
  echo "  . ./env.sh set main"
  echo "  . ./env.sh set test local"
  echo "  . ./env.sh set sim"
  echo "  . ./env.sh clear keepkey"
  echo "  . ./env.sh show"
  return
fi

if [ "$1" = "set" ]; then
  ENV="$2"
  LOCAL="$3"

  if [ -z "$ENV" ] || [ "$ENV" = "-h" ] || [ "$ENV" = "--help" ]; then
    echo "Usage: . ./env.sh set <environment> [local]"
    echo ""
    echo "Environments:"
    echo " main [local]   Mainnet (remote or localhost)"
    echo " test [local]   Testnet (remote or localhost)"
    echo " sim            Simulator (always localhost)"
    echo ""
    #echo "Note: When using 'local' with main/test, the backend's network determines mainnet/testnet."
    #echo "      ADD_SIG_DATA and FEE_PER_COST must be set manually for local mode."
    echo "Note: PRIVATE_KEY must be set manually or via CLI -p option."
    return
  fi

  case "$ENV" in
    main)
        if [ "$LOCAL" = "local" ]; then
        export BASE_URL="http://localhost:8000"
        #echo "Warning: Using local backend for mainnet context. Set ADD_SIG_DATA/FEE_PER_COST manually if needed."
      else
        export BASE_URL="https://api.circuitdao.com"
      fi
      export ADD_SIG_DATA="ccd5bb71183532bff220ba46c268991a3ff07eb358e8255a65c30a2dce0e5fbb"
      export FEE_PER_COST="fast"
      ;;
    test)
      if [ "$LOCAL" = "local" ]; then
        export BASE_URL="http://localhost:8000"
        #echo "Warning: Using local backend for testnet context. Set ADD_SIG_DATA/FEE_PER_COST manually if needed."
      else
        export BASE_URL="https://testnet-api.circuitdao.com"
      fi
      export ADD_SIG_DATA="37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615"
      export FEE_PER_COST="fast"
      ;;
    sim)
      if [ -n "$LOCAL" ] && [ "$LOCAL" != "local" ]; then
        echo "Warning: 'sim' ignores extra arguments. '$LOCAL' ignored."
      fi
      export BASE_URL="http://localhost:8000"
      export ADD_SIG_DATA="ccd5bb71183532bff220ba46c268991a3ff07eb358e8255a65c30a2dce0e5fbb"
      ;;
    *)
      echo "Unknown environment '$ENV'. Use -h for help."
      return
      ;;
  esac

elif [ "$1" = "clear" ]; then
  KEEP_KEY="$2"
  if [ "$KEEP_KEY" = "-h" ] || [ "$KEEP_KEY" = "--help" ]; then
    echo "Usage: . ./env.sh clear [keepkey]"
    echo "  keepkey: Do not unset PRIVATE_KEY"
    return
  fi

  unset BASE_URL
  unset ADD_SIG_DATA
  unset FEE_PER_COST

  if [ "$KEEP_KEY" != "keepkey" ]; then
    unset PRIVATE_KEY  # Fully remove if not keeping
  fi

elif [ "$1" = "show" ] || [ -z "$1" ]; then
  unset SHOW_PRIVATE_KEY
  if [ "$2" = "-h" ]; then
    echo "Usage: . ./env.sh show [--private-key]"
    echo "  --private-key: Show private master key (PRIVATE_KEY)"
    return
  elif [ "$2" = "--private-key" ]; then
      SHOW_PRIVATE_KEY=true
  fi
  # Show current values (even if no command given)
  #:
else
  echo "Unknown command '$1'. Use -h for help."
  return
fi

# Display current settings
if [ -z "${PRIVATE_KEY:-}" ]; then
  echo "PRIVATE_KEY: <unset>"
elif [[ $SHOW_PRIVATE_KEY ]]; then
  echo "PRIVATE_KEY: ${PRIVATE_KEY}"
else
  echo "PRIVATE_KEY: ****"
fi

echo "BASE_URL: ${BASE_URL:-<unset>}"
echo "ADD_SIG_DATA: ${ADD_SIG_DATA:-<unset>}"
echo "FEE_PER_COST: ${FEE_PER_COST:-<unset>}"
