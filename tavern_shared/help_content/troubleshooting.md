# Troubleshooting

## I'm not sure if my server is actually running

With the server running (the red "Close Server" button should be glowing and be ready to click), open a Powershell window.

Enter following commands:

```
Test-NetConnection 127.0.0.1 -p 1761
Test-NetConnection 127.0.0.1 -p 1762
```

Both of those tests should return a `TcpTestSucceeded: True`!
If not, the server isn't actually running.

## Other players can not join my server

Grab your public IP from <https://whatsmyip.com/>
Copy it and ask a friend to run this in powershell:

```
Test-NetConnection <your public IP> -p 1761
Test-NetConnection <your public IP> -p 1762
```

Both of those tests should return a `TcpTestSucceeded: True`!

If he's getting false, that means you haven't port forwarded correctly!

## I'm getting kicked to the main menu, help!

This means your patching/modding process has failed. On some machines Windows is preventing the automatic process.
Please refer to FAQ under "Being sent to the main menu and not the server" to do the patching/modding yourself manually.

## The game throws errors and stays on a black screen!

This means your patching/modding process has failed. On some machines Windows is preventing the automatic process.
Please refer to FAQ under "Being sent to the main menu and not the server" to do the patching/modding yourself manually.
