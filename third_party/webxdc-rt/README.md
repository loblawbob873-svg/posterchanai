# webxdc-rt

Browser-side iroh-gossip transport for webxdc realtime channels, so Armada and
Vector can play the same Mini App session.

Deliberately dumb: everything the two clients must agree on byte for byte — the
topic id, the 36-byte frame trailer, the base32 of a node address — lives in
`src/lib/webxdcRealtime.ts`, tested against Vector's own Rust. This crate moves
opaque bytes and nothing else.

## Build

```
wasm-pack build --release --target web --out-dir <dest>
```

## Two things that will bite

**A browser endpoint still needs an accept loop.** It cannot be dialled
directly, but it can be reached through its relay. Without `endpoint.accept()`
nobody answers: two peers dial each other, neither responds, and both report a
dial timeout while the relay WebSocket sits there happily passing traffic. That
looks exactly like a broken relay and is not one.

**`default-features = false` drops `tls-ring`.** Without it there is no crypto
provider, the build still succeeds, and `bind()` fails at runtime.

## Toolchain

Rust, the `wasm32-unknown-unknown` target, `wasm-pack`, and a real LLVM clang —
Apple Clang cannot target wasm32, so on macOS install LLVM via Homebrew (or
have an NDK clang on PATH). Homebrew binaryen 129 rejects the output, so
`wasm-opt` is off in `Cargo.toml`; a newer binaryen would shrink it.
