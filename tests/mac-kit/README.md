# Mac test kit

## Review fixes

A safety review read the script line by line (no Mac was available, so nothing was run). Changes:

- The script starts with a clean set of settings, so your own ssh-agent, mail path and other
  settings that `sudo` passes on can no longer reach the test users or the result file.
- The clipboard is put back only if the fake text is still on it, so something you copy during the
  run is never overwritten. What is on the clipboard is never written into the result file. If the
  clipboard cannot be read, it is not touched at all.
- Firewall (pf): it now empties only the kit's own rules (before, a broader "flush all" could also
  drop the Mac's open firewall connection records). It writes down whether pf was on or off and puts it
  back that way, also after a hard stop.
- The sudo rule file is checked twice: on its own, and then together with the Mac's whole sudo setup.
  If sudo would not accept it, it is removed at once. The test users' sudo "incident" mails are turned
  off, so no mail lands in root's mailbox.
- User ids are checked against company network accounts too, not only local ones.
- New: `sudo ./mac-tests.sh --cleanup` removes what a stopped run left behind. It only removes users,
  groups and folders that carry the kit's own mark.
- The test users' temporary folders in `/private/var/folders` are now removed too.
- Ctrl-C now also stops a test that is running at that moment (before, it could keep running).
- The result files are never written through a link, and the kit refuses to run from a folder every
  user can change.
- `KAPTEST_NO_INTERNET=1` skips the internet tests.
- Bug fixed: the fake person's program stopped after 10 minutes, so later tests could pass by mistake.

Details are in `REVIEW.md`.

## What it is for

This kit checks, on a real Mac, whether the safety walls we designed for the
"box" (the separate user that runs the team's AI chats) really work on macOS.
We wrote the design without a Mac, so we need your Mac to find out which walls hold.

## What it does

1. Makes two hidden test users:
   - `kaptest-alice`: a fake person. It gets fake secrets, for example a file that says
     `CANARY-PERSONAL-FILE-7f3a`, a fake ssh key, a fake Claude sign-in file and a fake chat.
   - `kaptest-box`: the box.
2. Builds the walls around the box: a locked home folder, a sudo rule, a Seatbelt sandbox
   profile, a firewall (pf) rule, and a shared team folder.
3. Tries to break in as `kaptest-box`: read the fake secrets, list the homes, look at the fake
   person's running program, reach its sockets, ssh-agent and local web service, reach the
   internet, type into its terminal, and read the clipboard.
4. Writes one result file, `mac-results.tsv`, one line per attack.
5. Removes everything it made.

It runs the attacks several times: first with no walls (so we know each attack works at all),
then with each wall added. Seeing a leak in the first round is expected and is good.

## What it changes (and undoes)

All names start with `kaptest` (or `Kaptest`). At the end, and also if you press Ctrl-C, it removes:

- the two test users, their groups, their home folders and their temporary folders
- one file in `/etc/sudoers.d/` (`kaptest-box`). It only lets the fake person start the box.
- one firewall rule set (pf anchor `com.apple/kaptest`). The rules only affect `kaptest-box`.
  If the firewall was off, it is on while the test runs and goes back off at the end.
  If it was on, it stays on.
- the folder `/Library/Kaptest` and a few small files in `/Users/Shared`, `/private/tmp`
  and `/private/var/tmp`

The last lines of the result file (`Z-...`) check that all of this is gone and that the firewall is
back the way it was.

## What it never does

- It never reads, lists or changes your own files, your home folder, your keychain, your ssh-agent or
  your settings. The fake person has its own fake home and its own fake ssh-agent.
- It does not change your own user's permissions. (The research says a real setup would
  set the person's home to `700`. This kit only does that to the fake person.)
- It uploads nothing. Only four tests use the internet: the box tries to open `example.com` and
  `1.1.1.1`, to ping `1.1.1.1` and to ask `1.1.1.1` for the address of `example.com`. The kit
  checks that this fails when it should. To skip these four tests, start it with
  `sudo KAPTEST_NO_INTERNET=1 ./mac-tests.sh`.
- It only reads a few facts about the Mac: the macOS version, the processor type, the sudo version,
  whether the firewall is on, and whether System Integrity Protection is on. It checks that
  `/etc/sudoers` loads extra rule files, and that sudo still accepts its whole setup; it does not
  copy or record what is in them.
- One exception, only if you say yes: the clipboard test puts a fake text on your clipboard for
  a few minutes and then puts your old text back. A copied picture or text formatting would be lost.
  Do not copy anything while it runs. If you say no, that test is skipped.

## What you need

- An admin account on the Mac. The script asks for your password once (`sudo`). It does not
  need Recovery mode and does not touch System Integrity Protection.
- Time: we expect 10 to 20 minutes, but we have not timed it on a Mac. Some tests wait a few
  seconds on purpose. You can leave it running.
- Nothing to install. It uses tools that are already on a Mac (bash, perl, curl, sudo, pf).
  It stops at the start and names the tool if one is missing.
- Put the `mac-kit` folder in your own home folder (for example Downloads). It refuses to run
  from a folder every user can change, such as `/tmp`.
- Do the run in a normal Terminal window, logged in at the desktop. (The clipboard and terminal
  tests need your desktop session.) If macOS asks whether Terminal may use a folder, say yes:
  that is the kit writing its result file next to itself.

## How to run it

```
cd ~/Downloads/mac-kit
sudo ./mac-tests.sh
```

It refuses to start if this is not a Mac, if you did not use `sudo`, or if a user, group or
file with a `kaptest` name already exists. Then it explains what it will do and waits for you to
type `YES`.

## How to stop it

Press Ctrl-C once, then wait until it says the results line (up to about a minute). It removes
everything first. Do not close the window while it cleans up.

If it was stopped hard (window closed, power cut, Mac restarted), run:

```
sudo ./mac-tests.sh --cleanup
```

It removes only what carries the kit's own mark, puts the firewall back, and writes
`mac-cleanup.tsv` with the checks.

## Sending the result back

Send us `mac-results.tsv` (it is next to the script). You can open it in Numbers or Excel.
Please also send `mac-log.txt` only if we ask for it.

The file has no personal data: no usernames, file names or text from your own Mac, and never what
is on your clipboard. It lists the macOS version, the processor type, the sudo version, and whether
the firewall and System Integrity Protection were on.

Reading the last column:

- `PASS`: the Mac did what the research expected.
- `FAIL`: the Mac did something different. This is a finding, not your mistake. It can be good
  news (a wall is stronger than we thought) or bad news (a wall leaks).
- `INFO`: only recorded.
- `SKIP`: could not run (for example no internet, or the clipboard test was declined).

## What this kit cannot test

- Things that need a person at the screen or a second Mac: turning Wi-Fi off and on and checking
  that the firewall rules survive; logging out to see that the box is hidden from the login window;
  Migration Assistant; a macOS update.
- The real tools (Claude Code, Codex and so on) running inside the box and Claude Code's own sandbox
  inside ours. The kit only checks that a plain sandboxed command starts inside another sandbox.
- Screen capture (it would copy your screen, and it needs a privacy permission).
- Whether the tools work on a hub on your office Wi-Fi (Local Network privacy), and Time Machine
  or iCloud behaviour.
