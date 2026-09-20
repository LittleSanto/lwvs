lwvs - ranking capture for Last War: Survival
=============================================

FIRST: install Wireshark
------------------------
lwvs does not sniff the network itself: it delegates to tshark, which ships
with Wireshark. Without Wireshark, lwvs starts but cannot capture anything,
and says so at the top of its window.

  1. Download Wireshark: https://www.wireshark.org/download.html
  2. During installation, LEAVE the Npcap install TICKED.
  3. The Npcap installer offers a checkbox
     "Restrict Npcap driver's access to Administrators only".
     DO NOT TICK IT. Ticked, no interface will see a single packet
     unless lwvs is run as administrator.
  4. Reboot if Npcap asks for it.

Run lwvs
--------
Double-click lwvs.exe (in this folder).

Windows may show "Windows protected your PC". That is SmartScreen: the
program is not signed by a known publisher, not a danger diagnosis.
Click "More info", then "Run anyway".

If your antivirus quarantines lwvs.exe, it is a false positive of the same
kind (an unsigned program that listens on the network). You have to allow
it explicitly.

Usage
-----
  1. Launch Last War.
  2. In lwvs: "Detect". Keep the game open: detection stops as soon as
     a game frame is recognised.
  3. "Start".
  4. In the game, open the ranking screens you want (VS, members,
     server ranking...). Each screen opened = one ranking captured.
     A screen that is already open must be RELOADED (close, then reopen).
  5. "Stop", then "Export" or "Send now".

The guard shown at the bottom must stay at ~100%. Below that, decoding
misses part of what goes by: report it.

What lwvs writes on your disk
-----------------------------
Nothing in this folder. Everything goes to:
  %LOCALAPPDATA%\lwvs\
  - lwvs.gui.json   your settings (interface, port, site URL)
  - identity.json   your alliance, remembered
  - lwvs.sqlite3    ONLY if you tick "Keep history locally"

By default no history is kept: the working database is a temporary file
deleted when you close the window.

Privacy
-------
An export contains the names and identifiers of ~200 real players.
"Send now" PUBLISHES them to a third party (the URL you entered): that is
not the same thing as writing a local file. The send token is only stored
on disk if you explicitly tick the box.

Something wrong?
----------------
The version number is in the window title: quote it.
If the window does not open at all, a file %LOCALAPPDATA%\lwvs\
lwvs-crash.log may have been written.
