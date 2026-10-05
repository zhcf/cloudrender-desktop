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

[Command-line options] (run cloudrender-desktop-server-python.exe to list them all)
  --port 8080        signaling/page port
  --fps 60           frame rate cap
  --bitrate 10000    target bitrate kbps (0 = adaptive)
  --help             full option list

[Firewall]
  On first launch Windows asks for network access - choose "Allow"
  (Private networks at minimum).
  For cross-machine access, allow inbound TCP 8080.

[Notes]
  - Includes the C++ DXGI capture accelerator (nativecore.dll); the startup
    log shows which capture backend is actually used.
  - Page and signaling share the same port and origin; just open
    http://<ip>:8080/ in a browser - no need to enter a signaling address.
  - Lock screen / secure desktop requires administrator privileges;
    without them the server degrades automatically (static lock screen).
  - To exit: close the server console window (or press Ctrl+C).