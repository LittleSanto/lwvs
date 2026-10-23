lwvs - ranking capture for Last War: Survival
=============================================

What you need
-------------
  - A Windows PC where you play Last War (the PC game client).
  - Wireshark with Npcap (next section). Free, one install, no coding.
  - Your alliance's site address and push token. An R4 gets them on the site:
    open the alliance site, "R4 Tools", then "Push feed", and generate a token.
    The address looks like https://<your-alliance>.kwet.eu/api/rankings

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
Unzip the whole folder somewhere you can write (Documents, not Program Files),
then double-click lwvs.exe.

Windows may show "Windows protected your PC". That is SmartScreen: the
program is not signed by a known publisher, not a danger diagnosis.
Click "More info", then "Run anyway".

If your antivirus quarantines lwvs.exe, it is a false positive of the same
kind (an unsigned program that listens on the network). You have to allow
it explicitly.

Connect lwvs to your alliance's site (once)
-------------------------------------------
  1. Open "Settings", card "Stats site".
  2. URL: paste your alliance's address. The field starts with another alliance's
     address (GolD): replace it. "your-alliance.kwet.eu" alone is enough.
  3. Token: paste the token generated in R4 Tools -> Push feed.
  4. Press "Test the connection". Expected: "Connected ... Nothing was sent."
       Token refused (401)   -> wrong or regenerated token: copy it again.
       No push token yet (503) -> an R4 must generate one on the site first.
       Nothing at this address (404) -> check the address.
       Cannot reach ...      -> no internet, or a typo in the address.
  5. Tick "Remember the token" if you do not want to paste it every time. It is
     then saved in plain text on this PC: do not tick it on a shared PC.
A token is a password for your alliance's data: do not post it in a chat.
Regenerating it on the site cuts off the old one.

Usage
-----
  1. Launch Last War.
  2. In lwvs, "Stats" screen: "Start capture". The first time, lwvs finds the
     network interface and port by itself (a few seconds, keep the game
     open), then the capture starts.
  3. In the game, open the ranking screens you want (VS, members,
     server ranking...). Each screen opened = one ranking captured.
     A screen that is already open must be RELOADED (close, then reopen).
  4. "Stop", then "Save JSON..." or "Send N to the site". Both buttons
     cover everything ready on the screen: rankings, Storm and trains.
     The site URL and token are entered once, in "Settings" (see above),
     along with the language (English, Français, Deutsch).

The "Decoded fully" tile must stay at ~100%. Below that, decoding
misses part of what goes by: report it.

Desert Storm / Canyon Storm
---------------------------
No capture needed, and Wireshark is not used: lwvs reads the result mails
the game already keeps on this PC (read-only, it never writes there).

  1. Play on this PC at least once after the battle, so the mail arrives.
  2. In lwvs: "Stats" screen, the Storm section of the table (read at
     startup, ↻ to read again). "Mail since": last 7 days by default; a date
     (2026-09-14) or "all" goes further.
  3. "Save JSON..." (one JSON per battle) or "Send to the site".

One battle per team: Canyon A and B on Thursday, Desert A and B on Friday.
The Canyon team is written in the mail. The Desert team is NOT: lwvs uses
the alliance's rule, evening slot = A, early afternoon slot = B, and marks
it "declared".

Alliance trains
---------------
Same source, same steps, in the trains section of the "Stats" screen. One JSON per train: the
conductor, then the VIP or the Guardian, plus the attacks it took. The day
is the game's day (server time, UTC-2), so a train leaving at 01:14 UTC
counts for the day before. A purchased train is marked as such: the daily,
planned train is the other one.

What lwvs writes on your disk
-----------------------------
Nothing in this folder. Everything goes to:
  %LOCALAPPDATA%\lwvs\
  - lwvs.gui.json   your settings (interface, port, site URL, language,
                    and the token only if you ticked "Remember the token")
  - identity.json   your alliance, remembered
  - lwvs.sqlite3    ONLY if you tick "Keep history locally"

By default no history is kept: the working database is a temporary file
deleted when you close the window.

Privacy
-------
An export contains the names and identifiers of ~200 real players.
"Send to the site" PUBLISHES them to a third party (the URL you entered): that is
not the same thing as writing a local file. The send token is only stored
on disk if you explicitly tick the box.

Something wrong?
----------------
The version number is in the window title: quote it.
If the window does not open at all, a file %LOCALAPPDATA%\lwvs\
lwvs-crash.log may have been written.
