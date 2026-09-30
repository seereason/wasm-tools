#!/usr/bin/env python3
"""Make JavaScript-backend CPP conditionals also select the wasm backend.

In these code bases `javascript_HOST_ARCH` / `ghcjs_HOST_OS` (the macros
the GHC JavaScript backend defines) mean "this is the browser client
build".  The wasm build is also a browser client build, so rewrite

    defined(javascript_HOST_ARCH)  ->  (defined(javascript_HOST_ARCH) || defined(wasm32_HOST_ARCH))
    defined(ghcjs_HOST_OS)         ->  (defined(ghcjs_HOST_OS) || defined(wasm32_HOST_ARCH))
    #ifdef  javascript_HOST_ARCH   ->  #if defined(javascript_HOST_ARCH) || defined(wasm32_HOST_ARCH)
    #ifndef javascript_HOST_ARCH   ->  #if !(defined(javascript_HOST_ARCH) || defined(wasm32_HOST_ARCH))

(and likewise for ghcjs_HOST_OS) in #if/#elif/#ifdef/#ifndef lines.
Where the JavaScript and wasm code must differ (foreign imports), the
code inside such a block then uses `#if defined(wasm32_HOST_ARCH)`.

`__GHCJS__` (old GHCJS) is left alone: neither backend defines it.

Idempotent.  Usage: cpp-client.py FILE...
"""

import re
import sys

MACROS = ['javascript_HOST_ARCH', 'ghcjs_HOST_OS']


def rewrite(line):
    s = line
    m = re.match(r'^(\s*#\s*)(ifdef|ifndef)\s+(\w+)\s*$', s)
    if m and m.group(3) in MACROS:
        mac = m.group(3)
        cond = 'defined(%s) || defined(wasm32_HOST_ARCH)' % mac
        if m.group(2) == 'ifdef':
            return '%sif %s\n' % (m.group(1), cond)
        return '%sif !(%s)\n' % (m.group(1), cond)
    if not re.match(r'^\s*#\s*(if|elif)\b', s):
        return s
    if 'wasm32_HOST_ARCH' in s:
        return s  # already done
    for mac in MACROS:
        s = re.sub(r'defined\s*\(\s*%s\s*\)' % mac,
                   '(defined(%s) || defined(wasm32_HOST_ARCH))' % mac, s)
        s = re.sub(r'defined\s+%s\b' % mac,
                   '(defined(%s) || defined(wasm32_HOST_ARCH))' % mac, s)
    return s


def main(files):
    n = 0
    for p in files:
        with open(p) as f:
            lines = f.readlines()
        new = [rewrite(l) for l in lines]
        if new != lines:
            with open(p, 'w') as f:
                f.writelines(new)
            n += 1
    print('cpp-client: %d files changed' % n, file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv[1:])
