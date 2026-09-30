# Porting GHC JavaScript-backend FFI code to the GHC wasm backend

Target: GHC 9.14.1 wasm backend (wasm32-wasi), JSFFI.  CPP symbol for the
wasm build: `wasm32_HOST_ARCH`.  The JavaScript build (`javascript_HOST_ARCH`)
must keep compiling to exactly what it compiles to today.

## The pattern

    #if defined(wasm32_HOST_ARCH)
    <wasm version>
    #else
    <original JavaScript-backend code, byte-for-byte unchanged>
    #endif

Add `{-# LANGUAGE CPP #-}` if the module lacks it.  Keep export lists and
Haskell-level types identical on both backends wherever possible.  If
something truly cannot exist on wasm (e.g. it exposes `ByteArray#`
representation of JSVal), exclude it from the wasm export list with `#if`
and leave a one-line comment saying why.

## wasm JSFFI rules (from the GHC 9.14 user guide)

* `foreign import javascript unsafe "<snippet>" f :: T`
  The snippet is a JavaScript *expression* or a *function body* (use
  `return`), over the arguments `$1`, `$2`, ...  (It is compiled as
  `($1,$2,...) => (snippet)` or `($1,$2,...) => {snippet}`.)
  A JS-backend snippet that is an arrow function `((x,y) => ...)` must be
  *called*: `((x,y) => ...)($1,$2)`, or rewritten as a body.
* `unsafe` = synchronous.  Use `unsafe` for everything that is
  synchronous JavaScript -- including JS-backend `safe` imports (on the JS
  backend `safe` is still synchronous; on wasm `safe` means asynchronous).
* `safe` = asynchronous: the snippet runs inside an async function, may
  `await`, and may return a Promise; the Haskell result is a thunk that
  blocks the evaluating Haskell thread until the Promise settles.  Use it
  only for genuinely asynchronous things (what the JS backend did with
  `interruptible` and `$c`).  Note even `IO ()` results must be forced.
* Marshalable argument/result types: `Bool` (passed to JS as 0/1),
  `Char` (code point), `Int`/`Word` (32-bit), `Int8..Int64`,
  `Word8..Word64` (64-bit ones are BigInt!), `Float`, `Double`, `Ptr`,
  `FunPtr`, `StablePtr`, `JSVal`, and *newtypes* of these whose
  constructors are in scope, plus `()` results.
  NOT allowed: unboxed types (`Int#`, `ByteArray#`, `State#`, ...),
  unboxed tuples, `Exts.Any`, tuples, lists.
* `foreign import javascript "wrapper"` / `"wrapper sync"` turn a Haskell
  function into a JS function (`IO JSVal`); `"dynamic"` goes the other way.
* In snippets `__exports.memory` is the wasm memory (a `WebAssembly.Memory`);
  a `Ptr` argument is a byte offset into `__exports.memory.buffer`.
* There is no `h$...` runtime and no `js-sources` on wasm.  JS helper code
  from `jsbits/*.js` must be inlined into the snippets (or reimplemented in
  Haskell).  `h$ret1`, `h$ret2`... (multiple return values) do not exist:
  return a JS array and pick it apart with extra imports, or split into
  several imports.
* Haskell lists cannot be built in JavaScript.  Where the JS backend
  returned `Exts.Any` (a Haskell list built with `h$toHsList` etc.), on wasm
  return a JS array (`JSVal`) and convert with `GHC.JS.Prim.fromJSArray`
  (or an index loop).  Going the other way, build a JS array with
  `GHC.JS.Prim.toJSArray`.
* Unboxed wrappers: the tool `wasm-tools/wasmify.py` already handled
  simple cases by importing `foo_wasm :: Int -> JSString -> JSString` and
  defining `foo :: Int# -> JSString -> JSString` as a wrapper using `I#`.
  Do the same by hand for the remaining ones so the calling code does not
  have to change.  Unboxed-tuple results can be produced in the wrapper,
  e.g. `case js_foo_wasm x of r -> (# unI (js_fst r), js_snd r #)`.

## What exists on wasm

`JSVal` is `GHC.Wasm.Prim.JSVal` (from ghc-experimental), an opaque boxed
type with no accessible constructor.  In ghcjs-base, on wasm only,
`wasm/GHC/JS/Prim.hs` and `wasm/GHC/JS/Foreign/Callback.hs` provide
stand-ins for the JS backend's `GHC.JS.Prim` / `GHC.JS.Foreign.Callback`:

    GHC.JS.Prim: JSVal, JSException(..), WouldBlockException(..),
      mkJSException, fromJSString :: JSVal -> String,
      toJSString :: String -> JSVal, toJSArray :: [JSVal] -> IO JSVal,
      fromJSArray :: JSVal -> IO [JSVal], fromJSInt, toJSInt,
      isNull, isUndefined, jsNull, getProp, getProp', unsafeGetProp,
      unsafeGetProp', seqList
    GHC.JS.Foreign.Callback: Callback(..) (newtype over JSVal),
      OnBlocked(..), releaseCallback, syncCallback{,1,2,3}{,'},
      asyncCallback{,1,2,3}

You may add more functions to `wasm/GHC/JS/Prim.hs` if several modules
need them (keep them general).  `GHC.Wasm.Prim` itself exports
`JSVal, freeJSVal, mkWeakJSVal, JSString(..), fromJSString, toJSString,
JSException(..), WouldBlockException(..)`; ghcjs-base's own `JSString`
(Data.JSString.Internal.Type) is a newtype over `JSVal` and is what
ghcjs-base code uses.

JavaScript-side values: JS strings are JS strings (same as the JS
backend); numbers are numbers; `null`/`undefined` as usual.

## Lessons from building (2026-09-30)

* **Newtype constructors must be in scope.**  A foreign import argument or
  result of a newtype (JSString, Callback, EventTarget, SomeArrayBuffer,
  Export, ...) marshals only if the newtype's data constructor is in scope
  in that module; otherwise the C stub mentions an unknown `HsFoo` type.
  `wasm-tools/ctor-imports.pl [--dry-run] DIR...` adds the missing
  `#if defined(wasm32_HOST_ARCH) import M (T(..)) #endif` imports.  It
  only imports from *exposed* modules; ghcjs-base exports these
  constructors on wasm only: `GHCJS.Types (JSString(..))`,
  `JavaScript.TypedArray.ArrayBuffer (SomeArrayBuffer(..))`,
  `GHCJS.Foreign.Export (Export(..))`.
* **CPP and strings.**  With CPP on, a Haskell string gap (`\` newline
  `\`) is broken by CPP's backslash-newline splicing; write the snippet
  on one line, or use CPP continuation (`\` at end of line, no leading
  `\` on the next).  `\(` is not a valid escape.
* **Syntax-check the JS.**  All snippets of a program end up in one JS
  module; one syntax error stops the whole client from loading.
  `nix shell nixpkgs#nodejs --command perl wasm-tools/check-snippets.pl DIR...`
  parses every snippet live on wasm the way the wasm backend wraps it.
* **Non-FFI repos too.**  Any package whose CPP (`javascript_HOST_ARCH`,
  `ghcjs_HOST_OS`) or cabal (`arch(javascript)`) conditions mean
  "browser client" needs `cpp-client.py` / `cabal-client.py` and a
  ghc9-wasm branch, even without JS FFI.
* **C sources are compiled on wasm** (the JS backend ignored them):
  guard POSIX-only calls (e.g. `ioctl(TIOCGWINSZ)`).
* **network** needs `sr-hix/patches/network-wasi.patch` (wasi-libc has
  no netdb.h); name lookups fail at run time.
* **Builds:** run big builds with `--cores 16 --max-jobs 4` (or less);
  uncapped they OOM the machine.
