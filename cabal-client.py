#!/usr/bin/env python3
"""Make `arch(javascript)` cabal conditionals also select the wasm backend.

In these packages `arch(javascript)` means "browser client build"; the
wasm build is one too.  In `if`/`elif` lines, rewrite

    arch(javascript)  ->  (arch(javascript) || arch(wasm32))

Blocks that carry JavaScript-backend-only fields (`js-sources`) get those
fields moved under a nested `if arch(javascript)`.

`flag(arch_javascript)` (the haskell.nix stand-in) is left alone: the
wasm build sets it too.  Idempotent.  Usage: cabal-client.py FILE...
"""

import re
import sys

JS_ONLY_FIELDS = re.compile(r'^(\s*)(js-sources)\s*:', re.I)


def indent_of(l):
    return len(l) - len(l.lstrip(' '))


def process(p):
    with open(p) as f:
        lines = f.read().split('\n')
    out = []
    changed = False
    k = 0
    while k < len(lines):
        l = lines[k]
        m = re.match(r'^(\s*)(if|elif)\s+(.*)$', l)
        if m and 'arch(javascript)' in m.group(3) and 'arch(wasm32)' not in m.group(3):
            cond = re.sub(r'arch\s*\(\s*javascript\s*\)', '(arch(javascript) || arch(wasm32))', m.group(3))
            out.append('%s%s %s' % (m.group(1), m.group(2), cond))
            changed = True
            k += 1
            continue
        # A js-sources field (with its continuation lines) that would now
        # also apply to wasm: nest it under `if arch(javascript)` unless
        # it is already directly under such a condition.
        fm = JS_ONLY_FIELDS.match(l)
        if fm:
            ind = indent_of(l)
            # find the enclosing condition, if any
            enclosing = None
            for prev in reversed(out):
                if prev.strip() == '' or prev.strip().startswith('--'):
                    continue
                if indent_of(prev) < ind:
                    enclosing = prev.strip()
                    break
            if enclosing is not None and re.fullmatch(r'if\s+arch\(javascript\)', enclosing):
                out.append(l)
                k += 1
                continue
            block = [l]
            k += 1
            while k < len(lines) and lines[k].strip() != '' and indent_of(lines[k]) > ind \
                    and ':' not in lines[k].split('--')[0]:
                block.append(lines[k])
                k += 1
            out.append(' ' * ind + 'if arch(javascript)')
            out.extend('  ' + b for b in block)
            changed = True
            continue
        out.append(l)
        k += 1
    if changed:
        with open(p, 'w') as f:
            f.write('\n'.join(out))
    return changed


if __name__ == '__main__':
    n = sum(process(p) for p in sys.argv[1:])
    print('cabal-client: %d files changed' % n, file=sys.stderr)
