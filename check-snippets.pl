#!/usr/bin/env perl
# check-snippets.pl DIR...
#
# Syntax-check every foreign import javascript snippet that is live on
# wasm.  The wasm backend compiles a snippet as an arrow function over
# $1..$n, with the snippet as its expression or, failing that, its body;
# a snippet that is neither is a syntax error in the generated JS glue,
# which stops the whole module from loading.  The glue is an ES module,
# so snippets are checked in strict mode.  Needs node on PATH.
use strict; use warnings;
use File::Find;

my @files;
find(sub { push @files, $File::Find::name
             if /\.hs$/ && $File::Find::name !~ m{/dist|/ZZZ/} }, @ARGV);

my @out;
for my $file (sort @files) {
  open my $fh, '<', $file or next; local $/; my $src = <$fh>; close $fh;
  next unless $src =~ /wasm32_HOST_ARCH/;
  $src = strip_block_comments($src);
  $src =~ s/\\\n//g;                         # CPP line continuation
  my @L = split /\n/, $src;
  my @stack;
  for my $i (0..$#L) {
    my $l = $L[$i];
    if ($l =~ /^#\s*if(n?def)?\b(.*)/) {
      my $c = $2; my $v;
      if ($c =~ /^\s*defined\s*\(\s*wasm32_HOST_ARCH\s*\)\s*$/) { $v = 1 }
      elsif ($c =~ /^\s*!\s*defined\s*\(\s*wasm32_HOST_ARCH\s*\)\s*$/) { $v = 0 }
      elsif ($c =~ /__GHCJS__/ && $c !~ /!\s*(defined\s*\(\s*)?__GHCJS__/ && $c !~ /\|\|/) { $v = 0 }
      push @stack, [$v];
      next;
    }
    if ($l =~ /^#\s*elif/) { $stack[-1] = [undef] if @stack; next }
    if ($l =~ /^#\s*else/) { if (@stack) { my ($v) = @{$stack[-1]}; $stack[-1] = [defined $v ? 1 - $v : undef] } next }
    if ($l =~ /^#\s*endif/) { pop @stack; next }
    next if grep { defined $_->[0] && $_->[0] == 0 } @stack;
    next unless $l =~ /^foreign\s+import\s+javascript\b/;
    # The literal may start on this line or the next few (string gaps).
    my $text = join "\n", @L[$i .. ($i + 60 > $#L ? $#L : $i + 60)];
    next unless $text =~ /^foreign\s+import\s+javascript\s+(?:unsafe|safe|interruptible)?\s*"((?:[^"\\]|\\.)*)"/s;
    my $lit = $1;
    next if $lit =~ /^(?:wrapper|dynamic)(?: sync)?$/;
    $lit =~ s/\\\s+\\//g;                    # Haskell string gap
    $lit =~ s/\\&//g;
    $lit =~ s/\\(["\\'])/$1/g;
    $lit =~ s/\\n/\n/g; $lit =~ s/\\t/\t/g;
    push @out, [ "$file:" . ($i + 1), $lit ];
  }
}

my $js = <<'EOF';
const items = JSON.parse(require('fs').readFileSync(0, 'utf8'));
// Optional: with ACORN_DIR=<dir containing node_modules/acorn{,-walk}>,
// also report assignments to names the snippet never declares.  In strict
// mode those throw a ReferenceError at run time unless a global of that
// name already exists.
let acorn = null, walk = null;
if (process.env.ACORN_DIR) {
  acorn = require(process.env.ACORN_DIR + '/node_modules/acorn');
  walk = require(process.env.ACORN_DIR + '/node_modules/acorn-walk');
}
function patNames(p, out) {
  if (!p) return;
  switch (p.type) {
    case 'Identifier': out.add(p.name); break;
    case 'ObjectPattern': p.properties.forEach(q => patNames(q.type === 'RestElement' ? q.argument : q.value, out)); break;
    case 'ArrayPattern': p.elements.forEach(q => patNames(q, out)); break;
    case 'AssignmentPattern': patNames(p.left, out); break;
    case 'RestElement': patNames(p.argument, out); break;
  }
}
function undeclaredAssignments(body, args) {
  const ast = acorn.parse('(function(' + args.join(',') + '){' + body + '\n})', {ecmaVersion: 'latest'});
  const declared = new Set(args);
  walk.full(ast, n => {
    if (n.type === 'VariableDeclarator') patNames(n.id, declared);
    if (/Function/.test(n.type)) { if (n.id) declared.add(n.id.name); n.params.forEach(p => patNames(p, declared)); }
    if (n.type === 'CatchClause') patNames(n.param, declared);
    if (n.type === 'ClassDeclaration' && n.id) declared.add(n.id.name);
  });
  const bad = new Set();
  walk.full(ast, n => {
    if (n.type === 'AssignmentExpression' && n.left.type === 'Identifier' && !declared.has(n.left.name)) bad.add(n.left.name);
    if (n.type === 'UpdateExpression' && n.argument.type === 'Identifier' && !declared.has(n.argument.name)) bad.add(n.argument.name);
  });
  return [...bad];
}
let bad = 0, globals = 0;
for (const [loc, s] of items) {
  let n = 0; for (const m of s.matchAll(/\$(\d+)/g)) n = Math.max(n, +m[1]);
  const args = Array.from({length: n}, (_, i) => '$' + (i + 1));
  let ok = null, err;
  for (const body of ["'use strict'; return (" + s + '\n);', "'use strict'; " + s]) {
    try { new Function(...args, body); ok = body; break; } catch (e) { err = e; }
  }
  if (!ok) { bad++; console.log(loc + ': ' + err.message + '\n    ' + s.replace(/\s+/g, ' ').slice(0, 200)); continue; }
  // JavaScript-backend RTS / js-sources helpers do not exist on wasm.
  const h = s.replace(/(["'`])(?:\\.|(?!\1)[^\\])*\1/g, "''").match(/(?<![\w$])h\$\w+/g);
  if (h) { bad++; console.log(loc + ': uses ' + [...new Set(h)].join(', ') + ' (not on wasm)\n    ' + s.replace(/\s+/g, ' ').slice(0, 200)); continue; }
  if (acorn) {
    const u = undeclaredAssignments(ok, args);
    if (u.length) { globals++; console.log(loc + ': assigns undeclared ' + u.join(', ') + '\n    ' + s.replace(/\s+/g, ' ').slice(0, 200)); }
  }
}
console.log(items.length + ' snippets, ' + bad + ' with syntax errors' +
            (acorn ? ', ' + globals + ' assigning undeclared names' : ''));
EOF
open my $node, '|-', 'node', '-e', $js or die "node: $!";
print $node '[' . join(',', map { '[' . js_str($_->[0]) . ',' . js_str($_->[1]) . ']' } @out) . ']';
close $node;

# Blank out (nested) {- -} comments, keeping newlines so line numbers
# stay right.  Pragmas {-# #-} are left alone.  Ignores string literals,
# which is good enough for finding foreign imports.
sub strip_block_comments {
  my ($s) = @_;
  my $out = ''; my $depth = 0; my $i = 0; my $n = length $s;
  while ($i < $n) {
    my $two = substr($s, $i, 2);
    # at top level {-# is a pragma; inside a comment it nests like {-
    if ($two eq '{-' && ($depth || substr($s, $i + 2, 1) ne '#')) { $depth++; $i += 2; next }
    if ($depth && $two eq '-}') { $depth--; $i += 2; next }
    my $c = substr($s, $i, 1);
    $out .= $depth ? ($c eq "\n" ? "\n" : '') : $c;
    $i++;
  }
  return $out;
}

sub js_str {
  my ($s) = @_;
  $s =~ s/\\/\\\\/g; $s =~ s/"/\\"/g; $s =~ s/\n/\\n/g; $s =~ s/\r/\\r/g; $s =~ s/\t/\\t/g;
  $s =~ s/([\x00-\x1f])/sprintf('\\u%04x', ord $1)/ge;
  return qq("$s");
}
