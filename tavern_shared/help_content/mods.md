# Mods

Open the "Mod Manager" (next to "Setup") to see every mod in your game folder, including ones copied in by hand (marked "Untracked"). Select a mod to disable, enable or uninstall it, or right-click it to change its version. Disabling keeps the files.

Click "Get Mods" to browse and install mods. If a mod needs other mods, Get Mods lists them and asks whether to install them too. A mod installed without them reads "Needs mods" until you add them with "Install deps" (or Install Dependencies in the Mod Manager). When an update is out, the Mod Manager button shows a count, and "Update" appears on the mod in Get Mods. A mod marked "Damaged" no longer matches its files; reinstall it.

A source can retire a mod in favour of another. The Mod Manager marks the old one "Replaced" and offers "Switch to" the new one, which installs it and then removes the old one. Pins, modlists and other mods that name the old one get the new one instead.

On the client, close the game before changing mods.

## Joining a modded server

When you join, the launcher matches your mods to the server's first, and shows what will change before it launches the game.

- Mods marked "Exact version required" must match the server's version. The launcher installs that version for you.
- The server's other mods are recommended. Choose "Skip" to leave one out for that server.
- Your mods that the server doesn't run are turned off for that server, not deleted. Choose "Keep" to leave one on for this join, or "Pin" to keep it on everywhere.

## Hosting

Install mods with the server launcher's Mod Manager while the server is stopped. Changes take effect when it next starts. If the client and server launchers share a game folder, they share its mods: joining another server never turns off a mod your server runs, and a mod your running server uses can't change version until it stops.

## Mod Sources

Mods come from the Modding Tavern's list by default. To add another source, open Get Mods, click "Manage Sources" and paste the repository's raw URL. Only add sources you trust: the Modding Tavern doesn't check them.
