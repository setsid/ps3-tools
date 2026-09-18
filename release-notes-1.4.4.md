## PS3 Tools 1.4.4

A fourth fix, Modern Warfare 2, and a new tool that graphs what the console is doing while it does it.

- **Modern Warfare 2 stats fix.** Multiplayer showing level 1 and keeping nothing, on any PSN account made after late 2018. The same identity fault as Black Ops, fixed from the other end: the identity the server holds you under is in the reply the game reads when it signs in, so the fix takes that rather than working one out. An account that already works is handed the identity it already had, which is why this one has no tick box in front of Apply
- That fix is built from work by [Jakes625](https://github.com/jacob-schroeder/IW4-Binaries), with his permission. This program's own build of it comes out byte for byte the same as his: the instruction at the hook, the forty four bytes of the cave, and the change to the program header that makes the cave executable. His releases carry about twenty security patches and a script compiler as well, and none of that is here
- The fix finds its hook by signature rather than by an address, and checks three facts about the build before a byte is written. Any one of them failing is a refusal saying which
- The home screen is two sections now: the four game fixes under a heading of their own, and everything else under Console tools
- Monitor is a new tool on the home screen. Press Start and it reads the console's status page every few seconds, graphing the CPU and RSX temperatures, the fan and the free space against time, with the current figure above each graph and the lowest and highest of the window beside it
- Holding the pointer over a line reads that moment out, so "how hot did it get while I was in that lobby" is a question the graph answers rather than one you estimate by eye
- The interval runs from two seconds to a minute and the window from five minutes to everything recorded in the session. A console too slow to answer as fast as the interval asks is noticed, counted, and a longer interval suggested
- It reads only while it is the screen in front of you unless you tick Keep recording, and only the page the diagnostic already reads. Nothing is written to disk unless you press Save, which writes every reading to a CSV on your Desktop
- Seventy and eighty degrees are marked on the graph, and every spell above them is listed underneath with the times and the peak. A console that switched itself off an hour ago is still answered for, which a figure that has since come back down cannot do
- A band under the temperatures says which game the console had loaded, so a climb and the launch that caused it are the same moment on the page
- Open readings loads a saved CSV back and graphs it with no console present, so somebody can send you their afternoon and you can look at it
- A tick box keeps the recording going while you use another tool. Off by default, and forgotten when the program closes
- A console that stops answering leaves a break in the line rather than a straight line drawn across the gap, and the footer counts the readings that went unanswered. Changing the console's address starts a new graph

### Download

`ps3-tools-1.4.4.exe`

sha256 `add the hash of the built exe here`

Get-FileHash .\ps3-tools-1.4.4.exe -Algorithm SHA256

### Legal

Not affiliated with or endorsed by Activision, Treyarch or Sony. All trademarks are the property of their respective owners.

Every reasonable step has been taken to make this software safe but no guarantee is given. Use it at your own risk. The app backs up the files it modifies. Keep your own backups as well.
