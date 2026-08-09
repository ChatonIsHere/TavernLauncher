"""The community-mods windows, shared by both launchers.

Kept out of tavern_shared.mods' own __init__ on purpose: the logic package is
imported by tests and by headless paths, and it should never drag tkinter in
just because something wanted plan_join. Import the window you need from its
own module.

ModDiffWindow is deliberately NOT here -- it renders a pre-join reconciliation,
and only the client ever joins a server, so it lives in client/core/.
"""
