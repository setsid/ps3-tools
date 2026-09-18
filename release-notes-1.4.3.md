## PS3 Tools 1.4.3

Files for PS3HEN are now built properly rather than nearly.

- A file signed for PS3HEN has its loaded sections compressed. That was taken for a choice the tool that built them happened to make, and it is not: a patch with everything else right went on to a HEN console cleanly and the console black screened the moment the patched binary loaded. Surveyed across twenty-eight paired builds, every HEN one compresses and every custom firmware one does not, with no exceptions
- The section table is now built from the program itself rather than copied. A file built from a custom firmware original was inheriting its section list, which is shorter, so three of the parts the console loads were missing from the file
- A file for PS3HEN is now genuinely signed. The 3.55 keyset is the newest one with a usable private key, which is why PS3HEN wants files built against it, and until now the signature field was carried over from the original rather than made
- Nothing changes for a custom firmware console, which follows the file it started from section by section as before

### Download

`ps3-tools-1.4.3.exe`

sha256 `b7ed977bf6bd528af877354069816cfb9068c23fe4ce3f02029aface89f951a9`

Get-FileHash .\ps3-tools-1.4.3.exe -Algorithm SHA256

### Legal

Not affiliated with or endorsed by Activision, Treyarch or Sony. All trademarks are the property of their respective owners.

Every reasonable step has been taken to make this software safe but no guarantee is given. Use it at your own risk. The app backs up the files it modifies. Keep your own backups as well.
