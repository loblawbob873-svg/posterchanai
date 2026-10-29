# New in PosterChan: a command bar with an assistant, snap layouts, workspaces, Photos, a screen recorder and emoji packs for Nostr

This release is mostly about the **desktop**. That means the windowed PosterChan desktop you get in the web app on a
wide screen, in the Windows/macOS/Linux desktop app, and as the whole desktop on PosterChanOS. It adds the things a
desktop is expected to have, and two new apps, and makes our custom emoji usable from every other Nostr client.

## A command bar for everything

Press **Ctrl+Space**, or **Ctrl+K** when you are not typing in a text box. On PosterChanOS, **Super+Space**. A
search box opens over whatever you are doing.

Type a few letters and it finds:

- **apps**, and the programs installed on the computer;
- **notes**, **drive files** and **files on this computer**, from the same sources as the desktop's own search;
- **actions**: new post, new note, take a screenshot, screenshot an area, record the screen, arrange windows, full
  screen, the volume mixer and system settings.

Arrow keys move, Enter runs the highlighted result and Esc closes the bar. Two rows are always at the bottom:

- **Search Nostr** for what you typed.
- **Ask AI**, which puts your question into AI chat *without sending it*, so you can read it first and add to it.

On a phone the bar opens as a full-width sheet.

### "Do it": an assistant for the desktop

Type what you want instead of hunting for it: *"notes and calendar side by side"*, *"mute the music"*, *"move the
terminal to workspace 2"*, *"make a note: buy milk tomorrow"*. Pick **Do it** and your server's own AI model plans
it as steps on this desktop:

- open apps;
- put windows in halves, thirds or quarters;
- arrange windows or move them between workspaces;
- set an app's volume;
- take a screenshot or start a recording;
- save a note, draft a post, or search Nostr.

The steps appear as buttons, and **nothing happens until you press one** (or Run all). Posts are only drafted,
never sent. If a step can't run any more, for example because its window was closed, it says so.

## Snap layouts

Hover over a window's **Maximise** button and a set of layout cards appears, the way Windows 11 does it:

- halves;
- thirds;
- two thirds and a third (either way round);
- a half and two quarters;
- quarters;
- full screen.

Click a cell and the window goes exactly there.

## Workspaces

The taskbar now has workspace buttons: **1 2 3 +**.

- Each window belongs to one workspace. Switching shows only that workspace's windows and taskbar buttons.
- **Move to workspace** is on the window's title-bar menu (on PosterChanOS, on its taskbar button's right-click menu).
- **Ctrl+Alt+Left/Right** switch workspaces; add **Shift** and the window in front comes along.
  On PosterChanOS the keys are **Super+Ctrl+Left/Right** and **Super+Ctrl+Shift+Left/Right**. (Super+Left/Right on
  their own still snap a window to half the screen.)
- On PosterChanOS this covers every window: PosterChan apps and installed programs such as PosterFox, Telegram or
  Steam. A window you minimised yourself stays minimised when you come back to its workspace.
- Opening an app that is already open on another workspace takes you to it. So does Alt+Tab to a window on
  another workspace.
- Arranging (Super+G) tiles only the workspace in front.
- **System Settings → Startup apps** lets each app that opens at login choose its workspace. The desktop creates
  the workspaces it needs and starts on the first one.
- Removing a workspace keeps its windows: they join the workspace before it.
- The number of workspaces is remembered on each device.

## A volume mixer per app

On PosterChanOS the system mixer already had a slider for each application. But every PosterChan window was one
application to it, and in a browser or the Windows/macOS app there was no mixer at all.

Now there is a **Volume mixer**: the speaker in the taskbar tray, or "Volume mixer" in the command bar. It has a row
for each open window, plus **Music** and **Other sounds**. Each row has its own slider and mute, and a row says
*playing* while that window is making sound, so you can tell which window it is.

An app's own volume control keeps working. The mixer scales whatever the app sets. Levels are remembered on each
device, because which app is too loud depends on the speakers.

## Photos

**Photos** is a new app in the sidebar (and, from the next Android build, on the launcher). It shows Nostr picture posts (NIP-68) in a
grid:

- **Following** shows pictures from the people you follow; **Global** shows everybody's.
- Tap a picture for the full view, every picture in the post, the caption and the author.
- **Save to album** files any picture, yours or someone else's, into one of your albums, or into a new one.
- **Albums** is the same set of albums as the Albums tab on your profile. They are NIP-51 picture sets, so the
  albums you make here also show up in Olas and other Nostr photo apps.
- **Post a photo** puts every picture you choose into one post, with a caption. Hashtags in the caption become
  tags, and the post can go straight into an album.

On a phone the grid is three columns, edge to edge.

**Synced** shows the pictures in your synced folders. If Folder Sync keeps your phone's camera folder in step with
your computer, every folder of pictures in it is an album here: Camera, Screenshots, WhatsApp Images and so on.
The newest pictures come first, and videos are marked. Tap one to open it in Preview, where you can save or share it.
These pictures stay private: they are decrypted on your device, and nothing is posted. If you don't sync any folders
yet, the tab has a **Set up Folder Sync** button.

Pictures are never cropped any more. A post with several pictures, the Media tab on a profile, Photos, albums and
file thumbnails all show each picture whole, letterboxed in its box.

## Screen Recorder

**Screen Recorder** records the whole screen, one window or a browser tab:

- The microphone and the screen's own sound are both optional, and when you use both they are recorded together.
- You can pause and resume while recording.
- When you stop, the recording opens in a player with **trim handles**.

When it looks right you can:

- **Post** it. The clip is trimmed and converted to MP4 on your PosterChan server, so it plays on every phone,
  iPhones included. Then the normal composer opens with the clip attached.
- **Save to Files**. It goes into a *Recordings* folder, encrypted like the rest of your drive, so only you can
  open it.
- **Download** it.

Nothing leaves your computer until you choose one of those. Phones and tablets don't allow web pages to capture the
screen, so on them the app says so and offers nothing it can't do.

## Our custom emoji, now on all of Nostr

Every server's custom emoji packs already worked in PosterChan, in Ditto and across the fediverse. Other Nostr
clients couldn't use them, because Nostr shares emoji as **emoji sets** (NIP-51, kind 30030), and we didn't
publish any.

Now every pack on a server is published as an emoji set, signed by the server, and kept up to date:

- Shortcodes are converted to names other Nostr clients accept.
- Very large packs are split into several sets.
- A removed pack is withdrawn rather than left pointing at images that no longer exist.

In **Settings → Social**, one button adds all of them to your own emoji list, so Amethyst, noStrudel, Coracle and
other clients offer them in their emoji pickers. Admins can find each set's address under **Admin → Emoji**.

## Fixes worth knowing about

- **Notes:** clicking a picture, PDF, video or song attached to a note opens it in Preview, where you can save or
  share it. It used to download straight away.

- **Thunderbird calendars keep their formatting.** An event with formatted notes, made in Thunderbird and then
  edited in PosterChan, used to come back unreadable in Thunderbird. Now an edit that doesn't touch the notes keeps
  them exactly as Thunderbird wrote them. If you do change the notes, they are saved as plain text.
- **Pictures on the timeline are no longer cropped.** A single picture is shown whole.
- **Pressing Enter after sending a DM no longer closes the conversation.**
