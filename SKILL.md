---
name: AutoHackintosh
description: Install macOS on a PC from a Linux host using OpenCore. Use when the user wants to build a Hackintosh, create a macOS OpenCore boot USB, assess whether their hardware can run macOS, generate a hardware-specific EFI, or install macOS with no existing Mac or Windows machine. Handles AMD and Intel graphics, experimental NVIDIA drivers, hardware inventory, ACPI analysis and a diagnostic report.
license: MIT
compatibility: Requires Linux, Python 3.12+, and an internet connection. Uses sudo for disk writes and ACPI dumps.
metadata:
  opencode/autoinvoke: true
---

# AutoHackintosh

Install macOS on a PC from Linux. You do the technical work; the user grants
access to a USB stick and reboots once or twice.

## Read this first

**A successful build is not a working daily driver.** Say so once, plainly, and
move on. Do not oversell.

**Never guess hardware facts.** Everything comes from
`scripts/autohackintosh.py inventory`, which reads real ACPI tables and PCI ids.

**Operations that need explicit human confirmation:** anything that erases,
repartitions or formats a device; overwrites an existing EFI or bootloader;
changes an installed OS; modifies persistent firmware/NVRAM settings; disables
security protections; installs experimental kernel drivers; or runs a recovery
action that could remove a working boot path. Reading hardware, researching
compatibility, preparing files and running the test suite do not.

## The pipeline

Run these in order. Each stage prints JSON; stop and report on any `FAIL`.

```bash
S=scripts/autohackintosh.py            # this skill's directory
W=${WORKDIR:-~/.cache/autohackintosh}

python3 $S deps     --workdir $W       # fetch + verify upstream tools
python3 $S inventory --workdir $W      # hardware, ACPI, firmware facts
python3 $S assess   --workdir $W       # verdict + recommended macOS
python3 $S recovery --workdir $W       # Apple's recovery image
python3 $S efi      --workdir $W --macos 24
python3 $S report   --workdir $W
```

`deps` must run first: it stages 1401's engine, which does the EFI work.
`--macos 24` is Sequoia, `25` is Tahoe.

## Deciding the macOS version

Take the version `assess` recommends unless the user asks otherwise.

NullMoth eligibility requires an exact discrete NVIDIA PCI ID from the pinned
upstream firmware table. Treat unmatched devices as unknown. For eligible
devices, Darwin 24 is the preview default. Explain the install and security
tradeoffs and obtain explicit opt-in before including the experimental driver.
Pass `--include-nullmoth` on both `efi` and `stick` only after that decision.
Stop on a blocking unsupported assessment.

## Before writing the USB

1. Find the device with `python3 scripts/verify_usb.py --list`.
2. Verify it and show the user the model, serial and size using
   `python3 scripts/verify_usb.py --device /dev/sdX`.
3. Review the plan with `stick --dry-run`, including the payload manifest.
4. Have the person authorizing the erase run the command below from a local
   Linux virtual console. They must review the displayed plan and type the
   requested phrase there.

```bash
python3 $S stick --workdir $W --device /dev/sdX \
  --i-authorise-erase <SERIAL>
```

The serial selects the device. The interactive approval authorizes one use of
the exact device state, partition layout, and payload. If the device check,
confirmation, or final plan comparison fails, stop. Do not select a different
device on the user's behalf or answer the prompt for them. Unrestricted root
can bypass in-process safeguards.

## After the stick is written

Tell the user, briefly, and only what their hardware needs:

- **BIOS** — read `references/installation-walkthrough.md` for the full list;
  the common ones are Above 4G Decoding ON, Resizable BAR ON, CSM OFF,
  Secure Boot OFF, VT-d OFF where available. Reboot into the stick.
- **Installer:** pick "macOS Base System", then Disk Utility, then erase the
  target volume as APFS, then install.
- **They need a target disk.** The USB is not one. If they have not chosen one,
  ask before they start.

Then read `references/installation-walkthrough.md` for what they will see and
what usually goes wrong.

## If the screen goes black

Expected on NVIDIA: there is no GPU driver during installation, so the installer
may draw on the firmware framebuffer only, or not at all. Do not treat it as a
failure yet.

When NullMoth was explicitly included, the recovery volume carries
`NullMoth/finish-setup.sh` for the Terminal-based first-account workflow. Full
procedure: `references/nvidia-nullmoth.md`.

## When something fails

Work the problem, do not declare it impossible, and do not invent support.

1. Get evidence: the stage's JSON `error`, the OpenCore log on the stick
   (`EFI/OC/opencore-*.txt`), or the line the screen stopped on.
2. Read the relevant reference — `troubleshooting.md` covers failure
   signatures by name.
3. Form one hypothesis, test it by rebuilding, and compare.
4. Re-run `report` so the change is recorded.

If a stage fails in a new way, inspect the actual error text and the upstream
source before changing anything. `references/linux-engine-notes.md` records
every workaround already known, so you do not rediscover them.

Stop and ask the user when you would need: a BIOS change, a reboot, physical
access, a different disk, or consent for anything destructive.

## Reporting

Always finish with a diagnosis, not just status:

- what hardware was found and which parts are native / patched / experimental
- which macOS and why
- what was written to which device, verified
- what the user must do next
- what is likely to be imperfect (sleep, USB, GPU acceleration)

Point them at `reports/diagnostic-report.shareable.md`. Shareable output is
built from fixed categories, enumerated values, booleans and bounded counts. The unredacted local copy is `reports/private/diagnostic-report.private.json`;
do not share it. **Do not open upstream issues** for problems that have not been
reproduced and understood locally, and never file an AI-written report claiming
others' bug without evidence.

## Ground rules

- Prefer native drivers. Experimental ones are opt-in and clearly labelled.
- Never disable a security feature without telling the user what it costs.
- Download only from pinned, checksummed sources; never pipe a remote script
  into a shell.
- Do not redistribute Apple binaries; `fetch_recovery.py` pulls from Apple.
- Current releases are pinned in `references/upstream-pins.json`. Check there
  before assuming a version.

## Reference map

| File | Read when |
| --- | --- |
| `references/hardware-compatibility.md` | choosing a verdict, unsupported parts |
| `references/installation-walkthrough.md` | guiding the user through setup |
| `references/nvidia-nullmoth.md` | an eligible NVIDIA GPU, or a black screen |
| `references/troubleshooting.md` | a stage or boot failed |
| `references/linux-engine-notes.md` | modifying or debugging the scripts |
| `references/upstream-pins.json` | checking versions and licences |
