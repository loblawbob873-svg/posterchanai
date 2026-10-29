// PosterFox: run posterfox.cfg (in the install dir) at startup. Not obscured, so anyone can read what it does.
pref("general.config.filename", "posterfox.cfg");
pref("general.config.obscure_value", 0);
// posterfox.cfg reads one file (~/.config/posterchan-desktop/config.json) and registers one stylesheet; both need
// the privileged scope. It is installed root-owned and read-only, like the browser itself.
pref("general.config.sandbox_enabled", false);
