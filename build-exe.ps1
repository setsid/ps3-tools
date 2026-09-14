# Builds dist\ps3-tools.exe.
#
# Requires Python 3 and PyInstaller:  pip install pyinstaller==6.22.3
# Run from anywhere:                  .\build-exe.ps1
#
# One exe, three tools. It replaces bo2-psn-fix.exe and mw3-psn-fix.exe, which
# are redundant once this builds.

$ErrorActionPreference = "Stop"

# Pinned exactly. A different PyInstaller produces a different bootloader and a
# different set of warnings, and an exe nobody can reproduce is not worth
# shipping to someone who is already nervous about running it.
$PyInstallerVersion = "6.22.3"

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Push-Location $root

try {
    # scetool and the two patchers are loaded from disk at run time, so a build
    # missing any of them produces an exe that fails once the user presses the
    # button rather than one that fails to build.
    $required = @(
        "ps3-tools.py",
        "make-icon.py",
        "ps3diag\__init__.py",
        "ps3diag\transport.py",
        "ps3tools\__init__.py",
        "ps3tools\titles.py",
        "tools\scetool\scetool.exe",
        "tools\scetool\data\keys",
        # Vendored copies of the two fixes. Left out of the exe, every file on
        # a patcher screen comes back "not recognised" on a console that is
        # perfectly fine, because nothing can read the patch site.
        "tools\patchers\patch-bo2.py",
        "tools\patchers\patch-mw3.py",
        "certs\scei-dnas-root-05.pem"
    )
    foreach ($item in $required) {
        if (-not (Test-Path -LiteralPath $item)) {
            if ($item -like "tools\scetool\*") {
                throw @"
Missing $item.

scetool is not in this repository. It is naehrwert's work and is bundled into
the built exe rather than redistributed here, the same as in the two standalone
patcher repos. A fresh clone cannot build without it.

Put a copy at:

    tools\scetool\scetool.exe
    tools\scetool\zlib1.dll
    tools\scetool\data\keys
    tools\scetool\data\ldr_curves
    tools\scetool\data\vsh_curves

The whole data folder is needed, keys included: scetool looks its keys up
relative to its own folder, so a copy of the exe on its own cannot decrypt
anything. The keyset must carry key revision 0019, which is what Black Ops II
and Modern Warfare 3 are signed with; that is checked separately below.
"@
            }
            throw "Missing $item. Build from a full checkout."
        }
    }

    # Black Ops II is key revision 0019, which not every keyset carries. A build
    # made against one that does not produces an exe that cannot decrypt
    # anything, and the symptom is a misleading complaint about the klicensee.
    if (-not (Select-String -LiteralPath "tools\scetool\data\keys" -Pattern "^revision=0019" -Quiet)) {
        throw "tools\scetool\data\keys has no revision 0019 entry, so it cannot handle Black Ops II or Modern Warfare 3."
    }

    # Never installed automatically. Pulling a package down mid-build is how a
    # pinned version quietly stops being the version that gets used.
    $installed = (python -m PyInstaller --version 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed. Run: pip install pyinstaller==$PyInstallerVersion"
    }
    if ($installed.Trim() -ne $PyInstallerVersion) {
        throw ("PyInstaller $($installed.Trim()) is installed but this build " +
               "is pinned to $PyInstallerVersion. Either run: pip install " +
               "pyinstaller==$PyInstallerVersion, or change " +
               "`$PyInstallerVersion at the top of this script if you are " +
               "deliberately moving the pin.")
    }

    # The pinned root is the only thing standing between the update check and
    # trusting whatever answers. Checked as a certificate, not as a file: a PEM
    # re-wrapped is the same certificate and is fine, a different certificate
    # is not, whatever the file is called.
    $fingerprint = (python -c "import sys; sys.path.insert(0, '.'); from ps3tools import updates; print(updates.certificate_fingerprint('certs/scei-dnas-root-05.pem'))" 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "certs\scei-dnas-root-05.pem could not be read as a certificate. See certs\README.md."
    }
    $expected = (python -c "import sys; sys.path.insert(0, '.'); from ps3tools import updates; print(updates.CERT_SHA256)")
    if ($fingerprint.Trim() -ne $expected.Trim()) {
        throw ("The bundled root is not the expected certificate.`n`n" +
               "  expected  $($expected.Trim())`n" +
               "  found     $($fingerprint.Trim())`n`n" +
               "That should be SCEI DNAS Root 05, self-signed, CN=SCEI DNAS " +
               "Root 05. Do not build with a substituted certificate.")
    }

    python -c "import PySide6" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PySide6 is not installed. Run: pip install PySide6==6.8.1"
    }


    # The icons are drawn rather than kept, so a fresh checkout has neither.
    if (-not ((Test-Path -LiteralPath "icon.ico") -and (Test-Path -LiteralPath "icon.png"))) {
        python make-icon.py
        if ($LASTEXITCODE -ne 0) {
            throw "make-icon.py exited $LASTEXITCODE."
        }
    }

    # The analysis passes are loaded by name at run time so that a missing one
    # costs its own findings rather than the whole program. PyInstaller cannot
    # see an import that is a string, so each one is named here. A pass added to
    # ps3diag\analysis.py and not added here builds a working exe that silently
    # has one fewer check than the checkout it came from.
    $hidden = @(
        "ps3diag.rules",
        "ps3diag.rules.engine",
        "ps3diag.rules.builtin",
        "ps3diag.rules.inference",
        "ps3diag.rules.storage_rules",
        "ps3diag.rules.games_rules",
        "ps3diag.rules.system_rules",
        "ps3diag.patchstate",
        "ps3diag.psnsafety",
        "ps3diag.isoid",
        "ps3diag.isoreader",
        # The screens register themselves on import and are reached through a
        # module name built at run time, which PyInstaller's analysis cannot
        # follow. Left out, the exe starts with an empty home screen saying
        # "No tools are registered" while a source checkout works perfectly.
        "ps3tools.screens",
        "ps3tools.screens.diagnostics",
        "ps3tools.screens.patcher",
        "ps3tools.screens.about",
        "ps3tools.screens.gameupdates",
        "ps3tools.screens.installpkg",
        "ps3tools.screens.saves",
        "ps3tools.screens.transfer",
        "ps3tools.transfer",
        "ps3tools.updates",
        "ps3tools.consoleactions",
        "ps3tools.savedata",
        "ps3tools.shell.icons",
        "ps3tools.shell.updatebanner",
        "ps3tools.update",
        "ps3tools.crashreport"
    )

    $arguments = @(
        "--onefile", "--windowed", "--name", "ps3-tools",
        "--icon", "icon.ico",
        "--paths", ".",
        "--add-data", "icon.ico;.",
        "--add-data", "icon.png;.",
        "--add-data", "tools\scetool;tools/scetool",
        "--add-data", "tools\patchers;tools/patchers",
        "--add-data", "certs;certs",
        # PyQt5 is on some development machines and PyInstaller will happily
        # bundle both toolkits if it finds them, which doubles the size and can
        # crash at start up when two Qt builds load into one process.
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib"
    )
    foreach ($module in $hidden) {
        $arguments += @("--hidden-import", $module)
    }
    python -m PyInstaller @arguments ps3-tools.py

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller exited $LASTEXITCODE."
    }

    Write-Host ""
    Write-Host "Built dist\ps3-tools.exe"
    Write-Host "Run it from a local drive. scetool cannot run from a UNC path,"
    Write-Host "and the program writes its settings and its output beside"
    Write-Host "itself, so do not put it in Program Files."
}
finally {
    Pop-Location
}
