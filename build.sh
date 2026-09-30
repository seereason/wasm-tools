#!/usr/bin/env bash
# Build sr-hix outputs against the local ghc9-wasm checkouts in
# /home/beshers/git.wasm, including their uncommitted changes (git+file:
# without a ref uses the working tree of tracked files -- `git add` new
# files first).
#
#   wasm-tools/build.sh .#ghcjs-base-wasm [more nix build args...]
set -euo pipefail
W=/home/beshers/git.wasm
cd "$W/sr-flake/sr-hix"
o() { echo --override-input "$1" "git+file://$W/$2"; }
exec nix ${NIXCMD:-build} --accept-flake-config ${NIXCMD:+} $([ "${NIXCMD:-build}" = build ] && echo -L --keep-going) \
  $(o alderon2                   alderon2) \
  $(o chili                      chili) \
  $(o clckwrks-plugin-agreements clckwrks-plugin-agreements) \
  $(o clckwrks-plugin-mailchimp  clckwrks-plugin-mailchimp) \
  $(o clckwrks-plugin-microblog  clckwrks-plugin-microblog) \
  $(o editor-client              editor-client-ghc912) \
  $(o editor-client-six          editor-client) \
  $(o ghcjs-base                 ghcjs-base) \
  $(o ghcjs-dom                  ghcjs-dom) \
  $(o happstack-authenticate     happstack-authenticate) \
  $(o happstack-ghcjs            happstack-ghcjs) \
  $(o appraisalscribe-tests      "happstack-ghcjs?dir=appraisalscribe-tests") \
  $(o appraisalscribe-types      "happstack-ghcjs?dir=appraisalscribe-types") \
  $(o happstack-ghcjs-server     "happstack-ghcjs?dir=happstack-ghcjs-server") \
  $(o seereason                  seereason) \
  $(o sr-dom                     sr-dom) \
  $(o webmodule                  webmodule) \
  $(o extra                      extra) \
  $(o formatting                 formatting) \
  $(o history                    history) \
  $(o hslogger                   hslogger) \
  $(o image-cache                image-cache) \
  $(o lens-path                  lens-path) \
  $(o sr-log                     sr-log) \
  $(o sr-order                   sr-order) \
  $(o sr-utils                   sr-utils) \
  "$@"
