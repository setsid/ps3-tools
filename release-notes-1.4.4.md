## PS3 Tools 1.4.4

A new tool: Monitor, which graphs what the console is doing while it does it.

- Monitor is a new tool on the home screen. Press Start and it reads the console's status page every few seconds, graphing the CPU and RSX temperatures, the fan and the free space against time, with the current figure above each graph and the lowest and highest of the window beside it
- Holding the pointer over a line reads that moment out, so "how hot did it get while I was in that lobby" is a question the graph answers rather than one you estimate by eye
- The interval runs from two seconds to a minute and the window from five minutes to everything recorded in the session. A console too slow to answer as fast as the interval asks is noticed, counted, and a longer interval suggested
- It reads only while it is the screen in front of you, and only the page the diagnostic already reads. Nothing is written to disk unless you press Save, which writes every reading to a CSV on your Desktop
- A console that stops answering leaves a break in the line rather than a straight line drawn across the gap, and the footer counts the readings that went unanswered. Changing the console's address starts a new graph

### Download

`ps3-tools-1.4.4.exe`

sha256 `add the hash of the built exe here`

Get-FileHash .\ps3-tools-1.4.4.exe -Algorithm SHA256

### Legal

Not affiliated with or endorsed by Activision, Treyarch or Sony. All trademarks are the property of their respective owners.

Every reasonable step has been taken to make this software safe but no guarantee is given. Use it at your own risk. The app backs up the files it modifies. Keep your own backups as well.
