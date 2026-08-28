# Updating an older Zapstore installation

PosterChan rotated its Android signing key after the former key was exposed. The
old private key is not used for releases. New APKs carry Android's signed
proof-of-rotation from the former certificate to the current certificate.

Zapstore's default AppCatalog relay currently rejects the two certificate-hash
tags that are required to describe that rotation. Zapstore 1.1 supports a
user-selected AppCatalog relay, and PosterChan publishes the complete signed
release graph to its own relay as a compatibility route.

In Zapstore:

1. Open **Profile** and find **App Catalog Relays**.
2. Add `wss://relay.poster.place` without removing the existing Zapstore relay.
3. Choose **Apply Relay Changes** and allow Zapstore to restart.
4. Open PosterChan and update normally.

The compatibility relay contains the publisher-signed application, release,
software asset, and NIP-C1 identity proof. The asset advertises both the former
certificate and the current certificate. Zapstore can therefore pass its
metadata preflight, after which Android verifies the APK's cryptographic
proof-of-rotation. Do not uninstall PosterChan: uninstalling discards local app
data and is unnecessary for this migration.
