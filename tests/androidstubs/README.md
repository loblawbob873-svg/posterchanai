Hand-written stand-ins for the platform APIs the Android sources use.

They exist so `tests/test_android_contact_sync.py` can run **javac** over the real
`place.poster.app.contacts.*` sources on a machine with no Android SDK: a typo, a wrong column
constant or a method that does not exist is then a failing test here rather than a broken APK build
half an hour later on CI. Nothing is executed — only type-checked.

They are DELIBERATELY minimal and are not a simulator: only the members the app actually calls, and
signatures copied from the real API (including which methods throw a checked exception, because that
is what decides whether a `try` is required). Adding a call to a new platform method means adding it
here too. Anything a stub cannot express — what ContactsProvider2 does with CALLER_IS_SYNCADAPTER, or
whether an OEM Contacts app honours the edit schema — is a phone's job to confirm, and is written up
in docs/CONTACTS.md instead of pretended at here.
