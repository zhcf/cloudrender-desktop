CloudRender Desktop - Python Server (v0.1.0)
====================================================

This package is a standalone build of the Python (aiortc) server.
No Python installation is required on the target machine.

[Launch]
  Double-click start_server.cmd (it requests administrator privileges for
  lock-screen capture and input into elevated windows; the server still
  runs without it, but those features degrade).
  Then open in your browser:
      http://localhost:8080/      (this machine)
      http://<machine IP>:8080/   (other devices on the LAN)
  The page has a Token box pre-filled with 123456, the server's default
  access token. Leave it as-is for a first test; see [Token] below before
  exposing the service.

[Command-line options] (run cloudrender-desktop-server-python.exe to list them all)
  --port 8080        signaling/page port
  --fps 60           frame rate cap
  --bitrate 10000    target bitrate kbps (0 = adaptive)
  --token 123456     access token (default 123456; "" disables the check)
  --help             full option list

[Token]
  Both the page box and --token must match. Change the default before real
  use: edit start_server.cmd (replace --token 123456) and enter the new
  value in the page before Connect. A wrong token shows "Token rejected"
  and stops auto-reconnect.

[Firewall]
  On first launch Windows asks for network access - choose "Allow"
  (Private networks at minimum).
  For cross-machine access, allow inbound TCP 8080.

[Notes]
  - Includes the C++ DXGI capture accelerator (nativecore.dll); the startup
    log shows which capture backend is actually used.
  - Page and signaling share the same port and origin; just open
    http://<ip>:8080/ in a browser - no need to enter a signaling address.
  - The default token 123456 is public knowledge; change it (and the page
    field) before using the server on an office or public network.
  - Lock screen / secure desktop requires administrator privileges;
    without them the server degrades automatically (static lock screen).
  - The Audio button streams the remote desktop's system mix (WASAPI
    loopback, no extra setup). It is independent of the video and can be
    toggled at any time without renegotiating the session.
  - To exit: close the server console window (or press Ctrl+C).