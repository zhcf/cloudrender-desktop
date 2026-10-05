CloudRender Desktop - C++ Server (v0.1.0)
====================================================

This package is a standalone build of the C++ session-core server.
No Python installation is required on the target machine.
Sessions/capture/injection are handled by cloudrender_session.dll (a libwebrtc
session + nativecore); Python only acts as the signaling shell.

[Launch]
  Double-click start_server.cmd (it will request administrator privileges
  automatically - the lock-screen worker needs SeDebugPrivilege; without it
  the lock screen only shows a still frame).
  Then open in your browser:
      http://localhost:8081/      (this machine)
      http://<machine IP>:8081/   (other devices on the LAN)

[Command-line options] (run cloudrender-desktop-server-cpp.exe to list them all)
  --port 8081        signaling/page port
  --fps 60           frame rate cap
  --bitrate 10000    target bitrate kbps (0 = adaptive)
  --dll <path>       session-core DLL path (default: the three DLLs under _internal)
  --help             full option list

[Firewall]
  On first launch Windows asks for network access - choose "Allow"
  (Private networks at minimum).
  For cross-machine access, allow inbound TCP 8081.

[Notes]
  - The three DLLs bundled in the package: cloudrender_session.dll (session
    core), nativecore.dll (DXGI capture/injection) and libwebrtc.dll (WebRTC
    runtime) must stay in the same directory (packaged under _internal root).
  - Page and signaling share the same port and origin; just open
    http://<ip>:8081/ in a browser - no need to enter a signaling address.
  - To exit: close the server console window (or press Ctrl+C).