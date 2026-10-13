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

Desert Storm / Canyon Storm
---------------------------
No capture needed, and Wireshark is not used: lwvs reads the result mails
the game already keeps on this PC (read-only, it never writes there).

  1. Play on this PC at least once after the battle, so the mail arrives.
  2. In lwvs: "Rankings" > "Storm results" tab.
     "Since" empty = last 7 days; a date (2026-09-14) or "all" goes further.
  3. "Export..." (one JSON per battle) or "Send".

One battle per team: Canyon A and B on Thursday, Desert A and B on Friday.
The Canyon team is written in the mail. The Desert team is NOT: lwvs uses
the alliance's rule, evening slot = A, early afternoon slot = B, and marks
it "declared".

Alliance trains
---------------
Same source, same steps, in the "Trains" tab. One JSON per train: the
conductor, then the VIP or the Guardian, plus the attacks it took. The day
is the game's day (server time, UTC-2), so a train leaving at 01:14 UTC
counts for the day before. A purchased train is marked as such: the daily,
planned train is the other one.

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
