# Installation walkthrough

What the user sees, and what you say about it. Keep explanations short; the user
did not ask for a course in OpenCore.

## 1. Firmware settings

Tell them to enter the BIOS (usually `Del` or `F2`) and set:

| Setting | Value | Why |
| --- | --- | --- |
| Above 4G Decoding | On | The GPU needs to map memory above 4 GiB |
| Resizable BAR | On | The EFI is built for it; matches tested hardware |
| CSM / Legacy boot | Off | macOS boots UEFI only |
| Secure Boot | Off | OpenCore is unsigned. **This is a security reduction** |
| VT-d / IOMMU | Off (Intel) | A frequent cause of a freeze before macOS starts |
| SATA mode | AHCI | VMD/RAID hides disks from macOS |

Point out the two that matter for security: Secure Boot and SIP. Say what they
lose. Do not make it sound routine.

## 2. Booting the stick

- Power on, then use the one-time boot menu (`F8`, `F11`, `F12` or `Esc`
  depending on vendor) and pick the USB device under its **UEFI** entry.
- The OpenCore picker appears. Choose **macOS Base System**.
- If the stick is not listed: it may be on a USB 3 port the firmware does not
  enumerate, or CSM is still on. Try a USB 2 port.

## 3. The installer

1. Language.
2. **Disk Utility** first: select the target volume, `Erase`, format **APFS**,
   scheme GUID.
3. Close Disk Utility, choose **Install macOS**.
4. The installer downloads the rest of the system from Apple. This needs
   network: wired is the safe bet.
5. Expect at least one automatic reboot. At the picker, choose the newly
   created macOS entry, not the stick again.

**The USB is not an install target.** If the user has not chosen a disk, stop
and ask. Do not let them erase an internal disk holding another OS without a
separate, explicit decision.

## 4. First boot

If there is a display signal, Setup Assistant runs normally.

If the screen is black with a cursor, that is the known no-driver case. Go to
`nvidia-nullmoth.md` and use `finish-setup.sh` from Recovery.

## 5. Rough expectations

| Stage | Time |
| --- | --- |
| Dependencies + EFI generation | 5-20 minutes |
| USB write | 5-15 minutes |
| macOS install | 20-45 minutes |
| First boot on NVIDIA, no driver | Unpredictable; may need the finish-setup route |

Actual time depends on the hardware and download speed.

## 6. What to check once macOS is up

Work through this with the user rather than declaring success:

- Network: does the Ethernet interface appear in System Settings > Network?
- Audio: does the internal speaker/line-out work? (HDMI audio from an NVIDIA
  card will not.)
- USB: try the ports. A port that is dead during install may work after mapping.
- Graphics: `system_profiler SPDisplaysDataType` for the card and VRAM.
- Sleep: try it, and expect trouble. Report what happens rather than repeating
  the marketing answer.

## 7. Preserve the working EFI

Once it boots reliably, copy the EFI off the stick somewhere durable and keep a
second copy on the macOS disk. A macOS update can invalidate the ESP contents;
having the known-good EFI is the difference between a ten-minute fix and a
reinstall.

The `report` stage writes a redacted record of what was built, which is the
starting point for any future debugging.
