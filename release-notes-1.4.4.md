## PS3 Tools 1.4.4

A fourth fix, Modern Warfare 2, and a new tool that graphs what the console is doing while it does it.

- **Modern Warfare 2.** Multiplayer showing level 1 and keeping nothing, on any PSN account made after late 2018. It is the same identity fault as Black Ops and the fix goes the other way about it: the identity the server holds you under is in the reply the game reads when it signs in, so the fix takes that rather than working one out. An account that already works is handed the identity it already had
- That fix is built from work by Jakes625, with his permission, and this program's own build of it comes out byte for byte the same as his. His releases carry a good deal more than the stats fix and none of the rest of it is here
- **Monitor**, a new tool. It reads the console's status page every few seconds while it is open and graphs the temperatures, the fan and the free space over time. Everywhere else in this program reads the console once; this is for the questions only a trend answers, such as whether a fan that climbed during an hour of play ever came back down
- Monitor reads only while it is the screen in front of you, unless you tick the box that keeps it going, and only the one page the diagnostic already reads. Nothing is written anywhere unless you press Save, which writes the readings as a CSV to your Desktop
- A console that stops answering leaves a break in the line rather than a straight line across the gap, and the footer counts how many readings went unanswered
- Under the temperatures is a band saying which game the console had loaded, so a climb and the launch that caused it are the same moment on the page
- Open readings loads a saved CSV back and graphs it with no console present, so somebody can send you their afternoon
- The home screen is two sections now. The four game fixes sit together, and everything else is under Console tools

### Download

`ps3-tools-1.4.4.exe`

sha256 `f0625ca5a05385d0ed4a1f651bdee94319d5e29887869c3c1ae0f8a638bc0f6a`

Get-FileHash .\ps3-tools-1.4.4.exe -Algorithm SHA256

### Legal

Not affiliated with or endorsed by Activision, Treyarch, Infinity Ward or Sony. All trademarks are the property of their respective owners.

Every reasonable step has been taken to make this software safe but no guarantee is given. Use it at your own risk. The app backs up the files it modifies. Keep your own backups as well.
