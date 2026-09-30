#!/usr/bin/env python3
"""Add GHC wasm-backend variants of GHC JavaScript-backend FFI imports.

For each `foreign import javascript` declaration in the given Haskell
files, emit

    #if defined(wasm32_HOST_ARCH)
    foreign import javascript unsafe "<wasm snippet>" js_foo ::
    #else
    foreign import javascript safe "<js snippet>" js_foo ::
    #endif
            A -> B -> IO C

duplicating only the lines up to (and including) the end of the string
literal.  The rest of the declaration is shared.

Translation rules (JS backend -> wasm JSFFI):

  * "((a,b) => BODY)"  ->  "((a,b) => BODY)($1,$2)"
    The JS backend takes a function expression; the wasm backend takes
    an expression or function body over $1..$n, so call the function.
  * "$1[\"x\"]($2)"      ->  unchanged (old GHCJS placeholder syntax is
                            already a valid wasm snippet), unless it uses
                            $r / $c / h$.
  * safe / interruptible-less / default safety -> unsafe.  On the JS
    backend `safe` is synchronous (exceptions become JSException); on
    wasm `safe` is asynchronous (Promise-returning), which changes
    semantics and cannot be used from synchronous callbacks, so keep the
    call synchronous.

Declarations that cannot be translated mechanically (unboxed types,
Exts.Any, unboxed tuples, h$ RTS functions, $c continuations, $r
results, ...) are left alone and reported on stderr as
`MANUAL <file>:<line> <name>: <reason>` so they can be ported by hand.

Files that already contain a wasm32_HOST_ARCH variant for a declaration
are skipped for that declaration, so the tool is idempotent.

Usage: wasmify.py [--dry-run] FILE...
"""

import re
import sys

DECL_RE = re.compile(r'^foreign\s+import\s+javascript\b')
ARROW_RE = re.compile(r'^\(\s*\(([^()]*)\)\s*=>')

WASM_IF = '#if defined(wasm32_HOST_ARCH)'

# --stub: replace untranslatable declarations with runtime-error stubs on
# wasm, for reasons where the declared type is still a valid lifted type.
STUB = False
STUBBABLE = {'tuple result', 'interruptible', '$c continuation', '$r result'}
STUB_QUAL_RE = re.compile(r'^import\s+qualified\s+Prelude\b', re.M)


def find_string(text, start):
    """Return (i, j) such that text[i:j] is the first Haskell string
    literal at or after start (including the quotes)."""
    i = text.index('"', start)
    j = i + 1
    while True:
        c = text[j]
        if c == '\\':
            j += 2
            continue
        if c == '"':
            return i, j + 1
        j += 1


def matching_paren(s, i):
    depth = 0
    in_str = None
    k = i
    while k < len(s):
        c = s[k]
        if in_str:
            if c == '\\':
                k += 2
                continue
            if c == in_str:
                in_str = None
        elif c in '\'"`':
            in_str = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return -1


def unescape(lit):
    """Haskell string literal body -> JS source (good enough for the
    escapes that appear in FFI snippets)."""
    out = []
    k = 0
    while k < len(lit):
        c = lit[k]
        if c == '\\' and k + 1 < len(lit):
            n = lit[k + 1]
            if n == 'n':
                out.append('\n')
            elif n == 't':
                out.append('\t')
            else:
                out.append(n)
            k += 2
        else:
            out.append(c)
            k += 1
    return ''.join(out)


def type_arity(ty):
    """Number of top-level arrows in a Haskell type."""
    depth = 0
    n = 0
    k = 0
    while k < len(ty):
        c = ty[k]
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
        elif ty.startswith('->', k) and depth == 0:
            n += 1
            k += 1
        k += 1
    return n


def strip_comments(ty):
    return re.sub(r'--.*', '', ty)


UNBOXED = {'Int#': ('Int', 'I#'), 'Word#': ('Word', 'W#'),
           'Double#': ('Double', 'D#'), 'Float#': ('Float', 'F#'),
           'Char#': ('Char', 'C#')}


def split_arrows(ty):
    """Split a type at its top-level arrows."""
    parts = []
    depth = 0
    cur = ''
    k = 0
    while k < len(ty):
        c = ty[k]
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
        if ty.startswith('->', k) and depth == 0:
            parts.append(cur.strip())
            cur = ''
            k += 2
            continue
        cur += c
        k += 1
    parts.append(cur.strip())
    return parts


def unboxed_signature(ty):
    """If the only unlifted types in ty are scalar primitives (Int#
    etc.), return (boxed_type, arg_wrappers, result_wrapper), where the
    wrappers are the boxing constructors (or None).  Otherwise None."""
    parts = split_arrows(strip_comments(ty))
    boxed = []
    wraps = []
    for p in parts:
        if p in UNBOXED:
            boxed.append(UNBOXED[p][0])
            wraps.append(UNBOXED[p][1])
        elif '#' in p:
            return None
        else:
            boxed.append(p)
            wraps.append(None)
    if not any(wraps):
        return None
    return ' -> '.join(boxed), wraps[:-1], wraps[-1]


def translate(safety, lit, ty):
    """Return (new_safety, new_lit) or raise ValueError(reason)."""
    js = unescape(lit)
    tyc = strip_comments(ty)
    if '#' in tyc:
        if unboxed_signature(ty) is None:
            raise ValueError('unboxed type')
        tyc = tyc.replace('#', '')
    if re.search(r'\bAny\b', tyc):
        raise ValueError('Exts.Any')
    if re.search(r'\bState\b', tyc):
        raise ValueError('State#')
    if re.search(r'\(\s*[^()]*,', tyc) and re.search(r'IO\s*\(', tyc):
        # IO (a, b): the JS backend returns tuples via h$ret1 etc.
        raise ValueError('tuple result')
    if safety == 'interruptible' or re.search(r'\$c\b', js):
        return translate_continuation(lit, js, tyc)
    if re.search(r'\$r\b', js):
        # Old GHCJS style: the snippet assigns its result to $r.
        if re.search(r'\bh\$', js):
            raise ValueError('h$ RTS reference')
        body = lit.strip()
        if not body.endswith(';'):
            body += ';'
        return 'unsafe', 'let $r; ' + body + ' return $r;'
    if re.search(r'\bh\$', js):
        raise ValueError('h$ RTS reference')
    if lit in ('wrapper', 'dynamic') or lit.startswith('wrapper'):
        raise ValueError('wrapper/dynamic')
    stripped = js.strip()
    m = ARROW_RE.match(stripped)
    arity = type_arity(tyc)
    if m:
        close = matching_paren(stripped, 0)
        if close != len(stripped) - 1:
            raise ValueError('arrow function does not span snippet')
        params = [p.strip() for p in m.group(1).split(',') if p.strip()]
        if len(params) > arity:
            raise ValueError('arrow has %d params, type arity %d'
                             % (len(params), arity))
        call = '(' + ','.join('$%d' % (i + 1) for i in range(len(params))) + ')'
        new_lit = lit.rstrip() + call
        return 'unsafe', new_lit
    if re.fullmatch(r'[A-Za-z_$][\w$.]*', stripped) and '$' not in stripped:
        # bare function name: call it with all arguments
        if arity == 0 or not re.search(r'->', tyc):
            # A bare global such as "Object": the same expression on wasm.
            return 'unsafe', lit
        call = '(' + ','.join('$%d' % (i + 1) for i in range(arity)) + ')'
        return 'unsafe', lit + call
    # Old-style placeholder snippet, e.g. "$1[\"x\"]($2)".
    return 'unsafe', lit


def continuation_arities(js):
    """Argument counts of every $c(...) call in a snippet."""
    arities = set()
    for mm in re.finditer(r'\$c\s*\(', js):
        close = matching_paren(js, mm.end() - 1)
        args = js[mm.end():close].strip()
        if not args:
            arities.add(0)
        else:
            depth = 0
            n = 1
            for ch in args:
                if ch in '([{':
                    depth += 1
                elif ch in ')]}':
                    depth -= 1
                elif ch == ',' and depth == 0:
                    n += 1
            arities.add(n)
    # $c passed as a value (e.g. as a success callback) counts as 1.
    if re.search(r'\$c\s*[,)]', js):
        arities.add(1)
    return arities


def translate_continuation(lit, js, tyc):
    """JS-backend interruptible imports resume Haskell by calling $c.
    When every $c call passes exactly one value, that is exactly a
    Promise resolve function, so wrap the snippet in a Promise and make
    it an asynchronous wasm import.  Multi-value continuations return
    unboxed tuples / IO tuples on the JS side and are left for porting
    by hand."""
    ar = continuation_arities(js)
    if ar != {1}:
        raise ValueError('interruptible')
    stripped = js.strip()
    m = ARROW_RE.match(stripped)
    if m:
        close = matching_paren(stripped, 0)
        if close != len(stripped) - 1:
            raise ValueError('arrow function does not span snippet')
        params = [p.strip() for p in m.group(1).split(',') if p.strip()]
        args = []
        k = 0
        for p in params:
            if p == '$c':
                args.append('$c')
            else:
                k += 1
                args.append('$%d' % k)
        body = lit.rstrip() + '(' + ','.join(args) + ');'
    else:
        body = lit.rstrip()
        if not body.endswith(';'):
            body += ';'
    return 'safe', 'return new Promise(($c) => { ' + body + ' });'


def process(path, dry_run=False):
    with open(path) as f:
        lines = f.read().split('\n')
    out = []
    changed = False
    i = 0
    manual = []
    stubbed = []
    # CPP nesting, to skip code that only old GHCJS (__GHCJS__) compiles:
    # neither the JavaScript nor the wasm backend defines __GHCJS__.
    stack = []   # one bool per open #if: True while in a __GHCJS__-only branch
    while i < len(lines):
        line = lines[i]
        cm = re.match(r'^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)', line)
        if cm:
            kw, rest = cm.group(1), cm.group(2).strip()
            ghcjs_only = bool(re.fullmatch(r'(__GHCJS__|defined\s*\(\s*__GHCJS__\s*\)|defined\s+__GHCJS__)', rest))
            if kw == 'if':
                stack.append(ghcjs_only)
            elif kw == 'ifdef':
                stack.append(rest == '__GHCJS__')
            elif kw == 'ifndef':
                stack.append(False)
            elif kw == 'elif' and stack:
                stack[-1] = ghcjs_only
            elif kw == 'else' and stack:
                stack[-1] = False
            elif kw == 'endif' and stack:
                stack.pop()
        if not DECL_RE.match(line) or any(stack):
            out.append(line)
            i += 1
            continue
        # Already translated?  Look back for a wasm #if immediately above.
        if out and out[-1].strip() in ('#else',) or \
           (out and out[-1].strip() == WASM_IF):
            out.append(line)
            i += 1
            continue
        # Collect the declaration: this line plus following indented lines.
        j = i + 1
        while j < len(lines) and lines[j].strip() != '' and \
                lines[j][0] in ' \t':
            j += 1
        decl = lines[i:j]
        text = '\n'.join(decl)
        m = re.match(r'foreign\s+import\s+javascript\s+(unsafe|safe|interruptible)?',
                     text)
        safety = m.group(1) or 'safe'
        try:
            s0, s1 = find_string(text, m.end())
        except (ValueError, IndexError):
            out.extend(decl)
            i = j
            continue
        lit = text[s0 + 1:s1 - 1]
        after = text[s1:]
        # name and type
        nm = re.match(r'\s*([A-Za-z_][\w\']*)\s*::(.*)', after, re.S)
        if not nm:
            manual.append((i + 1, '?', 'cannot parse name/type'))
            out.extend(decl)
            i = j
            continue
        name, ty = nm.group(1), nm.group(2)
        try:
            new_safety, new_lit = translate(safety, lit, ty)
        except ValueError as e:
            reason = str(e)
            if STUB and reason in STUBBABLE and STUB_QUAL_RE.search('\n'.join(lines)):
                # Same name and type, but calling it on wasm is a runtime
                # error until someone ports it by hand.
                out.append(WASM_IF)
                out.append('%s ::%s' % (name, ty.rstrip()))
                out.append('%s = Prelude.error "%s: %s is not yet ported to the wasm backend (%s)"'
                           % (name, path.split('/src/')[-1], name, reason))
                out.append('#else')
                out.extend(decl)
                out.append('#endif')
                changed = True
                stubbed.append((i + 1, name, reason))
            else:
                manual.append((i + 1, name, reason))
                out.extend(decl)
            i = j
            continue
        ub = unboxed_signature(ty)
        if ub is None and new_lit == lit and new_safety == safety:
            # Already a valid wasm import as written.
            out.extend(decl)
            i = j
            continue
        if ub is not None:
            # wasm JSFFI cannot marshal unboxed types: import a boxed
            # version and give the original name/type to a wrapper.
            boxed_ty, arg_wraps, res_wrap = ub
            wname = name + '_wasm'
            args = ['a%d' % (k + 1) for k in range(len(arg_wraps))]
            call = ' '.join([wname] + ['(%s %s)' % (w, a) if w else a
                                       for w, a in zip(arg_wraps, args)])
            if res_wrap:
                rhs = 'case %s of %s r -> r' % (call, res_wrap)
            else:
                rhs = call
            out.append(WASM_IF)
            out.append('foreign import javascript %s "%s"' % (new_safety, new_lit))
            out.append('  %s :: %s' % (wname, boxed_ty))
            out.append('%s ::%s' % (name, ty.rstrip()))
            out.append('%s %s = %s' % (name, ' '.join(args), rhs))
            out.append('{-# INLINE %s #-}' % name)
            out.append('#else')
            out.extend(decl)
            out.append('#endif')
            changed = True
            i = j
            continue
        # Number of declaration lines containing the head + string.
        head_end_line = text[:s1].count('\n')
        head = decl[:head_end_line + 1]
        rest = decl[head_end_line + 1:]
        head_text = '\n'.join(head)
        # Build wasm head: replace safety and literal.
        prefix = text[:s0]
        prefix = re.sub(r'(foreign\s+import\s+javascript)\s+(unsafe|safe|interruptible)?\s*',
                        r'\1 %s ' % new_safety, prefix, count=1)
        wasm_head_text = prefix + '"' + new_lit + '"' + head_text[s1:]
        out.append(WASM_IF)
        out.extend(wasm_head_text.split('\n'))
        out.append('#else')
        out.extend(head)
        out.append('#endif')
        out.extend(rest)
        changed = True
        i = j
    for (ln, name, reason) in manual:
        print('MANUAL %s:%d %s: %s' % (path, ln, name, reason), file=sys.stderr)
    for (ln, name, reason) in stubbed:
        print('STUB %s:%d %s: %s' % (path, ln, name, reason), file=sys.stderr)
    if changed:
        text = '\n'.join(out)
        if not re.search(r'LANGUAGE[^#]*\bCPP\b', text):
            text = '{-# LANGUAGE CPP #-}\n' + text
        if not dry_run:
            with open(path, 'w') as f:
                f.write(text)
    return changed, len(manual)


def main(argv):
    global STUB
    dry = False
    files = []
    for a in argv:
        if a == '--dry-run':
            dry = True
        elif a == '--stub':
            STUB = True
        else:
            files.append(a)
    nchanged = 0
    nmanual = 0
    for p in files:
        c, m = process(p, dry)
        nchanged += c
        nmanual += m
    print('wasmify: %d files changed, %d declarations need manual porting'
          % (nchanged, nmanual), file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv[1:])
