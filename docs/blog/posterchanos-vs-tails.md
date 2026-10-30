# Private, and still yours: why we built PosterChanOS instead of telling you to use Tails

Tails is a remarkable piece of work. Boot it from a USB stick, every connection goes through Tor, and when you shut
down it forgets everything. For a journalist meeting a source once, that is exactly right.

But most of us don't live a single session at a time. We have passwords and bookmarks we built up over years, notes
and files, a calendar, our DMs and our people on Nostr. On Tails, privacy means starting from nothing every time, or
carrying an encrypted "persistent" folder that only that one stick can open.

PosterChanOS takes a different bet: **your private data should be easy to reach from anywhere, and hard for anyone else
to reach at all.** It is a full desktop with a Nostr client, a personal cloud and a hardened browser built in, and Tor
whenever you want it.

## Sign in with your phone, and your key never touches the computer

On the PosterChanOS sign-in screen, choose to sign in with a remote signer. A QR code appears. Scan it with PosterChan
on your phone (**Settings → Log in another device**), or with Amber, nsec.app or Primal, and you're in.

Your Nostr private key stays on your phone. The desktop asks the phone to sign each thing it needs signed, over Nostr
(NIP-46). Sitting at a borrowed or untrusted machine? You get your whole account without typing a password or
pasting a key into a computer you don't control. When you're done, end the session from your phone.

Tails cannot do this. Its identity is whatever you bring on the stick, and anything you type into it is typed into
that machine.

## Your personal cloud, over Tor, the moment you sign in

A PosterChan server is a personal cloud you or a friend runs. It holds:

- **Files**: an encrypted drive, plus Folder Sync that keeps folders the same across your computers and your phone
- **Passwords**: a vault encrypted with a key only your account holds
- **Notes**: each note encrypted to your own Nostr key, so the server stores ciphertext it cannot read
- **Calendar, Contacts and Mail**, which also sync to your phone's own apps
- **Photos and Music**
- **DMs**: encrypted Nostr messages (NIP-17)

Sign in on PosterChanOS and all of it is simply there, in the same desktop. Nothing needs to be copied onto the machine
first, so there is nothing on its disk for anyone to find afterwards.

**Tor is one switch.** The PosterChan desktop app can run itself entirely through its own built-in Tor. It holds the
window until the Tor circuit is up, so nothing leaks while Tor is connecting, and if Tor stops it fails closed instead
of quietly reconnecting without it. On PosterChanOS it exits in the United States by default, and you can pick another
country. The server can also publish itself as a **.onion** site, so you reach your cloud through a hidden service
without it ever having a public address.

## PosterFox: a private browser that remembers only what you choose

PosterChanOS's browser is **PosterFox**, our build of Firefox:

- **Private by default.** No history is kept, and cookies, site data, cache and downloads are cleared when it closes.
- **Every tab is its own container**, so a site logged in in one tab cannot see any other tab.
- **Each tab looks like a different real browser.** Trackers can't stitch your tabs together into one profile.
- **Tor tabs.** Right-click a tab or a link, or press Alt+Shift+T, and that tab goes through Tor, DNS included. With
  Tor off it fails rather than connecting directly. **.onion addresses open in Tor by themselves**, as does any site
  you tell it to always open in Tor.
- **uBlock Origin and Privacy Badger** come built in, with strict tracking protection.

What PosterFox *does* keep is the part that's actually yours: **PosterChan Passwords** fills your logins and **syncs
your passwords and bookmarks over Nostr**, encrypted, between PosterFox, Firefox, Chrome and your phone. There is no
Mozilla account and nothing goes to a Big Tech sync server; the relay only ever stores ciphertext. On PosterChanOS
it pairs itself when you sign in: no extra setup.

That's the trade we think most people actually want: **the browser forgets your browsing; your account remembers your
data.**

## Nostr is the operating system's native language

On PosterChanOS, Nostr isn't an app you open; it is how the desktop works:

- Your **identity** is your npub, the same everywhere.
- Your **settings, desktop layout, notes and passwords** are encrypted Nostr documents, so a fresh install, or the live
  USB, picks them up when you sign in.
- **Messages, communities (Concord), calls and posts** are part of the desktop rather than separate apps with separate
  accounts.
- Your data lives on **relays you choose**, including your own, not in one company's database.

Lose the laptop and nothing is lost: sign in on the next machine and your desktop comes back.

## Encrypted and verified on the machine itself

Installed to a disk, PosterChanOS uses full-disk encryption (LUKS) and supports **Secure Boot**, with optional TPM
unlock and a travel mode that asks for the password at every start. The live USB is the same desktop, so you can try it
on any computer, or carry it, without installing anything.

## When Tails is still the right tool

We want to be straight about this. Tails forces **every** connection on the system through Tor and keeps **nothing**
from one session to the next, by design. PosterChanOS is not built that way: Tor is a strong, fail-closed option for the
desktop app and for PosterFox's Tor tabs, not a rule for every program on the machine. If your threat model needs
amnesia and Tor-or-nothing for every program, use Tails, or use both.

For everyone else, the people who want privacy **and** their own data every day, on every device, without trusting a
cloud company with it, PosterChanOS is the better fit.

## Try it

- **PosterChanOS ISO**: boot it live from a USB stick, or install it: https://iso.poster.place/posterchanos.iso
- **PosterChan on Android**: your phone as the key that signs you in.
- **PosterFox**: also for any Linux machine as a portable download: https://github.com/loblawbob873-svg/posterfox/releases

Your data. Your key. Your relays. Private when you want it, there when you need it.
