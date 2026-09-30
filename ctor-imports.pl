#!/usr/bin/env perl
# ctor-imports.pl [--dry-run] DIR...
#
# On wasm, a foreign import javascript argument/result of a newtype
# (JSString, Callback, EventTarget, SomeArrayBuffer, ...) is only
# marshalable if the newtype's constructor is in scope.  This finds the
# foreign imports that are live on wasm in every .hs file under DIRs,
# collects the type constructors in their signatures, and for each one
# whose data constructor is not already in scope adds (once per file,
# before the first top-level import)
#
#   #if defined(wasm32_HOST_ARCH)
#   import Some.Module (T(..))
#   #endif
#
# The provider table comes from the export lists of GHCJS.DOM.Types,
# ghcjs-base's exposed modules and the modules under DIRs.  Types it
# cannot place are reported, not guessed.
use strict; use warnings;
use File::Find;

my $dry = 0;
if (@ARGV && $ARGV[0] eq '--dry-run') { $dry = 1; shift }
my @dirs = @ARGV or die "usage: $0 [--dry-run] DIR...\n";
my $W = '/home/beshers/git.wasm';

# Types that marshal directly, or are not newtypes we can unwrap.
my %prim = map { $_ => 1 } qw(IO JSVal Int Word Double Float Bool Char Ptr
  FunPtr StablePtr Int8 Int16 Int32 Int64 Word8 Word16 Word32 Word64 CInt
  CUInt CDouble CChar CSize Maybe Either String Integer Any State RealWorld);

# ---- provider table: type name -> module exporting T(..) -------------
my %provider;
sub module_name { my ($src) = @_; $src =~ /^\s*module\s+([A-Z][\w.]*)/m ? $1 : undef }
sub scan_exports {
  my ($file, $prio) = @_;
  open my $fh, '<', $file or return; local $/; my $src = <$fh>; close $fh;
  my $mod = module_name($src) or return;
  if ($src =~ /^\s*module\s+[\w.]+\s*\((.*?)\)\s*where/ms) {
    my $ex = $1;
    while ($ex =~ /\b([A-Z]\w*)\s*\(\s*(?:\.\.|[A-Z]\w*)\s*\)/g) {
      $provider{$1} //= [$mod, $prio];
      $provider{$1} = [$mod, $prio] if $prio < $provider{$1}[1];
    }
  } else {                          # no export list: exports everything
    while ($src =~ /^(?:newtype|data)\s+([A-Z]\w*)/mg) {
      $provider{$1} //= [$mod, $prio];
    }
  }
}
scan_exports("$W/ghcjs-dom/ghcjs-dom-javascript/src/GHCJS/DOM/Types.hs", 0);
scan_exports("$W/ghcjs-base/wasm/GHC/JS/Foreign/Callback.hs", 0);
# (exposed modules that export a constructor on wasm only, like
# JavaScript.TypedArray.ArrayBuffer's SomeArrayBuffer(..), are picked up
# below because the export-list scan ignores CPP.)
{ # ghcjs-base exposed modules
  open my $fh, '<', "$W/ghcjs-base/ghcjs-base.cabal" or die; local $/; my $c = <$fh>;
  # Only exposed modules (there are several exposed-modules fields; each
  # runs until the next field or conditional).  Internal modules are
  # other-modules and cannot be imported.
  my %mods;
  while ($c =~ /exposed-modules:(.*?)(?=\n\s*[a-z-]+:|\n\s*(?:if|else)\b|\z)/gs) {
    $mods{$_} = 1 for $1 =~ /\b([A-Z]\w*(?:\.[A-Z]\w*)*)\b/g;
  }
  for my $m (sort keys %mods) {
    (my $p = $m) =~ s{\.}{/}g;
    scan_exports("$W/ghcjs-base/$p.hs", 1) if -f "$W/ghcjs-base/$p.hs";
  }
}
my @files;
find(sub { push @files, $File::Find::name if /\.hs$/ && $File::Find::name !~ m{/dist|/ZZZ/}}, @dirs);
scan_exports($_, 2) for @files;

# type synonyms: name -> head type constructor of the right-hand side
# (by defining module), e.g. ArrayBuffer -> SomeArrayBuffer.
my (%syn, %synmod);
sub scan_syns {
  my ($file) = @_;
  open my $fh, "<", $file or return; local $/; my $src = <$fh>; close $fh;
  my $mod = module_name($src) or return;
  while ($src =~ /^type\s+([A-Z]\w*)[^=\n]*=\s*(?:[A-Z]\w*\.)*([A-Z]\w*)/mg) {
    $syn{$1} //= $2; $synmod{$mod}{$1} = $2;
  }
}
{ my @gb; find(sub { push @gb, $File::Find::name if /\.hs$/ }, "$W/ghcjs-base");
  scan_syns($_) for @gb, @files; }
$provider{JSString} = ['GHCJS.Types', -1];   # exports JSString(..) on wasm
$provider{Callback} = ['GHC.JS.Foreign.Callback', -1];

# ---- per file ----------------------------------------------------------
my (%unknown, $changed);
for my $file (sort @files) {
  open my $fh, '<', $file or next; my @L = <$fh>; close $fh;
  next unless grep { /wasm32_HOST_ARCH/ } @L;
  my $src = join '', @L;

  # Walk CPP, tracking whether each line is live on wasm.
  my @stack; my @live; my $sig = ''; my @sigs;
  my $in_fi = 0;
  for my $i (0..$#L) {
    my $l = $L[$i];
    if ($l =~ /^#\s*if(n?def)?\b(.*)/) {
      my $c = $2; my $v;
      if ($c =~ /^\s*defined\s*\(\s*wasm32_HOST_ARCH\s*\)\s*$/) { $v = 1 }
      elsif ($c =~ /^\s*!\s*defined\s*\(\s*wasm32_HOST_ARCH\s*\)\s*$/) { $v = 0 }
      elsif ($c =~ /__GHCJS__/ && $c !~ /!\s*(defined\s*\(\s*)?__GHCJS__/ && $c !~ /\|\|/) { $v = 0 }
      else { $v = undef }
      push @stack, [$v, 0];
    } elsif ($l =~ /^#\s*elif/) { $stack[-1] = [undef, 1] if @stack }
    elsif ($l =~ /^#\s*else/) { if (@stack) { my ($v) = @{$stack[-1]}; $stack[-1] = [defined $v ? 1 - $v : undef, 1] } }
    elsif ($l =~ /^#\s*endif/) { pop @stack }
    my $alive = !grep { defined $_->[0] && $_->[0] == 0 } @stack;
    next if $l =~ /^#/;
    # A signature shared by both backends' foreign import lines follows
    # the #endif; the dead-branch import line must not end it.
    next if $in_fi && !$alive;
    if ($in_fi) {
      if ($l =~ /^\s/ && $l !~ /^\s*$/) { $sig .= $l; next }
      push @sigs, $sig; $in_fi = 0; $sig = '';
    }
    if ($alive && $l =~ /^foreign\s+import\s+javascript\b/) { $in_fi = 1; $sig = $l }
  }
  push @sigs, $sig if $in_fi;

  my %need;
  for my $s (@sigs) {
    $s =~ s/"(?:[^"\\]|\\.)*"//gs;              # drop the JS snippet
    my ($ty) = $s =~ /::(.*)/s or next;
    $ty =~ s/--[^\n]*//g;
    $ty =~ s/^.*=>//s;                        # drop class constraints
    while ($ty =~ /(?<![\w.'])(?:[A-Z]\w*\.)*([A-Z]\w*)/g) { $need{$1} = 1 }
  }
  my @add;
  # Expand synonyms, choosing by where this file imports the name from.
  for my $t (sort keys %need) {
    my $seen = 0;
    while (!$prim{$t} && !($provider{$t} && $provider{$t}[1] < 0) && $seen++ < 5) {
      # the first import whose import list names $t
      my $from;
      while ($src =~ /^import\s+(?:qualified\s+)?([\w.]+)[^(\n]*\(([^\n]*)\)/mg) {
        my ($m, $list) = ($1, $2);
        if ($list =~ /(?<![\w'.])$t(?![\w'])/) { $from = $m; last }
      }
      pos($src) = undef;
      my $to;
      if (defined $from) {
        last if $from =~ /^GHCJS\.DOM\b/;
        ($to) = grep { defined } map { $synmod{$_}{$t} } grep { $_ eq $from || index($_, "$from.") == 0 } keys %synmod;
        # (a synonym from elsewhere, e.g. JSON's Object, is not this name)
      } else {
        $to = $syn{$t} unless $provider{$t};
      }
      last unless defined $to;
      delete $need{$t}; $t = $to; $need{$t} = 1;
    }
  }
  for my $t (sort keys %need) {
    next if $prim{$t};
    next if $src =~ /^(?:newtype|data)\s+$t\b/m;                     # defined here
    # T(..) or T(T) in an unqualified import's list (not the module name:
    # "import Data.JSString (JSString)" imports only the type)
    my $imported = 0;
    while ($src =~ /^import\s+(?!qualified)[\w.]+(?:\s+as\s+\w+)?\s*\(([^\n]*)\)/mg) {
      $imported = 1 if $1 =~ /(?<![\w'.])$t\s*\(\s*(?:\.\.|$t)\s*\)/;
    }
    next if $imported;
    my $p = $provider{$t};
    unless ($p) { $unknown{$t}{$file} = 1; next }
    my $pm = $p->[0];
    next if $src =~ /^module\s+\Q$pm\E\b/m;
    # whole-module unqualified import of the provider
    next if $src =~ /^import\s+(?!qualified)\Q$pm\E(?:\s+as\s+\w+)?\s*$/m;
    push @add, "import $pm ($t(..))\n";
  }
  next unless @add;
  my $at;
  my $depth = 0;
  for my $i (0..$#L) {
    $depth++ if $L[$i] =~ /^#\s*if/; $depth-- if $L[$i] =~ /^#\s*endif/;
    if ($depth == 0 && $L[$i] =~ /^import\s/) { $at = $i; last }
  }
  unless (defined $at) { print "NO IMPORT ANCHOR: $file\n"; next }
  my %seen; @add = grep { !$seen{$_}++ } @add;
  print(($dry ? "would add" : "add"), " to $file:\n", map { "    $_" } @add);
  next if $dry;
  splice @L, $at, 0, "#if defined(wasm32_HOST_ARCH)\n", @add, "#endif\n";
  $L[0] = "{-# LANGUAGE CPP #-}\n$L[0]" unless $src =~ /LANGUAGE[^#]*\bCPP\b/;
  open my $oh, '>', $file or die; print $oh @L; close $oh; $changed++;
}
for my $t (sort keys %unknown) {
  print "UNKNOWN type $t in: ", join(' ', sort keys %{$unknown{$t}}), "\n";
}
print "changed ", ($changed // 0), " files\n" unless $dry;
