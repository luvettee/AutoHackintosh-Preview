# AutoHackintosh

An AI agent skill for installing macOS on a PC from Linux.

Hand an agent this repository, plug in a USB stick, and say **"install macOS on
this computer."** The agent inspects the hardware, decides what macOS version is
appropriate, builds a hardware-specific OpenCore EFI, downloads Apple's recovery
image, writes a bootable stick, and walks you through the parts only a human at
the keyboard can do.

No Mac required. No Windows required. No OpenCore expertise required.

> **Read [What this does not do](#what-this-does-not-do) before you start.**
> Getting macOS installed and having a computer you enjoy using are different
> problems.

---

## What this project does

Hackintoshing has a high front-loaded cost: learn OpenCore's config format, work
out which kexts your chipset needs, dump your ACPI tables, find out that your
specific board needs one quirk toggled, debug a boot that stops at a line nobody
has posted about. Most of that is research, not judgement, and it is the reason
people give up.

AutoHackintosh moves that work to an agent. It uses mature, existing tools rather
than reimplementing them:

| Upstream | Role |
| --- | --- |
| [OpenCore](https://github.com/acidanthera/OpenCorePkg) | Bootloader, and `ocvalidate` for validation |
| [Dortania](https://dortania.github.io/OpenCore-Install-Guide/) | The reference knowledge base |
| [OpCore-Simplify](https://github.com/lzhoang2801/OpCore-Simplify) | EFI generation |
| [Hardware Sniffer](https://github.com/lzhoang2801/Hardware-Sniffer) | Hardware and ACPI inventory |
| [1401](https://github.com/nullmoth/1401) | Compatibility rules and policy engine |
| [ACPICA](https://github.com/acpica/acpica) | `iasl`, the ACPI disassembler |
| [NullMoth NVIDIA](https://github.com/nullmoth/nvidia-macos-driver) | Optional: GeForce RTX (Turing and newer) acceleration |

What the agent does on its own:

- Inventories the machine from real evidence — PCI IDs, ACPI tables, DMI,
  firmware state — not a model-name guess.
- Produces a per-component verdict: native, needs patches, experimental driver,
  or unsupported.
- Recommends a macOS release based on that hardware, preferring the one with the
  most evidence behind it.
- Fetches and checksum-verifies every dependency at pinned versions.
- Generates, then validates, the OpenCore configuration with the `ocvalidate`
  that matches the OpenCore release.
- Writes the USB, re-verifying device identity immediately before erasing.
- Produces a redacted diagnostic report you could paste into an issue.

## What this does not do

**Automated installation does not mean effortless ownership.**

A working install is the start of the work, not the end. Specifically:

- **macOS updates can break it.** An update may overwrite the bootloader, drop a
  driver, or invalidate a patch. You will need to re-apply things.
- **Hardware support is uneven.** Some parts work natively; some need kexts that
  lag; some never work. The assessment tells you which, before you start.
- **Unsupported drivers cost real stability.** Third-party GPU drivers in
  particular can mean stutter, freezes, broken sleep, or all three.
- **This project covers initial installation and essential post-install
  configuration.** It does not promise long-term maintenance, ongoing update
  survival, or daily-driver reliability.

If macOS must work reliably for income or critical work, **buy a Mac.** This tool
cannot make unsupported hardware supported.

## Your mileage may vary

Success depends on your **hardware, firmware, chosen macOS version, available
drivers, and the capability of the model you run this with.**

- Some machines work on the first attempt.
- Some need substantial back-and-forth.
- Some do not work at all, and no amount of configuration changes that.

**A successful installation is not necessarily a stable or enjoyable daily-driver
experience.** A machine that reaches the desktop can still be unpleasant to use.

## Performance and cost

Inference cost depends entirely on your provider and model. A cheap,
capable-enough model can do this for very little; the work is mostly reading tool
output and making small decisions. Actual cost and installation time vary, and
difficult hardware can generate a lot of back-and-forth.

## Professional use

If you need macOS for professional or mission-critical work, **use genuine Apple
hardware.** The support, the updates, and the absence of driver roulette are
worth more than the price difference.

If you proceed anyway, understand:

- **Experimental drivers** are labelled as such throughout. They are third-party
  and not guaranteed.
- **Unsupported hardware** is reported as unsupported, not as "probably fine."
- **Apple's licence restricts installing macOS on non-Apple hardware.** That is a
  legal constraint you accept yourself; this project does not grant permission,
  and it does not bundle Apple's software — `fetch_recovery.py` downloads the
  recovery image from Apple's own servers on your behalf, as you would.

## AI disclosure and community

This project is built on other people's work. The Hackintosh community — the
Dortania authors, OpenCore's maintainers, the kext developers, OpCore-Simplify's
author, 1401's author, the NullMoth driver's author — produced everything that
makes this possible. AutoHackintosh orchestrates it; it does not replace it.

Openly: **this project uses AI agents to drive the installation.** The
deterministic parts are ordinary scripts you can read and test. The judgement
parts are an AI model's.

That has a consequence worth naming. AI makes it easy to produce configurations
nobody understands and then ask strangers to debug them. This project tries to
address that directly:

- Every build produces a **redacted diagnostic report** containing the hardware,
  versions, kexts, patches, decisions, logs, changes made to upstream tools, and
  remaining issues — the things a maintainer would otherwise have to ask for.
- Shareable reports include constrained categories and validation results.
  Keep raw inventory, configuration, and logs in the private report.
- Do not file upstream issues for problems you have not reproduced and
  understood. An AI's confident guess is not a bug report.

The goal is to lower the barrier to *starting*, not to devalue manual
Hackintoshing or shift work onto volunteers.

## Requirements

- **Linux** on the machine you are installing *from* (not the target).
- **Python 3.12+** and `sudo` for disk writes and ACPI dumps.
- **7-Zip** (`7z`) to inspect the signed recovery image. The compiler build
  requires `make`, a C compiler, flex and bison.
- USB preparation requires `lsblk`, `wipefs`, `sgdisk`, `partprobe`,
  `udevadm`, `mkfs.vfat`, and `mkfs.hfsplus`.
- **A USB stick, 16 GB or larger.** It holds OpenCore and the recovery
  environment.
- **A target disk** with enough room for macOS (~64 GB minimum).
- **Wired network** on the target during installation.
- An agent that can run shell commands. [OpenCode](https://opencode.ai) is the
  primary target; the layout follows the `SKILL.md` convention other agents use.

Linux live environments mostly work, provided the tools above are available.

## Install

```bash
git clone https://github.com/luvettee/AutoHackintosh-Preview ~/.config/opencode/skills/autohackintosh
```

Or copy this directory to any skill source root, e.g. `.opencode/skills/` in a
project. The skill ID is derived from the directory name.

Then, in an agent session:

> Install macOS on this computer.

The agent loads the skill and takes it from there.

## Using the scripts directly

Every stage is a standalone, testable command:

```bash
S=scripts/autohackintosh.py
W=~/.cache/autohackintosh

python3 $S deps     --workdir $W   # fetch and verify upstream tools
python3 $S inventory --workdir $W  # hardware, ACPI, firmware
python3 $S assess   --workdir $W   # verdict and recommended macOS
python3 $S recovery --workdir $W   # Apple's recovery image
python3 $S efi      --workdir $W --macos 24
python3 $S report   --workdir $W
```

Safety-relevant tools:

```bash
scripts/verify_usb.py --list                       # candidate devices
scripts/verify_usb.py --device /dev/sdX            # verify identity
scripts/make_usb.py --device /dev/sdX --efi ... \
    --recovery-dir ... --dry-run                   # plan, change nothing
```

`make_usb.py` uses a layered gate:

1. Run with `--dry-run` to inspect the device, proposed partition layout, and
   complete payload manifest.
2. The person approving the write runs the command on a foreground local Linux
   virtual console with `--i-authorise-erase <SERIAL>`. The serial must match
   exactly. Review the plan at the prompt and type the phrase shown there.
3. The writer backs up the partition table and reconstructs the plan immediately
   before erasure. It compares the disk state and every payload entry, then
   consumes the approval once. The approved payload is copied from private
   staging and read back. Both volumes must unmount successfully.

The writer refuses confirmation from a pipe, SSH session, or pseudoterminal.
Unrestricted root can bypass in-process safeguards. Keep control of the local
console and the host when authorizing a write.

The `stick` stage verifies the exact EFI, inventory and ACPI inputs against
the build's private validation record. It reruns the matching OpenCore
validator on the staged configuration before asking for approval.

NullMoth is experimental and disabled by default. A verified PCI device ID
from the pinned upstream firmware table is required for eligibility. Ask the
operator before using `--include-nullmoth` on both the EFI build and the
`stick` command. The flag is recorded in the validation evidence and erase
plan. A blocking unsupported assessment stops media preparation.

For non-destructive preparation — hardware inventory, compatibility, fetching
dependencies, building and validating an EFI — no approval is needed at all.

## How it works

```
Hardware Sniffer (Linux backend)     ->  Report.json + ACPI dump
        |
1401 engine (headless, on Linux)     ->  policy rules, compatibility, kexts
        |
OpCore-Simplify (1401's fork)        ->  config.plist, SSDTs, kexts
        |
ocvalidate (matching version)        ->  rejects a bad config before you boot
        |
Apple recovery + HFS+ partition      ->  bootable OpenCore USB
```

The interesting parts are where the upstream tools assume Windows or macOS. See
[`references/linux-engine-notes.md`](references/linux-engine-notes.md) for the
specific problems and their fixes.

## Repository layout

```
SKILL.md                    agent instructions (loaded by the model)
README.md                   this file
scripts/
  autohackintosh.py         orchestrator: inventory, assess, deps, recovery, efi, stick, report
  verify_usb.py             device identity verification (safety)
  make_usb.py               partition and write the stick (safety)
  fetch_deps.py             pinned, checksummed dependency fetch
  fetch_recovery.py         Apple recovery download and chunklist verification
  iasl_provision.py         build the pinned ACPICA disassembler
  report_normalize.py       repair Linux-only schema mismatches
  linuxcompat.py            runtime repairs for 1401's engine on Linux
  diagnostic_report.py      redacted shareable report
references/                 progressive-loading knowledge
tests/                      offline test suite
```

## Testing

```bash
python3 -m unittest discover -s tests -v
```

The offline suite uses synthetic device state and mocked disk commands.

## Status and scope

Experimental preview for initial installation. Per-machine firmware, macOS
boot, user-facing features, and updates require separate verification.
AutoHackintosh does not maintain an installed system.

## Licence

MIT for this project's own code. Upstream components keep their own licences —
see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Note that two of them
(1401 and the NullMoth driver) are **non-commercial**.
